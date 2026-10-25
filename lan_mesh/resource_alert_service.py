"""ResourceAlertService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import model_resources as policy
from .component_service import ComponentService

class ResourceAlertService(ComponentService):
    """ResourceAlertService business operations with explicit state ownership."""
    OPERATIONS = ('set_bot_notify', 'check_alerts', 'start_alert_checker', '_alert_loop')

    def set_bot_notify(self, callback: policy.Optional[policy.Callable]) -> None:
        """注入 Bot 推送回调 fn(event_type, data); 未注入时仅日志+内存记录。"""
        self.owner._bot_notify = callback

    def check_alerts(self, now: float=None) -> list:
        """扫描全部资源池, 生成到期/额度/重置预警。

        规则:
        - 到期 (expire_at>0): 14 天内 low / 7 天内 normal / 3 天内或已过期 high
        - 额度: 达阈值 normal / >=95% 或耗尽 high
        - 重置 (billing_period=renew): 距窗口重置 <=3 天 low (每窗口一次)

        去重: 同 (池,类别) 仅在档位升级时重新推送, 避免刷屏。

        Returns:
            本轮新推送的预警列表 (全量活跃预警存 _active_alerts)
        """
        now = now or policy.time.time()
        alerts: list[dict] = []
        for rid, pool in self.owner._resources.items():
            if pool.status == 'paused':
                continue
            if pool.expire_at > 0:
                days = (pool.expire_at - now) / 86400
                if days <= 0:
                    lv, msg = (3, f'已过期 ({-days:.1f} 天前), 相关模型已从路由剔除')
                elif days <= 3:
                    lv, msg = (3, f'{days:.1f} 天后到期, 请尽快续费或调整关联模型')
                elif days <= 7:
                    lv, msg = (2, f'{days:.1f} 天后到期')
                elif days <= 14:
                    lv, msg = (1, f'{days:.1f} 天后到期')
                else:
                    lv, msg = (0, '')
                if lv:
                    alerts.append({'resource_id': rid, 'kind': 'expire', 'level': lv, 'message': msg})
            if pool.quota > 0:
                usage = self.owner.get_usage(rid)
                rate = usage.get('rate', 0.0)
                if rate >= 1.0:
                    lv, msg = (3, f"额度已耗尽 ({usage.get('used')}/{pool.quota}), 相关模型已从路由剔除")
                elif rate >= max(pool.alert_threshold, 0.95):
                    lv, msg = (3, f'使用率 {rate * 100:.0f}%, 剩余不足 {(1 - rate) * pool.quota:.0f}')
                elif rate >= pool.alert_threshold:
                    lv, msg = (2, f'使用率 {rate * 100:.0f}% 已达告警阈值 {pool.alert_threshold * 100:.0f}%')
                else:
                    lv, msg = (0, '')
                if lv:
                    alerts.append({'resource_id': rid, 'kind': 'quota', 'level': lv, 'message': msg})
            if pool.billing_period == 'renew' and pool.renew_at > 0:
                step = max(1, pool.period_days) * 86400
                ws = pool.window_start(now)
                days_left = (ws + step - now) / 86400
                if 0 < days_left <= 3:
                    alerts.append({'resource_id': rid, 'kind': 'renew', 'level': 1, 'message': f'额度窗口将于 {days_left:.1f} 天后重置', '_window': ws})
        pushed: list[dict] = []
        for a in alerts:
            key = (a['resource_id'], a['kind'])
            if a['kind'] == 'renew':
                if self.owner._alert_state.get(key) == a['_window']:
                    continue
                self.owner._alert_state[key] = a['_window']
            else:
                if a['level'] <= self.owner._alert_state.get(key, 0):
                    continue
                self.owner._alert_state[key] = a['level']
            pushed.append(a)
            policy.logger.warning('[resources] 预警 %s/%s (Lv%d): %s', a['resource_id'], a['kind'], a['level'], a['message'])
            if self.owner._bot_notify:
                try:
                    self.owner._bot_notify(self.owner._ALERT_EVENT.get(a['level'], 'resource_alert'), {'resource_id': a['resource_id'], 'message': a['message']})
                except Exception as e:
                    policy.logger.warning('[resources] 预警推送失败: %s', e)
        for a in alerts:
            a.pop('_window', None)
        self.owner._active_alerts = alerts
        if pushed:
            try:
                from .event_bus import publish_event
                publish_event('resource_alert', {'count': len(pushed), 'max_level': max((x['level'] for x in pushed))})
            except Exception:
                pass
        return pushed

    def start_alert_checker(self, interval: float=300.0) -> bool:
        """启动后台预警检查 (daemon 线程, 异常隔离); 启动时立即检查一轮。"""
        if not self.owner._enabled:
            return False
        self.owner._alert_interval = max(30.0, float(interval or 300.0))
        if self.owner._alert_checker and self.owner._alert_checker.is_alive():
            return True
        self.owner._stop_evt.clear()
        self.owner._alert_checker = policy.threading.Thread(target=self.owner._alert_loop, name='resource-alert-checker', daemon=True)
        self.owner._alert_checker.start()
        try:
            self.owner.check_alerts()
        except Exception as e:
            policy.logger.warning('[resources] 首次预警检查异常: %s', e)
        policy.logger.info('[resources] 预警检查已启动 (周期 %.0fs)', self.owner._alert_interval)
        return True

    def _alert_loop(self) -> Any:
        """预警检查线程主循环 — 与上报线程共用 _stop_evt。"""
        while not self.owner._stop_evt.wait(self.owner._alert_interval):
            try:
                self.owner.check_alerts()
            except Exception as e:
                policy.logger.warning('[resources] 预警检查轮次异常: %s', e)
