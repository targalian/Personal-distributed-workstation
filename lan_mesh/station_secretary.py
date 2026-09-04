"""
Secretary 激活/停用与选举故障转移 — 模型资源预加载、让位、接管。

拆分自 station_controller.py (iter-75 Phase 4)。Secretary 是「管项目」的那一半
角色, 同进程按需加载 ChatHandler/PM/ModelRouter/MCPGateway。选举与故障转移
共用一套按 device_id 字典序的确定性仲裁规则 (E4/E5), 保证多节点并发时收敛。
"""
import os
import threading
import time
from pathlib import Path

from .config import load_model_pool
from .logger import get_logger

logger = get_logger("station")


class StationSecretaryMixin:
    """Secretary 生命周期与选举仲裁。

    依赖 StationController 提供的属性: cfg / state / db / director /
    secretary_active / chat_handler / project_manager / model_router 等。
    """

    # ── Secretary 激活/停用 ───────────────────────────────────────

    def _load_model_resources(self) -> None:
        """iter-55: 任何 station 模式都预加载模型资源池 (LLM Key 注入)。

        让位主机 (网络中已有 Secretary, 本机未激活) 作为远程派发
        Worker 执行 PM 任务时同样需要模型 Key 就绪 — 与激活解耦:
        - 加载 model_pool.yaml → self._model_pool (activate_secretary 复用)
        - 加载 resources.yaml → 注入直填 API Key 环境变量
        幂等, 重复调用无害; 异常 no-op 不影响启动。
        """
        try:
            from .config import load_model_pool
            from .model_resources import (init_resource_manager,
                                          set_bot_notify_global)
            if self._model_pool is None:
                self._model_pool = load_model_pool()
            pool = self._model_pool
            resources_path = self._find_resources_path()
            if resources_path:
                init_resource_manager(
                    resources_path,
                    pool.models if pool.models else None,
                    self.db,
                )
                if self.bot_gateway:
                    set_bot_notify_global(self.bot_gateway.notify)
        except Exception as e:
            logger.warning("模型资源预加载失败 (no-op): %s", e)

    def activate_secretary(self) -> dict:
        """同进程激活 Secretary 模式。

        PM Agent 架构演进后:
        - 不再创建 Orchestrator (由 PM Agent 接管任务分解/调度/分发)
        - 创建 ChatHandler (Web 端聊天窗口)
        - 保留 ModelRouter 和 MCPGateway (PM 需要)
        - 创建聊天专用 AgentRuntime (LLM 调用)
        """
        if self.secretary_active:
            return {"ok": True, "message": "Secretary 已激活", "already_active": True}

        peer = self._find_existing_secretary_host()
        if peer and peer.device_id < self.state.device_id:
            peer_name = peer.device_name or peer.device_id[:8]
            peer_url = f"http://{peer.ip}:{peer.api_port}"
            logger.warning(
                "[E4] 手动激活被拒绝: %s (%s) 仲裁优先, 本站保持 Station 模式",
                peer_name, peer_url)
            return {
                "ok": False,
                "message": (
                    f"Secretary 冲突: {peer_name} 仲裁优先, "
                    "本站保持 Station 模式; 请使用当前 Secretary"
                ),
                "conflict": True,
                "secretary_url": peer_url,
            }

        from .project import ProjectManager
        from .model_router import ModelRouter
        from .mcp_gateway import MCPGateway
        from .agent_runtime import AgentRuntime
        from .chat_handler import ChatHandler

        # 项目管理器
        self.project_manager = ProjectManager(self.db)

        # 模型路由器
        model_pool = self._model_pool or load_model_pool()
        self.model_router = ModelRouter(model_pool.models, self.project_manager) if model_pool.models else None
        self._default_model = model_pool.default_model  # 全局默认模型
        if self.model_router:
            model_info = f"{self.model_router.pool_size} 个模型"
            if self._default_model:
                model_info += f", 默认: {self._default_model}"
            logger.info("模型路由器已加载: %s", model_info)

        # R1: 模型资源管理 (resources.yaml 不存在时 no-op, 不影响功能)
        try:
            from .model_resources import (init_resource_manager,
                                          set_bot_notify_global)
            resources_path = self._find_resources_path()
            if resources_path:
                init_resource_manager(
                    resources_path,
                    model_pool.models if model_pool.models else None,
                    self.db,
                )
                # R7: 到期/额度预警推送到 Bot 通道
                set_bot_notify_global(self.bot_gateway.notify)
        except Exception as e:
            logger.warning("模型资源管理初始化失败 (no-op): %s", e)

        # iter-41: 任务停滞主动告警守护 (基于 task_flow 追踪, 异常不影响激活)
        # iter-43: 检查周期/阈值改由 config.yaml observability 段驱动 (≤0 禁用)
        try:
            from .runtime_trace import set_stall_bot_notify, start_stall_watcher
            if self.bot_gateway:
                set_stall_bot_notify(self.bot_gateway.notify)
            obs = self.cfg.observability
            start_stall_watcher(interval=obs.stall_check_interval,
                                stall_minutes=obs.stall_minutes)
        except Exception as e:
            logger.warning("任务停滞检测启动失败 (no-op): %s", e)

        # MCP 工具网关
        self.mcp_gateway = MCPGateway()

        # 聊天专用 AgentRuntime (用于秘书对话的 LLM 调用)
        self.chat_runtime = AgentRuntime(
            agent_id=f"secretary-{self.state.device_id[:8]}",
            shared_folder_path=str(self.state.shared_folder.path),
        )

        # 秘书聊天处理器 (多对话 + 共享文件夹持久化)
        self.chat_handler = ChatHandler(
            runtime=self.chat_runtime,
            controller=self,
            db=self.db,
            shared_folder=self.state.shared_folder,
        )

        # 优化15: 将 ChatHandler 注入 BotGateway, 统一消息入口
        if self.bot_gateway:
            self.bot_gateway.set_chat_handler(self.chat_handler)

        self.secretary_active = True
        # BUG-004 fix: 统一状态源 — 激活时同步设置 host_id/port
        self.secretary_host_id = self.state.device_id
        self.secretary_host_port = self.state.api_port
        logger.info("Secretary 模式已激活 — 聊天处理器/模型路由/MCP工具 已就绪 (PM Agent 架构)")
        # F1: 激活后立即与在线节点对齐密钥 (角色无关, 兜底启动同步时机差)
        try:
            threading.Thread(target=self._align_config_with_peers,
                             name="station-secret-align", daemon=True).start()
        except Exception as e:
            logger.warning("[F1] 激活后密钥对齐启动失败: %s", e)

        # 任务断点恢复: 将上次运行中断的任务标记为 interrupted
        self._recover_stale_tasks()

        return {
            "ok": True,
            "message": "Secretary 已激活",
            "models": self.model_router.pool_size if self.model_router else 0,
        }

    def _find_resources_path(self) -> str:
        """查找 resources.yaml (与 model_pool.yaml 同目录约定)。"""
        candidates = []
        env_path = os.environ.get("LAN_MESH_RESOURCES")
        if env_path:
            candidates.append(Path(env_path))
        candidates.append(Path(__file__).parent / "resources.yaml")
        candidates.append(Path("resources.yaml"))
        for c in candidates:
            try:
                if c.is_file():
                    return str(c)
            except Exception:
                continue
        return ""

    def deactivate_secretary(self) -> dict:
        """停用 Secretary 模式, 回到纯 Station 模式。"""
        if not self.secretary_active:
            return {"ok": True, "message": "Secretary 未激活"}

        self.secretary_active = False
        # BUG-004 fix: 统一状态源 — 停用时同步清除 host_id/port
        self.secretary_host_id = None
        self.secretary_host_port = None
        self.project_manager = None
        self.model_router = None
        self.mcp_gateway = None
        self.chat_handler = None
        self.chat_runtime = None

        # 优化15: 清除 BotGateway 的 ChatHandler 引用
        if self.bot_gateway:
            self.bot_gateway.set_chat_handler(None)

        logger.info("Secretary 模式已停用 — 回到纯基础设施管理")
        return {"ok": True, "message": "Secretary 已停用"}

    def _recover_stale_tasks(self):
        """iter-53: 启动时恢复中断任务 — 有快照自动续跑, 无快照标 interrupted。

        系统重启后, 之前处于 running/monitoring/planning/executing 的任务
        其 PM Agent 进程已不存在:
        - 存在执行态快照 (pm_snapshots) → 自动重建 PM 从断点续跑
          (保留已完成子任务输出, 重新分发未完成部分)
        - 无快照 → 标记 interrupted, 用户可在 UI 中看到并决定重新提交或删除
        """
        stale_statuses = ("running", "monitoring", "planning", "executing", "awaiting_input", "paused")
        recovered = 0
        for status in stale_statuses:
            try:
                tasks = self.db.list_tasks(status=status, limit=100)
                for task in tasks:
                    # iter-53: 有快照 → 自动续跑
                    if self.db.get_pm_snapshot_by_task(task.task_id):
                        result = self._local_resume_pm(task.task_id)
                        if result.get("ok"):
                            logger.info("任务断点恢复: %s 已从快照续跑 (pm=%s)",
                                        task.task_id[:16], result["pm_id"][:12])
                            recovered += 1
                            continue
                        logger.warning("任务断点恢复失败: %s → %s",
                                       task.task_id[:16], result.get("message"))
                    task.status = "interrupted"
                    if not task.output_data:
                        task.output_data = {}
                    task.output_data["_interrupted"] = {
                        "reason": "系统重启, PM Agent 进程已丢失",
                        "original_status": status,
                        "interrupted_at": time.time(),
                    }
                    self.db.save_task(task)
                    recovered += 1
            except Exception as e:
                logger.warning("恢复中断任务失败 (status=%s): %s", status, e)
        if recovered:
            logger.info("任务断点恢复: %d 个中断任务已处理 (快照续跑或标记 interrupted)", recovered)

    # ── Secretary 自动选举 ─────────────────────────────────────

    def _secretary_election(self):
        """First-Station-Wins 选举: 等待发现窗口, 判断是否由本站担任 Secretary。"""
        logger.info("Secretary 选举: 等待 5s 发现窗口...")
        time.sleep(5)  # 等待 UDP 发现收集网络中已有主机

        existing = self._find_existing_secretary()
        if not existing:
            try:
                self.activate_secretary()
                logger.info("Secretary 选举: 本站当选 (网络中无其他 Secretary)")
                # 通知前端 Secretary 已激活
                self._queue_ws_broadcast("secretary_activated", {
                    "message": "Secretary 自动选举激活",
                    "device_id": self.state.device_id,
                })
            except Exception as e:
                logger.error("Secretary 自动激活失败: %s (可在 Web UI 手动激活)", e)
        else:
            logger.info("Secretary 选举: 网络中已有 Secretary [%s], 本站保持 Station 模式", existing)
            self._converge_mesh_token()

    def _converge_mesh_token(self, target_ip: str = "", target_port: int = 0):
        """S1: 从 Secretary 拉取 mesh token 收敛加密信任根。

        Station 间无注册链路 (注册仅 Worker 用), 非 Secretary 节点
        在此处引导拉取并持久化; 与注册下发同一信任假设 (LAN 成员)。
        E4: 支持直接传入目标 (冲突仲裁时 DB 角色可能尚未刷新)。
        """
        from .auth import save_mesh_token
        from .http_retry import http_get, set_auth_token
        ip, port = (target_ip or "").strip(), int(target_port or 0)
        if not ip or not port:
            for h in self.db.list_hosts():
                if (getattr(h, "device_id", "") != self.state.device_id
                        and getattr(h, "role", "") == "secretary"
                        and getattr(h, "online", False)
                        and getattr(h, "ip", "") and getattr(h, "api_port", 0)):
                    ip, port = h.ip, h.api_port
                    break
        if not ip or not port:
            return
        try:
            resp = http_get(
                f"http://{ip}:{port}"
                "/api/station/bootstrap-token", timeout=10)
            token = (resp.json() or {}).get("mesh_token", "")
            if token and token != self._mesh_token:
                if save_mesh_token(token):
                    self._mesh_token = token
                    set_auth_token(token if self._mesh_auth_enabled else "")
                    logger.info("[S1] mesh token 已收敛 (拉取自 Secretary %s)", ip)
        except Exception as e:
            logger.warning("[S1] mesh token 收敛失败: %s", e)

    def _yield_secretary_to(self, peer_name: str, ip: str, port: int):
        """E4: 向仲裁优先级更高 (device_id 更小) 的节点让位 Secretary。

        选举 5s 窗口丢包/时机错开会致双 Secretary 脑裂 (各自生成
        mesh_token, 密钥互推解密失败)。双端对称规则: device_id
        字典序较大者降级。让位后收敛 token 并拉取资源密钥,
        保证加密信任根与配置全网一致。
        """
        if getattr(self, "_secretary_yielded", False):
            return
        self._secretary_yielded = True
        logger.warning("[E4] Secretary 冲突: %s (%s) 仲裁优先, 本站让位为 Station 模式",
                       peer_name, ip)
        try:
            self.deactivate_secretary()
        except Exception as e:
            logger.error("[E4] Secretary 让位失败: %s", e)
            return
        try:
            self._queue_ws_broadcast("secretary_yielded", {
                "message": "Secretary 冲突仲裁: 本站已让位, %s 接管" % peer_name,
                "peer": peer_name,
            })
        except Exception:
            pass

        def _converge_after_yield():
            time.sleep(2)  # 等对端服务稳定
            try:
                self._converge_mesh_token(target_ip=ip, target_port=port)
            except Exception as e:
                logger.warning("[E4] 让位后 token 收敛失败: %s", e)
            try:
                self.pull_resource_secrets(ip, port)
            except Exception as e:
                logger.warning("[E4] 让位后密钥拉取失败: %s", e)

        threading.Thread(target=_converge_after_yield,
                         name="station-yield-converge", daemon=True).start()

    def _find_existing_secretary(self) -> str:
        """查找网络中已存在的在线 Secretary。返回设备名或空字符串。

        F3.4 (iter-64): 仅考虑本网段主机 (source=lan) — 联邦远端
        Secretary 不参与本网段仲裁, 各网段 Secretary 联邦共存。
        """
        hosts = self.db.list_hosts(source="lan")
        for h in hosts:
            if (getattr(h, 'device_id', '') != self.state.device_id
                    and getattr(h, 'role', '') == 'secretary'
                    and getattr(h, 'online', False)):
                return getattr(h, 'device_name', h.device_id[:8])
        return ""

    def _find_existing_secretary_host(self):
        """返回本网段当前在线 Secretary 主机, 无则返回 None。"""
        hosts = self.db.list_hosts(source="lan")
        candidates = [
            h for h in hosts
            if (getattr(h, 'device_id', '') != self.state.device_id
                and getattr(h, 'role', '') == 'secretary'
                and getattr(h, 'online', False))
        ]
        return min(candidates, key=lambda h: h.device_id) if candidates else None

    # ── E5: Secretary Failover ─────────────────────────────────

    def _secretary_failover_check(self):
        """E5: Secretary 离线接管检查 (由 _prune_loop 每轮调用)。

        选举只在启动时进行 (First-Station-Wins), Secretary 宕机后无人接管。
        本检查在 Secretary 超时离线且网络无其他在线 Secretary 时,
        由 device_id 字典序最小的在线 Station 接任 — 与 E4 同一对称
        仲裁规则, 多节点并发接管亦自然收敛 (双 Secretary 出现时
        _on_device_seen 的让位逻辑兼容裁决)。
        """
        if self.secretary_active:
            return
        # F3.4 (iter-64): 仅本网段主机参与仲裁 (联邦远端 Secretary 隔离)
        hosts = self.db.list_hosts(source="lan")
        for h in hosts:
            if (getattr(h, "device_id", "") != self.state.device_id
                    and getattr(h, "role", "") == "secretary"
                    and getattr(h, "online", False)):
                return  # 仍有在线 Secretary, 无需接管
        # 候选集 = 本站 + 全部在线非 Secretary 主机
        candidate_ids = [self.state.device_id]
        for h in hosts:
            if (getattr(h, "device_id", "") != self.state.device_id
                    and getattr(h, "online", False)
                    and getattr(h, "role", "") != "secretary"):
                candidate_ids.append(h.device_id)
        if min(candidate_ids) != self.state.device_id:
            return  # 存在仲裁优先级更高 (device_id 更小) 的在线 Station
        try:
            self.activate_secretary()
        except Exception as e:
            logger.error("[E5] Secretary failover 激活失败: %s", e)
            return
        logger.warning("[E5] Secretary failover: 原 Secretary 离线, 本站接管 "
                       "(device_id 仲裁最小)")
        try:
            self._queue_ws_broadcast("secretary_failover", {
                "message": "Secretary 离线故障转移: 本站接管",
                "device_id": self.state.device_id,
            })
        except Exception:
            pass
        try:
            self.bot_gateway.notify("secretary_failover", {
                "device": self.state.device_name,
            })
        except Exception:
            pass
