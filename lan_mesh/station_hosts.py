"""
主机与发现域 — 发现回调、主机信息采集、配置刷新下发、日志修剪、Bot 通道。

拆分自 station_controller.py (iter-74 Phase 2)。Station Director「只管机器」
的那一半: UDP 发现包构造与设备上线回调 (含离线清理触发 PM 孤儿迁移)、
配置脚本下发与周期刷新、日志修剪守护、Bot 通道配置与命令分派。
"""
import shutil
import threading
import time
from pathlib import Path

from .bot_gateway import BotChannel
from .host_info import collect_host_info, make_discovery_packet
from .logger import get_logger
from .protocol import (
    DiscoveryPacket,
    HEARTBEAT_INTERVAL_SECS,
    HostInfo,
    PRUNE_INTERVAL_SECS,
)

logger = get_logger("station")


class StationHostsMixin:
    """发现/主机/配置刷新/日志修剪/Bot 通道。

    依赖 StationController 提供的属性: cfg / state / db / director /
    bot_gateway / discovery / _running 等。
    """

    def _load_bot_config(self):
        """从 AppConfig 加载 Bot 通道配置。"""
        bot_cfg = self.cfg.bot
        for ch_cfg in bot_cfg.channels:
            channel = BotChannel(
                channel_type=ch_cfg.channel_type,
                enabled=ch_cfg.enabled,
                webhook_url=ch_cfg.webhook_url,
                bot_token=ch_cfg.bot_token,
                chat_id=ch_cfg.chat_id,
                webhook_url_base=ch_cfg.webhook_url_base,
                min_priority=ch_cfg.min_priority,
            )
            self.bot_gateway.add_channel(channel)
        # 设置命令处理回调
        self.bot_gateway.set_command_handler(self._on_bot_command)
        enabled_count = sum(1 for c in bot_cfg.channels if c.enabled)
        if enabled_count:
            logger.info("Bot 通道已加载: %d 个启用", enabled_count)

    def _on_bot_command(self, command: str, args: str, chat_id: str) -> str:
        """处理来自 Telegram 的命令。"""
        if command == "status":
            hosts = self.db.list_hosts()
            online = [h for h in hosts if h.online]
            tasks = self.db.list_tasks(limit=5) if self.secretary_active else []
            return (
                f"📊 工作站状态\n"
                f"在线主机: {len(online)}/{len(hosts)}\n"
                f"Secretary: {'激活' if self.secretary_active else '未激活'}\n"
                f"最近任务: {len(tasks)} 个"
            )
        if command == "hosts":
            hosts = self.db.list_hosts()
            lines = ["🖥️ 主机列表:"]
            for h in hosts:
                status = "✅" if h.online else "❌"
                lines.append(f"{status} {h.device_name or h.hostname or '未知'} ({h.ip or '-'})")
            return "\n".join(lines) if len(lines) > 1 else "暂无主机"
        if command == "tasks":
            if not self.secretary_active:
                return "Secretary 未激活"
            tasks = self.db.list_tasks(limit=5)
            if not tasks:
                return "暂无任务"
            lines = ["📋 最近任务:"]
            for t in tasks:
                icon = {"completed": "✅", "failed": "❌", "running": "🔄", "pending": "⏳"}.get(t.status, "❓")
                lines.append(f"{icon} {t.name} [{t.status}]")
            return "\n".join(lines)
        return f"未知命令: /{command}\n可用: /status /hosts /tasks /help"

    def _collect_info(self) -> HostInfo:
        """采集 Station Director 自身的主机信息。"""
        role = "secretary" if self.secretary_active else "station"
        return collect_host_info(
            device_id=self.state.device_id,
            device_name=self.state.device_name,
            role=role,
            api_port=self.state.api_port,
            shared_folder=str(self.state.shared_folder.path),
            shared_file_count=self.state.shared_folder.file_count(),
            start_time=self.state.start_time,
        )

    def _make_packet(self) -> DiscoveryPacket:
        """生成 Station 的 UDP 发现包。"""
        info = self._collect_info()
        packet = make_discovery_packet(info)
        # E4: 广播真实角色 (此前固定 station, 导致对端永远无法经 UDP
        # 感知 Secretary 身份, 冲突仲裁与选举避让全部失效)
        packet.role = "secretary" if self.secretary_active else "station"
        return packet

    def _on_device_seen(self, packet: DiscoveryPacket, ip: str):
        """UDP 发现到设备时: 首次自动注册入 DB, 后续更新 last_seen (轻量心跳)。

        UDP presence 包每 3 秒到达一次, 证明对方存活。
        对于已注册主机, 利用 UDP 包更新 last_seen + IP, 避免被 prune_offline 误判离线。
        这对 Station 间互相发现尤为重要 (Station 间无 HTTP 心跳通道)。
        """
        if not packet.device_id or packet.device_id == self.state.device_id:
            return

        # E4: Secretary 冲突仲裁 — 选举时机错开致双 Secretary 时,
        # 按 device_id 字典序确定性让位 (较大者降级为 Station),
        # 双端对称规则保证全网收敛到同一 Secretary
        if (packet.role == "secretary" and self.secretary_active
                and packet.device_id < self.state.device_id):
            self._yield_secretary_to(packet.device_name, ip, packet.api_port)

        existing = self.db.get_host(packet.device_id)
        if existing:
            # 已注册: 仅更新 last_seen + IP + 实时指标 (轻量心跳)
            try:
                self.station_director.on_heartbeat(packet.device_id, {
                    "cpu_percent": packet.cpu_percent,
                    "memory_percent": packet.memory_percent,
                    "disk_percent": packet.disk_percent,
                    "ip": ip,
                    "code_version": packet.code_version,
                    "version_ts": packet.version_ts,
                    # E4: 携带真实角色, 修复 DB role 陈旧致选举误判
                    "role": packet.role,
                })
            except Exception:
                pass
            return

        # 首次发现: 从 UDP 包构造 HostInfo 并自动入站
        try:
            info = HostInfo(
                device_id=packet.device_id,
                device_name=packet.device_name,
                role=packet.role,
                hostname=packet.hostname,
                platform=packet.platform,
                cpu_count=packet.cpu_count,
                cpu_percent=packet.cpu_percent,
                memory_total_mb=packet.memory_total_mb,
                memory_percent=packet.memory_percent,
                disk_total_gb=packet.disk_total_gb,
                disk_percent=packet.disk_percent,
                shared_folder=packet.shared_folder,
                ip_addresses=packet.ip_addresses or [ip],
                api_port=packet.api_port,
                code_version=packet.code_version,
                version_ts=packet.version_ts,
            )
            self.station_director.on_host_registered(info)
            # 触发 WS 立即推送 (从非 async 线程安全地设置 event)
            if self._ws_push_event:
                try:
                    loop = self._ws_push_event._loop
                    loop.call_soon_threadsafe(self._ws_push_event.set)
                except Exception:
                    pass
            logger.info("UDP 自动注册: %s (%s)", packet.device_name, ip)
            # S3: 新主机入网即时同步 (免轮询): 密钥推送/拉取 + 版本通知
            sync_t = threading.Thread(
                target=self._sync_with_new_peer,
                args=(packet.device_id, ip, packet.api_port,
                      packet.role, packet.code_version, packet.version_ts),
                name="station-peer-sync", daemon=True,
            )
            sync_t.start()
            self._threads.append(sync_t)
        except Exception as e:
            logger.error("UDP 自动注册异常: %s", e)

    def _deploy_config_script(self):
        """将独立采集脚本部署到共享文件夹,供其他主机使用。"""
        script_src = Path(__file__).parent / "collect_config.py"
        script_dest = self.state.shared_folder.path / "collect_config.py"
        if script_src.is_file() and not script_dest.exists():
            shutil.copy2(script_src, script_dest)

    def _refresh_host_config(self):
        """刷新共享文件夹中的本机配置报告。"""
        try:
            info = self._collect_info()
            self.state.shared_folder.write_host_config(info)
        except Exception as e:
            logger.error("配置报告刷新异常: %s", e)

    def _config_refresh_loop(self):
        """定期刷新共享文件夹中的配置报告 + 自身心跳 + 优化12: 定期汇报。"""
        while self._running:
            time.sleep(HEARTBEAT_INTERVAL_SECS)
            self._refresh_host_config()
            # 自身心跳: 保持 Station Director 在线状态
            try:
                info = self._collect_info()
                self.station_director.on_heartbeat(self.state.device_id, {
                    "cpu_percent": info.cpu_percent,
                    "memory_percent": info.memory_percent,
                    "disk_percent": info.disk_percent,
                    "shared_file_count": info.shared_file_count,
                    "ip": info.ip_addresses[0] if info.ip_addresses else "",
                    "code_version": info.code_version,
                    "version_ts": info.version_ts,
                    # E4: 自身角色同步落库 (secretary/station 切换即时可见)
                    "role": info.role,
                })
            except Exception:
                pass
            # 优化12: 定期汇报 (仅 Secretary 激活时)
            if self.secretary_active:
                self._try_periodic_report()

    def _prune_logs_if_due(self):
        """iter-54: 按观测配置周期修剪日志表并 VACUUM (异常隔离)。

        保留期/周期/开关由 config.yaml observability 段驱动:
        log_retention_days ≤0 或 log_prune_interval_hours ≤0 禁用修剪。
        """
        try:
            obs = self.cfg.observability
            retention = float(obs.log_retention_days or 0)
            interval = max(1.0, float(obs.log_prune_interval_hours or 0)) * 3600
        except Exception:
            return
        if retention <= 0 or interval <= 0:
            return
        now = time.time()
        if now - self._last_log_prune_ts < interval:
            return
        # 无论成败都推进时间戳, 避免每 5s 重试风暴
        self._last_log_prune_ts = now
        try:
            stats = self.db.prune_logs(retention)
            logger.info("[LogPrune] 日志修剪完成: %s", stats)
            if getattr(obs, "log_vacuum", True):
                self.db.vacuum()
                logger.info("[LogPrune] VACUUM 完成")
        except Exception as e:
            logger.warning("[LogPrune] 日志修剪异常 (下轮重试): %s", e)

    def _prune_loop(self):
        """定期清理超时离线主机, 触发 F3.3 PM 迁移与 E5 Secretary 接管检查; iter-54 附带日志容量修剪。"""
        while self._running:
            time.sleep(PRUNE_INTERVAL_SECS)
            try:
                gone_ids = self.station_director.prune_offline(self.cfg.discovery.device_ttl)
                if gone_ids:
                    self._migrate_orphaned_pms(gone_ids)
                self._secretary_failover_check()
                self._prune_logs_if_due()
            except Exception as e:
                logger.error("清理离线主机异常: %s", e)
