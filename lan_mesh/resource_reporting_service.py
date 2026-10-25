"""ResourceReportingService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import model_resources as policy
from .component_service import ComponentService

class ResourceReportingService(ComponentService):
    """ResourceReportingService business operations with explicit state ownership."""
    OPERATIONS = ('set_report_target', 'stop_reporter', 'report_once', '_report_loop', '_push_once_ws', '_ws_push_loop')

    def set_report_target(self, url: str, interval: float=60.0, token: str='') -> bool:
        """设置/启动用量上报目标 (Worker 主机配置或运行时注入)。

        Args:
            url: Secretary 站点地址 (空 → 不启用上报)
            interval: 上报周期 (秒)
            token: mesh_token (M5-2: 非空时启用 WS 直推通道鉴权)

        Returns:
            是否已启用上报线程
        """
        target = (url or '').strip().rstrip('/')
        self.owner._secretary_url = target
        self.owner._report_interval = max(5.0, float(interval or 60.0))
        if token:
            self.owner._ws_token = str(token)
        ws_url = policy._build_ws_url(target, self.owner._ws_token)
        if ws_url:
            self.owner._ws_url = ws_url
        if not target or not self.owner._enabled or self.owner._db is None:
            return False
        self.owner._stop_evt.clear()
        if not (self.owner._reporter and self.owner._reporter.is_alive()):
            self.owner._reporter = policy.threading.Thread(target=self.owner._report_loop, name='resource-usage-reporter', daemon=True)
            self.owner._reporter.start()
            policy.logger.info('[resources] 用量上报已启用 → %s (周期 %.0fs)', target, self.owner._report_interval)
        if self.owner._ws_url and (not (self.owner._ws_push_thread and self.owner._ws_push_thread.is_alive())):
            self.owner._ws_push_thread = policy.threading.Thread(target=self.owner._ws_push_loop, name='resource-usage-ws-push', daemon=True)
            self.owner._ws_push_thread.start()
            policy.logger.info('[resources] WS 直推已启用 → %s', self.owner._ws_url.split('?', 1)[0])
        return True

    def stop_reporter(self) -> bool:
        """停止上报线程 (含 M5-2 WS 推送线程, 共用 _stop_evt)。"""
        self.owner._stop_evt.set()
        from .application_lifecycle import join_threads
        threads = [self.owner._reporter, self.owner._ws_push_thread, self.owner._alert_checker]
        pending = join_threads(threads, 10.0)
        if pending:
            policy.logger.warning('[resources] 后台线程退出中: %s', pending)
            return False
        self.owner._reporter = None
        self.owner._ws_push_thread = None
        self.owner._alert_checker = None
        return True

    def report_once(self, batch: int=200) -> dict:
        """执行一轮上报 (同步, 供测试/手动触发)。

        未上报记录 → POST 到 Secretary /api/resources/usage (批量),
        成功后标记 reported; 失败不推进游标, 下轮重试 (离线容错)。

        Returns:
            {"reported": 条数, "duplicate": 重复条数, "error"?}
        """
        if not self.owner._secretary_url or self.owner._db is None:
            return {'reported': 0, 'error': 'no_report_target'}
        if self.owner._ws_last_ok and policy.time.time() - self.owner._ws_last_ok < max(30.0, self.owner._report_interval):
            return {'reported': 0, 'via': 'ws'}
        try:
            rows = self.owner._db.query_unreported_usage(batch)
        except Exception as e:
            return {'reported': 0, 'error': f'query_failed: {e}'}
        if not rows:
            return {'reported': 0}
        payload = {'records': [{'usage_id': r['usage_id'], 'model': r['model_id'], 'input_tokens': r['input_tokens'], 'output_tokens': r['output_tokens'], 'task_id': r.get('task_id', ''), 'project_id': r.get('project_id', '')} for r in rows]}
        try:
            import requests
            resp = requests.post(f'{self.owner._secretary_url}/api/resources/usage', json=payload, timeout=10)
            resp.raise_for_status()
            body = resp.json() if resp.text else {}
        except Exception as e:
            policy.logger.warning('[resources] 用量上报失败 (%d 条, 下轮重试): %s', len(rows), e)
            return {'reported': 0, 'pending': len(rows), 'error': str(e)}
        self.owner._db.mark_usage_reported([r['id'] for r in rows])
        dup = int(body.get('duplicate', 0))
        policy.logger.info('[resources] 用量已上报 Secretary: %d 条 (重复 %d)', len(rows), dup)
        return {'reported': len(rows), 'duplicate': dup}

    def _report_loop(self) -> Any:
        """上报线程主循环 — 异常隔离, 永不退出 (直到 stop)。"""
        while not self.owner._stop_evt.wait(self.owner._report_interval):
            try:
                self.owner.report_once()
            except Exception as e:
                policy.logger.warning('[resources] 上报轮次异常: %s', e)

    def _push_once_ws(self, conn) -> bool:
        """一轮 WS 推送: 查未上报记录 → 发批 → 等 ack 推游标。

        Returns:
            True  — 连接可继续使用 (含空轮次/ack 超时重试/被拒暂缓)
            False — 连接已坏或不可用, 外层断开重连
        """
        if self.owner._db is None:
            return False
        try:
            rows = self.owner._db.query_unreported_usage(200)
        except Exception as e:
            policy.logger.warning('[resources] WS 推送查询失败: %s', e)
            return not self.owner._stop_evt.wait(self.owner._ws_push_interval)
        if not rows:
            self.owner._ws_last_ok = policy.time.time()
            return not self.owner._stop_evt.wait(self.owner._ws_push_interval)
        payload = {'type': 'usage_batch', 'records': [{'usage_id': r['usage_id'], 'model': r['model_id'], 'input_tokens': r['input_tokens'], 'output_tokens': r['output_tokens'], 'task_id': r.get('task_id', ''), 'project_id': r.get('project_id', '')} for r in rows]}
        try:
            conn.send(policy.json.dumps(payload))
            raw = conn.recv(timeout=10)
        except TimeoutError:
            policy.logger.debug('[resources] WS ack 超时, %d 条下轮重发', len(rows))
            return True
        except Exception as e:
            policy.logger.warning('[resources] WS 收发失败: %s', e)
            return False
        try:
            ack = policy.json.loads(raw if isinstance(raw, str) else raw.decode('utf-8'))
        except Exception:
            policy.logger.warning('[resources] WS ack 解析失败')
            return False
        if isinstance(ack, dict) and ack.get('ok'):
            self.owner._db.mark_usage_reported([r['id'] for r in rows])
            self.owner._ws_last_ok = policy.time.time()
            policy.logger.info('[resources] 用量已 WS 直推 Secretary: %d 条 (重复 %d)', len(rows), int(ack.get('duplicate', 0)))
            return True
        policy.logger.debug('[resources] WS 推送被拒: %s', ack.get('error', '') if isinstance(ack, dict) else '?')
        return not self.owner._stop_evt.wait(self.owner._report_interval)

    def _ws_push_loop(self) -> Any:
        """M5-2: WS 直推线程主循环 — 实时推送未上报用量。

        连接失败/断开按指数退避重连 (5s→60s 封顶); 断线期间记录
        留存本地库, report_once (HTTP 批量) 自动接手兜底。
        异常隔离, 直到 stop 才退出。
        """
        try:
            from websockets.sync.client import connect as ws_connect
        except Exception as e:
            policy.logger.warning('[resources] websockets 不可用, WS 直推停用 (HTTP 批量兜底仍有效): %s', e)
            return
        backoff = 5.0
        while not self.owner._stop_evt.is_set():
            url = self.owner._ws_url
            if not url:
                if self.owner._stop_evt.wait(10.0):
                    return
                continue
            try:
                conn = ws_connect(url, open_timeout=5)
            except Exception as e:
                policy.logger.debug('[resources] WS 直推连接失败 (%.0fs 后重试): %s', backoff, e)
                if self.owner._stop_evt.wait(backoff):
                    return
                backoff = min(backoff * 2, 60.0)
                continue
            backoff = 5.0
            self.owner._ws_last_ok = policy.time.time()
            policy.logger.info('[resources] WS 直推通道已建立')
            try:
                while not self.owner._stop_evt.is_set():
                    if not self.owner._push_once_ws(conn):
                        break
            except Exception as e:
                policy.logger.warning('[resources] WS 直推轮次异常: %s', e)
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
            policy.logger.info('[resources] WS 直推断开, 稍后重连')
            self.owner._stop_evt.wait(5.0)
