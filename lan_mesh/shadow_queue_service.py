"""ShadowQueueService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from . import shadow_dev as policy
from .component_service import ComponentService

class ShadowQueueService(ComponentService):
    """ShadowQueueService business operations with explicit state ownership."""
    OPERATIONS = ('submit', 'status', 'list_runs', 'get_run', '_read_report', '_load_history')

    def submit(self, task: str, backend: str='', timeout: int=1800, simulate: bool=False) -> dict:
        """提交影子任务; 同一时间只执行一个, 其余排队。"""
        task = task.strip()
        if not task:
            raise ValueError('task 不能为空')
        run_id = policy.new_run_id()
        record = {'run_id': run_id, 'task': task, 'backend': 'simulate' if simulate else backend or 'auto', 'timeout': max(30, min(timeout, 1800)), 'simulate': simulate, 'status': 'queued', 'created_at': policy.datetime.now().isoformat()}
        with self.owner._condition:
            if self.owner._stopping:
                raise RuntimeError('影子开发守护已停止')
            self.owner._runs[run_id] = record
            self.owner._queue.append(record)
            queued = len(self.owner._queue)
            self.owner._condition.notify_all()
        policy._emit_run_event(record, 'queued', queued)
        self.owner.start_guardian()
        return record

    def status(self) -> dict:
        """返回守护状态与队列摘要。"""
        with self.owner._condition:
            queued = len(self.owner._queue)
            running = any((run['status'] == 'running' for run in self.owner._runs.values()))
            thread_alive = bool(self.owner._thread and self.owner._thread.is_alive())
            return {'running': thread_alive, 'busy': running, 'queued': queued, 'stopping': self.owner._stopping}

    def list_runs(self) -> list[dict]:
        """合并内存队列/运行状态与历史报告。"""
        with self.owner._condition:
            records = [dict(run) for run in self.owner._runs.values()]
        known = {run['run_id'] for run in records}
        records.extend(self.owner._load_history(known))
        return sorted(records, key=lambda run: run.get('run_id', ''), reverse=True)

    def get_run(self, run_id: str) -> dict | None:
        """获取单次运行详情。"""
        with self.owner._condition:
            if run_id in self.owner._runs:
                return dict(self.owner._runs[run_id])
        return self.owner._read_report(run_id)

    def _read_report(self, run_id: str) -> dict | None:
        """读取历史 report.json。"""
        report_path = policy.SHADOW_HOME / run_id / 'report.json'
        if not report_path.is_file():
            return None
        try:
            data = policy.json.loads(report_path.read_text(encoding='utf-8'))
            data['status'] = data.get('verdict', 'unknown')
            return data
        except Exception:
            return {'run_id': run_id, 'status': 'REPORT_ERROR'}

    def _load_history(self, known_ids: set[str]) -> list[dict]:
        """加载不在内存中的历史运行。"""
        if not policy.SHADOW_HOME.is_dir():
            return []
        records = []
        for directory in policy.SHADOW_HOME.iterdir():
            if not directory.name.startswith('20') or directory.name in known_ids:
                continue
            report = self.owner._read_report(directory.name)
            if report:
                records.append(report)
        return records
