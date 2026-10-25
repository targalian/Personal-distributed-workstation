"""StationHostService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from .application_lifecycle import wait_for_stop
from typing import Any
from . import station_hosts as policy
from .component_service import ComponentService

class StationHostService(ComponentService):
    """StationHostService business operations with explicit state ownership."""
    OPERATIONS = ('_load_bot_config', '_on_bot_command', '_collect_info', '_make_packet', '_on_device_seen', '_deploy_config_script', '_refresh_host_config', '_config_refresh_loop', '_prune_logs_if_due', '_prune_loop')

    def _load_bot_config(self) -> Any:
        """从 AppConfig 加载 Bot 通道配置。"""
        bot_cfg = self.owner.cfg.bot
        for ch_cfg in bot_cfg.channels:
            channel = policy.BotChannel(channel_type=ch_cfg.channel_type, enabled=ch_cfg.enabled, webhook_url=ch_cfg.webhook_url, bot_token=ch_cfg.bot_token, chat_id=ch_cfg.chat_id, webhook_url_base=ch_cfg.webhook_url_base, min_priority=ch_cfg.min_priority)
            self.owner.bot_gateway.add_channel(channel)
        self.owner.bot_gateway.set_command_handler(self.owner._on_bot_command)
        enabled_count = sum((1 for c in bot_cfg.channels if c.enabled))
        if enabled_count:
            policy.logger.info('Bot 通道已加载: %d 个启用', enabled_count)

    def _on_bot_command(self, command: str, args: str, chat_id: str) -> str:
        """处理来自 Telegram 的命令。"""
        if command == 'status':
            hosts = self.owner.db.list_hosts()
            online = [h for h in hosts if h.online]
            tasks = self.owner.db.list_tasks(limit=5) if self.owner.secretary_active else []
            return f"📊 工作站状态\n在线主机: {len(online)}/{len(hosts)}\nSecretary: {('激活' if self.owner.secretary_active else '未激活')}\n最近任务: {len(tasks)} 个"
        if command == 'hosts':
            hosts = self.owner.db.list_hosts()
            lines = ['🖥️ 主机列表:']
            for h in hosts:
                status = '✅' if h.online else '❌'
                lines.append(f"{status} {h.device_name or h.hostname or '未知'} ({h.ip or '-'})")
            return '\n'.join(lines) if len(lines) > 1 else '暂无主机'
        if command == 'tasks':
            if not self.owner.secretary_active:
                return 'Secretary 未激活'
            tasks = self.owner.db.list_tasks(limit=5)
            if not tasks:
                return '暂无任务'
            lines = ['📋 最近任务:']
            for t in tasks:
                icon = {'completed': '✅', 'failed': '❌', 'running': '🔄', 'pending': '⏳'}.get(t.status, '❓')
                lines.append(f'{icon} {t.name} [{t.status}]')
            return '\n'.join(lines)
        return f'未知命令: /{command}\n可用: /status /hosts /tasks /help'

    def _collect_info(self) -> policy.HostInfo:
        """采集 Station Director 自身的主机信息。"""
        role = 'secretary' if self.owner.secretary_active else 'station'
        return policy.collect_host_info(device_id=self.owner.state.device_id, device_name=self.owner.state.device_name, role=role, api_port=self.owner.state.api_port, shared_folder=str(self.owner.state.shared_folder.path), shared_file_count=self.owner.state.shared_folder.file_count(), start_time=self.owner.state.start_time)

    def _make_packet(self) -> policy.DiscoveryPacket:
        """生成 Station 的 UDP 发现包。"""
        info = self.owner._collect_info()
        packet = policy.make_discovery_packet(info)
        packet.role = 'secretary' if self.owner.secretary_active else 'station'
        return packet

    def _on_device_seen(self, packet: policy.DiscoveryPacket, ip: str) -> Any:
        """UDP 发现到设备时: 首次自动注册入 DB, 后续更新 last_seen (轻量心跳)。

        UDP presence 包每 3 秒到达一次, 证明对方存活。
        对于已注册主机, 利用 UDP 包更新 last_seen + IP, 避免被 prune_offline 误判离线。
        这对 Station 间互相发现尤为重要 (Station 间无 HTTP 心跳通道)。
        """
        if not packet.device_id or packet.device_id == self.owner.state.device_id:
            return
        if packet.role == 'secretary' and self.owner.secretary_active and (packet.device_id < self.owner.state.device_id):
            self.owner._yield_secretary_to(packet.device_name, ip, packet.api_port)
        existing = self.owner.db.get_host(packet.device_id)
        if existing:
            try:
                self.owner.station_director.on_heartbeat(packet.device_id, {'cpu_percent': packet.cpu_percent, 'memory_percent': packet.memory_percent, 'disk_percent': packet.disk_percent, 'ip': ip, 'code_version': packet.code_version, 'version_ts': packet.version_ts, 'role': packet.role})
            except Exception:
                pass
            return
        try:
            info = policy.HostInfo(device_id=packet.device_id, device_name=packet.device_name, role=packet.role, hostname=packet.hostname, platform=packet.platform, cpu_count=packet.cpu_count, cpu_percent=packet.cpu_percent, memory_total_mb=packet.memory_total_mb, memory_percent=packet.memory_percent, disk_total_gb=packet.disk_total_gb, disk_percent=packet.disk_percent, shared_folder=packet.shared_folder, ip_addresses=packet.ip_addresses or [ip], api_port=packet.api_port, code_version=packet.code_version, version_ts=packet.version_ts)
            self.owner.station_director.on_host_registered(info)
            if self.owner._ws_push_event:
                try:
                    loop = self.owner._ws_push_event._loop
                    loop.call_soon_threadsafe(self.owner._ws_push_event.set)
                except Exception:
                    pass
            policy.logger.info('UDP 自动注册: %s (%s)', packet.device_name, ip)
            sync_t = policy.threading.Thread(target=self.owner._sync_with_new_peer, args=(packet.device_id, ip, packet.api_port, packet.role, packet.code_version, packet.version_ts), name='station-peer-sync', daemon=True)
            sync_t.start()
            self.owner._threads.append(sync_t)
        except Exception as e:
            policy.logger.error('UDP 自动注册异常: %s', e)

    def _deploy_config_script(self) -> Any:
        """将独立采集脚本部署到共享文件夹,供其他主机使用。"""
        script_src = policy.Path(__file__).parent / 'collect_config.py'
        script_dest = self.owner.state.shared_folder.path / 'collect_config.py'
        if script_src.is_file() and (not script_dest.exists()):
            policy.shutil.copy2(script_src, script_dest)

    def _refresh_host_config(self) -> Any:
        """刷新共享文件夹中的本机配置报告。"""
        try:
            info = self.owner._collect_info()
            self.owner.state.shared_folder.write_host_config(info)
        except Exception as e:
            policy.logger.error('配置报告刷新异常: %s', e)

    def _config_refresh_loop(self) -> Any:
        """定期刷新共享文件夹中的配置报告 + 自身心跳 + 优化12: 定期汇报。"""
        while self.owner._running:
            if wait_for_stop(self.owner, policy.HEARTBEAT_INTERVAL_SECS):
                return
            self.owner._refresh_host_config()
            try:
                info = self.owner._collect_info()
                self.owner.station_director.on_heartbeat(self.owner.state.device_id, {'cpu_percent': info.cpu_percent, 'memory_percent': info.memory_percent, 'disk_percent': info.disk_percent, 'shared_file_count': info.shared_file_count, 'ip': info.ip_addresses[0] if info.ip_addresses else '', 'code_version': info.code_version, 'version_ts': info.version_ts, 'role': info.role})
            except Exception:
                pass
            if self.owner.secretary_active:
                self.owner._try_periodic_report()

    def _prune_logs_if_due(self) -> Any:
        """iter-54: 按观测配置周期修剪日志表并 VACUUM (异常隔离)。

        保留期/周期/开关由 config.yaml observability 段驱动:
        log_retention_days ≤0 或 log_prune_interval_hours ≤0 禁用修剪。
        """
        try:
            obs = self.owner.cfg.observability
            retention = float(obs.log_retention_days or 0)
            interval = max(1.0, float(obs.log_prune_interval_hours or 0)) * 3600
        except Exception:
            return
        if retention <= 0 or interval <= 0:
            return
        now = policy.time.time()
        if now - self.owner._last_log_prune_ts < interval:
            return
        self.owner._last_log_prune_ts = now
        try:
            stats = self.owner.db.prune_logs(retention)
            policy.logger.info('[LogPrune] 日志修剪完成: %s', stats)
            if getattr(obs, 'log_vacuum', True):
                self.owner.db.vacuum()
                policy.logger.info('[LogPrune] VACUUM 完成')
        except Exception as e:
            policy.logger.warning('[LogPrune] 日志修剪异常 (下轮重试): %s', e)

    def _prune_loop(self) -> Any:
        """定期清理超时离线主机, 触发 F3.3 PM 迁移与 E5 Secretary 接管检查; iter-54 附带日志容量修剪。"""
        while self.owner._running:
            if wait_for_stop(self.owner, policy.PRUNE_INTERVAL_SECS):
                return
            try:
                gone_ids = self.owner.station_director.prune_offline(self.owner.cfg.discovery.device_ttl)
                if gone_ids:
                    self.owner._migrate_orphaned_pms(gone_ids)
                self.owner._secretary_failover_check()
                self.owner._prune_logs_if_due()
            except Exception as e:
                policy.logger.error('清理离线主机异常: %s', e)
