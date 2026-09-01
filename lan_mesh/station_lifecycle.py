"""
进程生命周期与 Web 装配 — 端口选择、FastAPI 装配、WS 推流、dev-reload。

拆分自 station_controller.py (iter-75 Phase 5)。start() 是 Station 进程的总装
入口: 单实例守护 → preflight → 认证/限流/用户装配 → 发现层 → 各守护线程 →
uvicorn。Web 静态目录常量仍留在 station_controller (被测试直接 import),
本模块通过延迟导入取用以避免循环依赖。
"""
import asyncio
import os
import socket
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import create_worker_router
from .cloud_sync import CloudSyncManager
from .config import get_db_path
from .discovery import DiscoveryService
from .logger import get_logger
from .station_api import create_station_router

logger = get_logger("station")


# ── Web UI 模板路径 ─────────────────────────────────────────────
# 定义在此 (_create_app 的直接消费者); station_controller 仍 re-export 这三个
# 名字以保持 `from lan_mesh.station_controller import STATIC_DIR` 等既有 import
# 路径不变 (tests 4 处)。两个模块同属 lan_mesh 包, __file__ 父目录一致。
WEB_DIR = Path(__file__).parent / "web"
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"


class StationLifecycleMixin:
    """进程启停、FastAPI 装配与 WS 推流。

    依赖 StationController 提供的属性: cfg / state / db / director /
    discovery / _running / bot_gateway 等; Web 目录常量经
    station_controller 延迟导入 (见 _create_app)。
    """

    # ── F1.2: 自愈重启 ─────────────────────────────────────────

    def request_restart(self, reason: str = ""):
        """F1.2: 调度 graceful 重启。

        在后台线程中等待 3 秒后关闭进程，
        配合外部进程管理器 (systemd/schtasks) 实现自动拉起。
        """
        import os
        import signal

        logger.warning("自愈重启已触发: %s", reason)

        def _delayed_exit():
            time.sleep(3)
            logger.info("进程即将退出 (reason=%s)", reason)
            os.kill(os.getpid(), signal.SIGTERM if hasattr(signal, 'SIGTERM') else signal.SIGINT)

        t = threading.Thread(target=_delayed_exit, daemon=True, name="restart-worker")
        t.start()

    async def _ws_push_loop(self):
        """定期向 WebSocket 客户端推送最新主机状态 (新主机入站时立即触发)。"""
        self._ws_push_event = asyncio.Event()
        while self._running:
            try:
                # 等待事件触发或 3 秒超时
                try:
                    await asyncio.wait_for(self._ws_push_event.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
                self._ws_push_event.clear()
                hosts = self.db.list_hosts()
                from .station_api import _broadcast
                await _broadcast(self.state, "hosts", [h.to_dict() for h in hosts])

                # 消费同步代码塞入的广播队列 (任务创建/PM启动等事件)
                while self._ws_broadcast_queue:
                    evt = self._ws_broadcast_queue.pop(0)
                    await _broadcast(self.state, evt["type"], evt["data"])
            except Exception:
                pass

    def _queue_ws_broadcast(self, event_type: str, data):
        """从同步代码向 WS 广播队列塞事件 (下次 push loop 迭代时发送)。"""
        self._ws_broadcast_queue.append({"type": event_type, "data": data})
        # 触发 push loop 立即唤醒
        if self._ws_push_event:
            try:
                loop = self._ws_push_event._loop
                loop.call_soon_threadsafe(self._ws_push_event.set)
            except Exception:
                pass

    # ── FastAPI 应用 ───────────────────────────────────────────────

    def _create_app(self) -> FastAPI:
        """创建 Station Director 的 FastAPI 应用。

        包含全部路由:
        - 基础层 (始终可用): 主机注册/心跳/查询/Station API/角色激活
        - Secretary 层 (激活后可用): 任务/Agent/项目/MCP工具/模型路由
        """
        app = FastAPI(title="LAN Mesh Station Director", version="0.1.0")

        # F1.5: 注册限流 + 认证中间件
        from .station_api import api_guard_middleware, configure_mesh_auth
        # Phase 0: 将节点认证配置同步给中间件 (auth_enabled 时才校验)
        configure_mesh_auth(self._mesh_auth_enabled, self._mesh_token)
        # iter-57 (补强#5): 限流双桶阈值配置化 (严格桶防滥用/信任桶保并发)
        from .station_routes_common import configure_rate_limit
        configure_rate_limit(
            self.cfg.observability.api_rate_limit,
            self.cfg.observability.api_rate_limit_trusted)
        # iter-58 (补强#6 F5.2): 多用户权限 — 用户表注入 (空 = 关闭,
        # 所有人持 mesh token 即 boss 向后兼容)
        # iter-63 (团队场景深化): DB 持久化优先 (users 表), config 仅作
        # 首次种子 → token 轮换/角色修改跨重启保留
        from .station_routes_common import (
            configure_users, load_users_from_db, set_users_db,
        )
        set_users_db(self.db)
        configure_users([u.model_dump() for u in self.cfg.security.users])
        load_users_from_db(self.db)
        app.middleware("http")(api_guard_middleware)

        # Station 路由 (含全部 API, Secretary 路由会检查 active 状态)
        station_router = create_station_router(self)
        app.include_router(station_router)

        # Worker 路由 (本机也可被其他节点查询)
        worker_router = create_worker_router(
            collect_info_fn=self._collect_info,
            shared_folder=self.state.shared_folder,
        )
        app.include_router(worker_router)

        # 静态文件
        if STATIC_DIR.is_dir():
            app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

        # iter-56 (补强#4 F5.1): React SPA 挂载 (/spa, hash 路由无需 fallback)
        spa_dir = STATIC_DIR / "spa"
        if (spa_dir / "index.html").is_file():
            app.mount("/spa", StaticFiles(directory=str(spa_dir), html=True),
                      name="spa")

        # Web UI 仪表盘
        @app.get("/", response_class=HTMLResponse)
        async def dashboard():
            html_path = TEMPLATES_DIR / "dashboard.html"
            if html_path.is_file():
                # 禁缓存: 避免 UI 更新后浏览器用旧页面导致按钮无响应
                return HTMLResponse(
                    content=html_path.read_text(encoding="utf-8"),
                    headers={"Cache-Control": "no-cache, must-revalidate"})
            return HTMLResponse(content="<h1>LAN Mesh Station Director</h1><p>dashboard.html 未找到</p>")

        # iter-62 (F5.4 移动端 PWA): Service Worker 根路径挂载
        # (scope 默认 /; SW 注册请求不带 Authorization 头, 认证白名单放行)
        @app.get("/sw.js")
        async def service_worker():
            sw_path = STATIC_DIR / "sw.js"
            if sw_path.is_file():
                return FileResponse(
                    sw_path, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache, must-revalidate"})
            return JSONResponse(status_code=404, content={"detail": "sw.js 未找到"})

        return app

    def _find_available_port(self, start: int, count: int = 20) -> int:
        """查找可用端口。"""
        for port in range(start, start + count):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(("", port))
                    return port
            except OSError:
                continue
        return start

    # ── 生命周期 ───────────────────────────────────────────────────

    def start(self, dev_reload: bool = False):
        """启动 Station Director。

        Args:
            dev_reload: 开发模式, 监控 lan_mesh/ 文件变动自动重启进程。
        """
        self._running = True
        try:
            guardian = self.shadow_dev_manager.start_guardian()
            logger.info("[Station] 影子开发守护已启动: %s", guardian)
        except Exception as exc:
            logger.warning("[Station] 影子开发守护启动失败: %s", exc)
        try:
            optimizer = self.workstation_optimizer.start_guardian()
            logger.info("[Station] 工作站优化守护已启动: %s", optimizer)
        except Exception as exc:
            logger.warning("[Station] 工作站优化守护启动失败: %s", exc)

        # E6: 主机级单实例守护 - 同版本/更新实例在跑则取消启动;
        # 旧版实例在跑则关闭后由本进程接管 (杜绝端口回退双实例)
        from .singleton import ensure_single_instance, register_cleanup
        from .version_sync import local_version_info
        _ver = local_version_info()
        action = ensure_single_instance(
            self.cfg.secretary.api_port,
            commit=_ver.get("commit", ""),
            commit_time=_ver.get("commit_time", 0.0),
            dev_reload=dev_reload,
        )
        if action != "proceed":
            logger.warning("[E6] 单实例仲裁: %s, 本进程退出", action)
            sys.exit(0)
        register_cleanup()

        # 启动前自检 (复用 secretary 自检: 含 DB 路径 + Web 模板检查)
        from .preflight import run_preflight
        if not run_preflight("secretary", self.cfg):
            logger.critical("自检未通过, 启动中止。请根据上述提示修复后重试。")
            sys.exit(1)

        self.state.api_port = self._find_available_port(self.cfg.secretary.api_port)

        # iter-55: 预加载模型资源 (任何模式) — 让位主机远程派发执行 LLM 任务
        # 需要 Key 就绪; Secretary 当选后 activate_secretary 复用已加载配置
        self._load_model_resources()

        logger.info("设备 ID: %s", self.state.device_id)
        logger.info("设备名称: %s", self.state.device_name)
        logger.info("共享目录: %s", self.state.shared_folder.path)
        logger.info("数据库: %s", get_db_path(self.cfg))
        logger.info("HTTP API + Web UI 端口: %d", self.state.api_port)

        # 启动 UDP 发现服务
        self.discovery = DiscoveryService(
            device_id=self.state.device_id,
            device_name=self.state.device_name,
            role="station",
            api_port=self.state.api_port,
            make_packet=self._make_packet,
            on_device_seen=self._on_device_seen,
            discovery_port=self.cfg.discovery.port,
            presence_interval=self.cfg.discovery.presence_interval,
            device_ttl=self.cfg.discovery.device_ttl,
        )
        self.station_director.bind_discovery(self.discovery)
        self.discovery.start()

        # 自注册为主机 (Station Director 本身也是一台主机)
        try:
            self_info = self._collect_info()
            self.station_director.on_host_registered(self_info)
            logger.info("自注册完成: %s (%s)", self_info.device_name, self_info.ip_addresses)
        except Exception as e:
            logger.warning("自注册失败: %s (服务器仍将启动)", e)

        # 云存储同步 (如果启用)
        cloud_cfg = self.cfg.cloud_storage
        if cloud_cfg.enabled and cloud_cfg.endpoint:
            self.state.cloud_sync = CloudSyncManager(
                local_path=str(self.state.shared_folder.path),
                device_id=self.state.device_id,
                endpoint=cloud_cfg.endpoint,
                bucket=cloud_cfg.bucket,
                prefix=cloud_cfg.prefix,
                access_key=cloud_cfg.access_key,
                secret_key=cloud_cfg.secret_key,
                region=cloud_cfg.region,
                secure=cloud_cfg.secure,
                auto_sync=cloud_cfg.auto_sync,
                sync_interval=cloud_cfg.sync_interval,
            )
            self.state.cloud_sync.start_auto_sync()
            logger.info("云存储同步已启动: %s/%s", cloud_cfg.endpoint, cloud_cfg.bucket)

        # 部署采集脚本并生成初始配置报告
        self._deploy_config_script()
        self._refresh_host_config()
        logger.info("配置报告已生成: %s/host_config.json", self.state.shared_folder.path)

        # 启动配置刷新线程
        config_thread = threading.Thread(
            target=self._config_refresh_loop, name="station-config-refresh", daemon=True
        )
        config_thread.start()
        self._threads.append(config_thread)

        # 启动离线清理线程
        prune_thread = threading.Thread(
            target=self._prune_loop, name="station-prune", daemon=True
        )
        prune_thread.start()
        self._threads.append(prune_thread)

        # S3: 启动一次性同步 (版本比对 + API Key 对齐, 替代 60s 轮询)
        sync_thread = threading.Thread(
            target=self._startup_sync_once, name="station-startup-sync", daemon=True
        )
        sync_thread.start()
        self._threads.append(sync_thread)

        # F1: 周期角色无关密钥对齐 (主从无关, config_ts 仲裁收敛)
        align_thread = threading.Thread(
            target=self._align_loop, name="station-align", daemon=True
        )
        align_thread.start()
        self._threads.append(align_thread)

        # iter-44: 错误追踪闭环接线 (F1.4 后半) — 每条错误事件推送 (WS 实时刷新面板)
        # + 突发告警 (事件总线 + Bot, 冷却去重在 tracker 内); 异常不影响启动
        # iter-47: + 落盘持久化回调 (error_log 表, 重启不丢诊断历史)
        try:
            from .error_tracker import error_tracker
            from .event_bus import publish_event

            def _on_error_captured(record: dict):
                try:
                    publish_event("error_captured", record)
                except Exception:
                    pass

            def _on_error_persist(record: dict):
                try:
                    self.db.save_error_record(record)
                except Exception:
                    pass

            def _on_error_burst(module: str, count: int, window_secs: float):
                data = {"module": module, "count": count, "window": window_secs}
                try:
                    publish_event("error_burst", data)
                except Exception:
                    pass
                if self.bot_gateway:
                    try:
                        self.bot_gateway.notify("error_burst", data)
                    except Exception as e:
                        logger.warning("[ErrorTracker] 突发告警推送失败: %s", e)

            error_tracker.set_event_callback(_on_error_captured)
            error_tracker.set_alert_callback(_on_error_burst)
            error_tracker.set_persist_callback(_on_error_persist)
        except Exception as e:
            logger.warning("错误追踪接线失败 (no-op): %s", e)

        # iter-50: F4.2 自动自愈守护线程 (周期扫描诊断 + 安全动作自动执行, 默认关)
        try:
            auto_heal_thread = threading.Thread(
                target=self._auto_heal_loop, name="station-auto-heal", daemon=True
            )
            auto_heal_thread.start()
            self._threads.append(auto_heal_thread)
        except Exception as e:
            logger.warning("自动自愈守护启动失败 (no-op): %s", e)

        # F3.1: 启动自动扩缩容监控
        self._start_autoscaler()

        # F3.4 (iter-64): 跨网段联邦轮询线程 (静态 peer, 配置启用才启动)
        if self.cfg.federation.enabled and self.cfg.federation.peers:
            try:
                federation_thread = threading.Thread(
                    target=self._federation_loop, name="station-federation", daemon=True
                )
                federation_thread.start()
                self._threads.append(federation_thread)
            except Exception as e:
                logger.warning("联邦轮询线程启动失败 (no-op): %s", e)

        # Secretary 自动选举 (First-Station-Wins, 后台线程不阻塞 API 启动)
        election_thread = threading.Thread(
            target=self._secretary_election, name="secretary-election", daemon=True
        )
        election_thread.start()
        self._threads.append(election_thread)

        # 创建 FastAPI 应用
        app = self._create_app()

        # 添加 WS 推送后台任务
        @app.on_event("startup")
        async def startup_event():
            asyncio.create_task(self._ws_push_loop())

        config = uvicorn.Config(
            app,
            host="0.0.0.0",
            port=self.state.api_port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._server = server

        # 开发模式: 启动文件监控线程, 变动时自动重启进程
        if dev_reload:
            watch_thread = threading.Thread(
                target=self._dev_file_watcher, daemon=True, name="dev-reload"
            )
            watch_thread.start()
            logger.info("🔁 开发模式已启用: lan_mesh/ 文件变动将自动重启")

        local_ips = self._collect_info().ip_addresses
        logger.info("服务已启动!")
        logger.info("  Web UI:  http://localhost:%d", self.state.api_port)
        for ip in local_ips:
            logger.info("  局域网:  http://%s:%d", ip, self.state.api_port)
        logger.info("Secretary 已就绪, 可直接通过聊天窗口下发任务")

        try:
            server.run()
        except KeyboardInterrupt:
            logger.info("正在停止...")
            self.stop()

    def stop(self):
        """停止 Station Director。"""
        self._running = False
        if self.workstation_optimizer:
            try:
                self.workstation_optimizer.stop_guardian()
            except Exception as exc:
                logger.warning("[Station] 工作站优化守护停止失败: %s", exc)
        if self.shadow_dev_manager:
            try:
                self.shadow_dev_manager.stop_guardian()
            except Exception as exc:
                logger.warning("[Station] 影子开发守护停止失败: %s", exc)
        if self.state.cloud_sync:
            self.state.cloud_sync.stop()
        if self.discovery:
            self.discovery.stop()

    def _dev_file_watcher(self):
        """开发模式: 监控 lan_mesh/ 目录文件变动, 检测到修改后自动重启进程。"""
        watch_dir = Path(__file__).parent
        extensions = {".py", ".html", ".yaml"}
        # 初始快照
        snapshot = {}
        for f in watch_dir.rglob("*"):
            if f.suffix in extensions and "__pycache__" not in str(f):
                try:
                    snapshot[str(f)] = f.stat().st_mtime
                except OSError:
                    pass
        logger.info("[dev-reload] 监控 %d 个文件 (%s)", len(snapshot), watch_dir)

        while self._running:
            time.sleep(1.5)
            changed = []
            for f in watch_dir.rglob("*"):
                if f.suffix not in extensions or "__pycache__" in str(f):
                    continue
                try:
                    mtime = f.stat().st_mtime
                except OSError:
                    continue
                old = snapshot.get(str(f))
                if old is None or mtime > old:
                    changed.append(f.name)
                    snapshot[str(f)] = mtime
            if changed:
                logger.info("🔁 [dev-reload] 检测到变动: %s → 重启中...", ", ".join(changed[:5]))
                time.sleep(0.5)  # 等待文件写入完成
                self._dev_restart()

    def _dev_restart(self):
        """重启进程: 优雅关闭 uvicorn 释放端口 → 等待端口释放 → 启动新进程 → 退出。

        M2 修复: 原实现直接 os._exit(0), 旧进程监听端口未释放时
        新进程 bind 失败 (Windows 上 uvicorn 端口占用报错)。
        """
        self._running = False  # 通知主循环停止
        # 请求 uvicorn 优雅退出 (释放监听 socket)
        if self._server:
            self._server.should_exit = True
        # 轮询等待端口释放 (最多 8s), 消除新进程绑定竞态
        deadline = time.time() + 8.0
        while time.time() < deadline:
            if not self._port_in_use(self.state.api_port):
                break
            time.sleep(0.2)
        import subprocess as _sp
        _sp.Popen([sys.executable] + sys.argv)
        logger.info("🔁 [dev-reload] 新进程已启动, 当前进程退出")
        time.sleep(0.3)  # 等待日志刷新
        os._exit(0)

    @staticmethod
    def _port_in_use(port: int) -> bool:
        """探测端口是否仍有进程监听 (重启前等待释放用)。"""
        import socket as _socket
        try:
            with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False
