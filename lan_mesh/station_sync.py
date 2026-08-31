"""
集群同步域 — 密钥/资源/配置/版本四类同步与对齐。

拆分自 station_controller.py (iter-74 Phase 2)。覆盖 S1 API Key 加密分发
(push/pull)、S3 启动时一次性同步、R4 配置多数派对齐、F1 版本领导者比对与
自动升级。所有跨节点调用均包裹超时与异常隔离, 失败不阻断主流程。
"""
import sys
import threading
import time
from pathlib import Path

from .config import load_model_pool
from .http_retry import http_post
from .logger import get_logger

logger = get_logger("station")


class StationSyncMixin:
    """密钥/配置/版本同步与对齐。

    依赖 StationController 提供的属性: cfg / state / db / director /
    _running / _config_hash 等。
    """

    def _startup_sync_once(self):
        """S3: 启动时一次性同步 (替代 60s 轮询) — 版本比对 + API Key 同步。

        等发现层可见对端后执行一次:
        1. 版本领先检测 (领先则通知落后节点, 落后则提醒自身升级)
        2. 密钥同步: 本机是 Secretary 则推, 否则从 Secretary 拉取
        """
        from .version_sync import local_version_info
        peers: list = []
        deadline = time.time() + 60
        while self._running and time.time() < deadline:
            time.sleep(5)
            try:
                peers = [
                    d for d in (self.discovery.list_devices()
                                if self.discovery else [])
                    if d.get("online") and d.get("device_id") != self.state.device_id
                ]
            except Exception:
                peers = []
            if not peers:
                # 回退: 发现层尚无对端时查 DB 既有在线主机记录
                try:
                    peers = [
                        {"device_id": h.device_id, "role": h.role,
                         "ip": h.ip, "api_port": h.api_port,
                         "code_version": h.code_version,
                         "version_ts": h.version_ts}
                        for h in self.db.list_hosts()
                        if h.online and h.device_id != self.state.device_id
                        and h.ip and h.api_port
                    ]
                except Exception:
                    peers = []
            if peers:
                break
        if not peers or not self._running:
            logger.info("[S3] 启动同步: 未发现对端节点, 跳过")
            return
        # 等 Secretary 选举完成 (最长 30s), 确保密钥同步方向判定准确
        wait_until = time.time() + 30
        while (self._running and time.time() < wait_until
               and not self.secretary_active):
            time.sleep(1)
        try:
            self_ver = local_version_info()
            if self_ver.get("commit"):
                vpeers = [p for p in peers if p.get("code_version")]
                if vpeers:
                    self._check_version_leadership(self_ver, vpeers)
        except Exception as e:
            logger.warning("[S3] 启动版本比对异常: %s", e)
        try:
            self._startup_key_sync(peers)
        except Exception as e:
            logger.warning("[S3] 启动密钥同步异常: %s", e)

    def _startup_key_sync(self, peers: list):
        """F1: 启动密钥对齐 — 角色无关, 与在线对端按 config_ts 自动收敛。

        不再依赖 Secretary/Station 主从方向: 谁新谁胜,
        任意节点启动都会与对端对齐 (推或拉由仲裁结果决定)。
        """
        summary = self._align_config_with_peers(peers)
        total = len(summary["pushed"]) + len(summary["pulled"])
        if total or summary["failed"]:
            logger.info("[F1] 启动密钥对齐: 推 %d / 拉 %d / 失败 %d",
                        len(summary["pushed"]), len(summary["pulled"]),
                        len(summary["failed"]))

    def _align_config_with_peers(self, peers: list = None) -> dict:
        """F1: 角色无关密钥对齐 — 与主从无关, 内容不一致时自动收敛。

        仲裁规则 (与 Secretary/Station 角色无关):
        - 内容指纹一致 (config_hash 排除 config_ts) → 跳过
        - config_ts 新者胜: 本机新 → 推送; 对端新 → 拉取
        - ts 缺失/相等 → 资源池数多者胜; 仍相同 → 跳过告警

        Returns:
            {"pushed": [...], "pulled": [...], "skipped": n, "failed": [...]}
        """
        from pathlib import Path
        from .http_retry import http_get
        from . import model_resources as model_resources
        from .secret_sync import config_hash

        target = Path(__file__).parent / "resources.yaml"
        cfg = model_resources.read_config_data(target)
        mine = cfg.get("data") or {}
        mine_hash = config_hash(mine) if mine else ""
        try:
            mine_ts = float(mine.get("config_ts") or 0)
        except (TypeError, ValueError):
            mine_ts = 0.0
        mine_pools = len(mine.get("resources") or [])

        summary = {"pushed": [], "pulled": [], "skipped": 0, "failed": []}
        if peers is None:
            peers = [
                {"device_id": h.device_id,
                 "device_name": getattr(h, "device_name", "") or h.hostname,
                 "ip": getattr(h, "ip", "") or "",
                 "api_port": getattr(h, "api_port", 0)}
                for h in self.db.list_hosts()
                if h.online and h.device_id != self.state.device_id
                and getattr(h, "ip", "") and getattr(h, "api_port", 0)
            ]
        for p in peers:
            ip = (p.get("ip") or "").strip()
            try:
                port = int(p.get("api_port") or 0)
            except (TypeError, ValueError):
                port = 0
            name = p.get("device_name") or str(p.get("device_id", ""))[:8]
            if not ip or not port:
                continue
            try:
                resp = http_get(f"http://{ip}:{port}/api/secrets/fetch", timeout=10)
                payload = resp.json() or {}
            except Exception as e:
                summary["failed"].append({"peer": name, "detail": f"探测失败: {e}"})
                continue
            peer_hash = (payload.get("config_hash") or "").strip()
            if not peer_hash or not payload.get("blob"):
                summary["skipped"] += 1  # 对端无可用密钥配置
                continue
            if peer_hash == mine_hash:
                summary["skipped"] += 1  # 内容一致
                continue
            try:
                peer_ts = float(payload.get("config_ts") or 0)
            except (TypeError, ValueError):
                peer_ts = 0.0
            try:
                peer_pools = int(payload.get("pools") or 0)
            except (TypeError, ValueError):
                peer_pools = 0
            if mine_ts and peer_ts:
                if mine_ts > peer_ts:
                    action = "push"
                elif mine_ts < peer_ts:
                    action = "pull"
                else:
                    action = ("push" if mine_pools > peer_pools
                              else "pull" if mine_pools < peer_pools else "")
            else:
                # ts 缺失视为旧配置; 双方都无仲裁依据时按规模收敛
                if mine_ts:
                    action = "push"
                elif peer_ts:
                    action = "pull"
                else:
                    action = ("push" if mine_pools > peer_pools
                              else "pull" if mine_pools < peer_pools else "")
            if not action:
                logger.warning("[F1] 与 %s 配置不一致但无仲裁依据 "
                               "(ts 相同且池数相同), 跳过", name)
                summary["failed"].append({"peer": name, "detail": "无仲裁依据"})
                continue
            try:
                if action == "push":
                    res = self.push_resource_secrets(
                        only_device_id=p.get("device_id", ""),
                        fallback_ip=ip, fallback_port=port)
                    ok = any(r.get("ok") for r in res)
                    detail = (res[0].get("detail", "") if res else "无结果")
                    summary["pushed"].append({"peer": name, "ok": ok,
                                              "detail": detail})
                else:
                    res = self.pull_resource_secrets(ip, port)
                    summary["pulled"].append({"peer": name,
                                              "ok": bool(res.get("ok")),
                                              "detail": res.get("detail", "")})
            except Exception as e:
                summary["failed"].append(
                    {"peer": name, "detail": f"{action} 失败: {e}"})
        if summary["pushed"] or summary["pulled"] or summary["failed"]:
            logger.info("[F1] 密钥对齐: 推 %d / 拉 %d / 跳过 %d / 失败 %d",
                        len(summary["pushed"]), len(summary["pulled"]),
                        summary["skipped"], len(summary["failed"]))
        return summary

    def _align_loop(self):
        """F1: 周期角色无关对齐 — 任意节点每 60s 与在线对端收敛密钥配置。

        内容一致时静默跳过 (不刷日志); 不一致时自动推/拉并落盘。
        """
        time.sleep(30)  # 先让选举/让位稳定
        while self._running:
            try:
                self._align_config_with_peers()
            except Exception as e:
                logger.debug("[F1] 周期对齐异常: %s", e)
            time.sleep(self._align_interval)

    def pull_resource_secrets(self, ip: str, port: int) -> dict:
        """S3: 从指定节点拉取加密资源配置 (含 API Key) 并应用。

        解密用本机 mesh_token; 指纹校验 + 幂等跳过 + validate 后落盘热重载。
        """
        from pathlib import Path
        from .auth import get_mesh_token
        from .config import load_model_pool
        from .model_resources import (init_resource_manager, read_config_data,
                                      save_config, set_bot_notify_global,
                                      validate_config)
        from .secret_sync import config_hash, decrypt_config
        from .http_retry import http_get
        resp = http_get(f"http://{ip}:{port}/api/secrets/fetch", timeout=15)
        payload = resp.json()
        if not payload.get("blob"):
            return {"ok": False, "applied": False,
                    "detail": payload.get("detail", "对端无配置")}
        token = getattr(self, "_mesh_auth_token", None) or get_mesh_token()
        try:
            data = decrypt_config(payload, token)
        except (ValueError, RuntimeError) as e:
            # S1 自愈: 加密信任根分歧 (历史双 Secretary 脑裂 / token
            # 文件重建) 时, 先向目标 Secretary 收敛 mesh_token 再重试
            if "mesh_token 不匹配" in str(e):
                try:
                    self._converge_mesh_token(target_ip=ip, target_port=port)
                    token = get_mesh_token()
                    data = decrypt_config(payload, token)
                    logger.info("[S1] mesh_token 收敛后密钥解密重试成功 (来自 %s)", ip)
                except Exception as retry_err:
                    return {"ok": False, "applied": False,
                            "detail": f"解密失败: {retry_err}"}
            else:
                return {"ok": False, "applied": False, "detail": f"解密失败: {e}"}
        expected = (payload.get("config_hash") or "").strip()
        if expected and config_hash(data) != expected:
            return {"ok": False, "applied": False, "detail": "配置指纹不匹配"}
        if not isinstance(data, dict):
            return {"ok": False, "applied": False, "detail": "解密内容非配置对象"}
        target = Path(__file__).parent / "resources.yaml"
        current = read_config_data(target) if target.is_file() else {}
        cur_data = current.get("data") or {}
        if cur_data and config_hash(cur_data) == config_hash(data):
            return {"ok": True, "applied": False, "detail": "配置一致"}
        errors = validate_config(data)
        if errors:
            return {"ok": False, "applied": False,
                    "detail": "校验失败: " + "; ".join(errors)}
        saved = save_config(target, data)
        if not saved.get("ok"):
            return {"ok": False, "applied": False,
                    "detail": f"保存失败: {saved.get('error')}"}
        pool = load_model_pool()
        mgr = init_resource_manager(
            target, pool.models if pool.models else None, self.db)
        set_bot_notify_global(self.bot_gateway.notify)
        from .event_bus import publish_event
        publish_event("resource_config",
                      {"ok": True, "pools": len(mgr.list_resources()),
                       "source": "startup-sync"})
        logger.info("[S3] 已从 %s 拉取密钥并应用 %d 个资源池",
                    ip, len(mgr.list_resources()))
        return {"ok": True, "applied": True, "detail": "已应用",
                "pools": len(mgr.list_resources())}

    def _sync_with_new_peer(self, device_id: str, ip: str, port: int,
                            role: str, code_version: str, version_ts: float):
        """S3: 新主机入网即时同步 (免轮询) — 密钥与版本一次性对齐。

        1. 密钥: 本机是 Secretary 则向新节点推送; 新节点是 Secretary 则向其拉取
        2. 版本: 本机领先则通知新节点升级, 落后则提醒自身
        """
        time.sleep(2)  # 等待对端 API 就绪
        try:
            # F1: 角色无关密钥对齐 (推/拉由 config_ts 仲裁决定)
            self._align_config_with_peers([{
                "device_id": device_id,
                "ip": ip, "api_port": port,
                "device_name": "", "role": role,
            }])
        except Exception as e:
            logger.warning("[F1] 新主机密钥对齐异常: %s", e)
        try:
            if code_version and ip and port:
                from .version_sync import local_version_info
                self_ver = local_version_info()
                if self_ver.get("commit"):
                    self._check_version_leadership(self_ver, [{
                        "device_id": device_id,
                        "code_version": code_version,
                        "version_ts": version_ts,
                        "ip": ip,
                        "api_port": port,
                    }])
        except Exception as e:
            logger.warning("[S3] 新主机版本比对异常: %s", e)

    def _check_version_leadership(self, self_ver: dict, peers: list):
        """S2: 版本领先检测 — 本机领先全网则通知落后节点, 落后则提醒自身升级。

        通知内容仅为升级建议 (git pull + 重启), 不远程操控目标节点。
        """
        from .version_sync import compare_versions, find_leader
        from .http_retry import http_post

        versions = [{"device_id": self.state.device_id,
                     "commit": self_ver["commit"],
                     "commit_time": self_ver.get("commit_time", 0.0)}]
        for p in peers:
            versions.append({"device_id": p["device_id"],
                             "device_name": p.get("device_name", ""),
                             "commit": p.get("code_version", ""),
                             "commit_time": p.get("version_ts", 0.0),
                             "ip": p.get("ip", ""),
                             "api_port": p.get("api_port", 0)})
        leader = find_leader(versions)
        if not leader:
            return

        if leader["device_id"] == self.state.device_id:
            # 本机严格领先: 通知每个落后节点 (同版本只通知一次)
            for p in versions[1:]:
                if compare_versions(self_ver, p) != "ahead":
                    continue
                if not self._upgrade_notifier.should_notify(
                        p["device_id"], self_ver["commit"]):
                    continue
                ip, port = p.get("ip", ""), p.get("api_port", 0)
                if not ip or not port:
                    continue
                try:
                    http_post(
                        f"http://{ip}:{port}/api/version/upgrade-notice",
                        json={
                            "from_device_id": self.state.device_id,
                            "from_name": self.state.device_name,
                            "commit": self_ver["commit"],
                            "version": self_ver.get("version", ""),
                            "note": self_ver.get("note", ""),
                            "upgrade_hint": self_ver.get("upgrade_hint", ""),
                        }, timeout=10)
                    logger.info("[S2] 已通知 %s (%s) 升级: 本机 %s 领先于 %s",
                                p.get("device_name") or p["device_id"][:8], ip,
                                self_ver["commit"], p["commit"])
                except Exception as e:
                    logger.warning("[S2] 通知 %s 升级失败: %s", ip, e)
        else:
            # 他人领先: 提醒本机升级 (同一领先版本只提醒一次)
            lead_commit = leader.get("commit", "")
            if lead_commit in self._version_behind_warned:
                return
            self._version_behind_warned.add(lead_commit)
            lead_name = leader.get("device_name") or lead_commit[:8]
            logger.warning("[S2] 检测到 %s 版本领先 (%s), 建议本机 git pull 升级后重启",
                           lead_name, lead_commit)
            # F1: 角色无关版本对齐 — 落后节点自动 git pull + 依赖安装
            self._auto_upgrade(lead_commit, lead_name)
            from .event_bus import publish_event
            publish_event("version_upgrade_notice", {
                "behind": True,
                "from_name": lead_name,
                "commit": lead_commit,
                "hint": "git pull 升级后重启节点",
            })

    def _auto_upgrade(self, leader_commit: str, leader_name: str = ""):
        """F1: 版本落后自动对齐 — git pull + 依赖安装, 与主从无关。

        代码更新后由 dev-reload 自动重启 (未开 dev 模式则提示手动重启)。

        安全边界:
        - 工作区脏 (未提交改动) → 跳过并告警, 绝不覆盖本地改动
        - 同一领先 commit 仅自动尝试一次 (失败转人工)
        - config.yaml auto_upgrade: false 可整体关闭
        """
        if not self.auto_upgrade_enabled:
            return
        if not leader_commit or leader_commit in self._upgrade_attempted:
            return
        self._upgrade_attempted.add(leader_commit)

        def _run():
            import subprocess
            root = Path(__file__).resolve().parent.parent
            try:
                status = subprocess.run(
                    ["git", "status", "--porcelain"], cwd=root,
                    capture_output=True, text=True, timeout=15)
                if status.stdout.strip():
                    logger.warning("[F1] 自动升级跳过: 工作区有未提交改动 "
                                   "(领先者 %s @ %s)", leader_name, leader_commit)
                    return
                logger.info("[F1] 版本落后, 自动升级: git pull "
                            "(领先者 %s @ %s)", leader_name, leader_commit)
                pull = subprocess.run(
                    ["git", "pull", "--ff-only"], cwd=root,
                    capture_output=True, text=True, timeout=120)
                if pull.returncode != 0:
                    logger.warning("[F1] 自动升级失败 (git pull): %s",
                                   (pull.stderr or pull.stdout).strip()[-300:])
                    return
                deps = subprocess.run(
                    [sys.executable, "-m", "pip", "install", "-q",
                     "-r", "requirements.txt"], cwd=root,
                    capture_output=True, text=True, timeout=600)
                if deps.returncode != 0:
                    logger.warning("[F1] 依赖安装失败 (代码已更新): %s",
                                   (deps.stderr or deps.stdout).strip()[-300:])
                    return
                logger.info("[F1] 自动升级完成: git pull 成功 → "
                            "等待重启加载新代码")
                from .event_bus import publish_event
                publish_event("version_upgrade_notice", {
                    "behind": False, "auto_upgraded": True,
                    "commit": leader_commit, "from_name": leader_name,
                    "hint": "代码已更新, dev 模式自动重启或手动重启节点",
                })
            except Exception as e:
                logger.warning("[F1] 自动升级异常: %s", e)

        threading.Thread(target=_run, name="auto-upgrade",
                         daemon=True).start()

    def push_resource_secrets(self, only_device_id: str = "",
                              fallback_ip: str = "",
                              fallback_port: int = 0) -> list:
        """S1: 将本机资源配置 (含 api_key 直填) 加密推送到在线节点。

        信任根为 mesh_token (HKDF 派生 AES-256-GCM 密钥), 与认证
        开关解耦; token 缺失时拒绝推送 (绝不降级明文)。

        Args:
            only_device_id: 仅推指定节点 (空 = 本机外全部在线节点)
            fallback_ip/fallback_port: 目标主机记录缺 ip/port 时的
                回退地址 (注册即推场景, 发现信息尚未入库)

        Returns:
            [{"device_id", "device_name", "ok", "detail"}]
        """
        if not self._mesh_token:
            return [{"ok": False,
                     "detail": "无 mesh_token (加密信任根缺失), 拒绝推送"}]

        from .http_retry import http_post
        from .model_resources import read_config_data
        from .secret_sync import config_hash, encrypt_config
        target = Path(__file__).parent / "resources.yaml"
        if not target.is_file():
            return [{"ok": False, "detail": "本机无 resources.yaml, 无密钥可分发"}]
        cfg = read_config_data(target)
        data = cfg.get("data") or {}
        pools = data.get("resources") or []
        key_count = sum(1 for p in pools if (p.get("api_key") or "").strip())
        if not key_count:
            return [{"ok": False, "detail": "无资源池配置 api_key 直填值, 无需推送"}]

        hosts = self.db.list_hosts()
        targets = [
            h for h in hosts
            if h.device_id != self.state.device_id
            and (h.online or h.device_id == only_device_id)
            and (not only_device_id or h.device_id == only_device_id)
        ]
        if not targets:
            return [{"ok": False, "detail": "无其他在线节点"}]

        results = []
        cfg_hash = config_hash(data)
        for h in targets:
            ip = h.ip or (fallback_ip if h.device_id == only_device_id else "")
            port = h.api_port or (fallback_port if h.device_id == only_device_id else 0)
            item = {"device_id": h.device_id,
                    "device_name": h.device_name or h.hostname}
            if not ip or not port:
                item["ok"] = False
                item["detail"] = "目标地址不完整"
                results.append(item)
                continue
            try:
                payload = encrypt_config(data, self._mesh_token)
                payload["config_hash"] = cfg_hash
                # S1 自愈: 附带本机端口, 对端解密失败时据此收敛信任根
                payload["src_port"] = self.state.api_port
                resp = http_post(
                    f"http://{ip}:{port}/api/secrets/receive",
                    json=payload,
                    timeout=15,
                )
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                item["ok"] = resp.status_code == 200
                if resp.status_code == 200 and body.get("applied"):
                    item["detail"] = f"已应用 ({body.get('pools', 0)} 池)"
                elif resp.status_code == 200:
                    item["detail"] = "配置一致, 已跳过"
                else:
                    item["detail"] = (body.get("detail")
                                      or f"HTTP {resp.status_code}")
            except Exception as e:
                item["ok"] = False
                item["detail"] = f"推送失败: {e}"
            results.append(item)
            logger.info("[S1] 密钥推送 %s (%s): %s — %s",
                        item.get("device_name"), ip,
                        "成功" if item["ok"] else "失败", item["detail"])
        return results
