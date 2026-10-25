"""OptimizationQueueService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from . import workstation_optimizer as policy
from .component_service import ComponentService

class OptimizationQueueService(ComponentService):
    """OptimizationQueueService business operations with explicit state ownership."""
    OPERATIONS = ('submit', 'decide', 'list_items', 'get_item', 'summary', 'guardian_status', '_process_once', '_start_item', '_sync_shadow_result', '_shadow_result', '_build_prompt', '_publish')

    def submit(self, source: str, title: str, description: str, priority: str='normal') -> dict:
        """Create an optimization item from Boss, bottleneck, or Agent input."""
        source = source.lower().strip()
        priority = priority.lower().strip()
        if source not in policy.SOURCES:
            raise ValueError('source 必须是 boss/bottleneck/agent')
        if priority not in policy.PRIORITIES:
            raise ValueError('priority 必须是 low/normal/high/urgent')
        title = title.strip()
        description = description.strip()
        if not title:
            raise ValueError('title 不能为空')
        now = policy.time.time()
        status = 'queued' if source == 'boss' else 'waiting_boss' if source == 'bottleneck' else 'candidate'
        item = {'id': f'opt-{policy.uuid.uuid4().hex[:12]}', 'source': source, 'title': title[:120], 'description': description[:4000], 'priority': priority, 'status': status, 'decision_reply': '', 'shadow_run_id': '', 'result': {}, 'created_at': now, 'updated_at': now}
        self.owner.db.save_optimization_item(item)
        event = 'workstation_optimization_waiting_boss' if status in policy.WAITING_STATUSES else 'workstation_optimization_created'
        self.owner._publish(event, item)
        policy.log(f"工作站优化项已创建: {item['id']} ({source}/{status})")
        self.owner.start_guardian()
        return item

    def decide(self, item_id: str, decision: str, reply: str='') -> dict:
        """Apply a Boss decision to a candidate optimization item."""
        item = self.owner.db.get_optimization_item(item_id)
        if not item:
            raise KeyError('优化项不存在')
        if item['status'] not in policy.WAITING_STATUSES:
            raise ValueError('该优化项当前不需要 Boss 决策')
        decision = decision.lower().strip()
        if decision not in {'approve', 'reject', 'clarify'}:
            raise ValueError('decision 必须是 approve/reject/clarify')
        if decision == 'approve':
            item['status'] = 'queued'
        elif decision == 'reject':
            item['status'] = 'rejected'
        if reply.strip():
            previous = item.get('decision_reply', '')
            item['decision_reply'] = f'{previous}\n{reply.strip()}'.strip() if previous else reply.strip()
        item['updated_at'] = policy.time.time()
        self.owner.db.save_optimization_item(item)
        self.owner._publish('workstation_optimization_updated', item)
        policy.log(f'工作站优化项已决策: {item_id} ({decision})')
        self.owner.start_guardian()
        return item

    def list_items(self, status: str='', limit: int=100) -> list[dict]:
        """List optimization items, optionally filtered by status."""
        return self.owner.db.list_optimization_items(status=status, limit=limit)

    def get_item(self, item_id: str) -> dict | None:
        """Return one optimization item."""
        return self.owner.db.get_optimization_item(item_id)

    def summary(self) -> dict:
        """Return guardian status and queue counters."""
        items = self.owner.list_items(limit=500)
        running = next((item for item in items if item['status'] == 'running'), None)
        completed = next((item for item in items if item['status'] == 'completed'), None)
        guardian_running = bool(self.owner._thread and self.owner._thread.is_alive())
        return {'guardian_running': guardian_running, 'queue_count': sum((item['status'] == 'queued' for item in items)), 'waiting_boss_count': sum((item['status'] in policy.WAITING_STATUSES for item in items)), 'running_item': running, 'recent_completed': completed}

    def guardian_status(self) -> dict:
        """Return only guardian lifecycle state."""
        return {'running': bool(self.owner._thread and self.owner._thread.is_alive()), 'stopping': self.owner._stopping}

    def _process_once(self) -> None:
        """Poll running shadow work and submit one queued optimization item."""
        for item in self.owner.list_items(status='running', limit=50):
            self.owner._sync_shadow_result(item)
        queued = self.owner.list_items(status='queued', limit=100)
        if queued:
            self.owner._start_item(sorted(queued, key=lambda item: policy.PRIORITY_ORDER.get(item['priority'], 2))[0])

    def _start_item(self, item: dict) -> None:
        """Submit one approved optimization item to shadow development."""
        prompt = self.owner._build_prompt(item)
        try:
            record = self.owner.shadow_dev_manager.submit(task=prompt, simulate=self.owner.simulate)
            item['shadow_run_id'] = record['run_id']
            item['status'] = 'running'
            item['result'] = {'shadow_status': record.get('status', 'queued')}
        except Exception as exc:
            item['status'] = 'failed'
            item['result'] = {'error': str(exc)}
            policy.log(f"工作站优化项启动失败: {item['id']}: {exc}")
        item['updated_at'] = policy.time.time()
        self.owner.db.save_optimization_item(item)
        self.owner._publish('workstation_optimization_updated', item)

    def _sync_shadow_result(self, item: dict) -> None:
        """Update an optimization item from its shadow run report."""
        run_id = item.get('shadow_run_id', '')
        report = self.owner.shadow_dev_manager.get_run(run_id) if run_id else None
        if not report:
            return
        status = str(report.get('status', report.get('verdict', '')))
        if status in {'READY_FOR_REVIEW', 'completed'}:
            item['status'] = 'completed'
            item['result'] = self.owner._shadow_result(report)
            policy.log(f"工作站优化项已生成待审 diff: {item['id']}")
        elif status in {'GATES_FAILED', 'AGENT_FAILED', 'ERROR', 'failed'}:
            item['status'] = 'failed'
            item['result'] = self.owner._shadow_result(report)
            policy.log(f"工作站优化项执行失败: {item['id']} ({status})")
        else:
            return
        item['updated_at'] = policy.time.time()
        self.owner.db.save_optimization_item(item)
        event = 'workstation_optimization_completed' if item['status'] == 'completed' else 'workstation_optimization_updated'
        self.owner._publish(event, item)

    def _shadow_result(self, report: dict) -> dict:
        """Extract a compact result from a shadow report."""
        diff = report.get('diff', {}) if isinstance(report.get('diff'), dict) else {}
        return {'verdict': report.get('verdict', report.get('status', '')), 'diff_file': diff.get('diff_file', ''), 'diff_bytes': diff.get('bytes', 0), 'error': report.get('error', '')}

    def _build_prompt(self, item: dict) -> str:
        """Build the shadow agent prompt for one optimization item."""
        reply = item.get('decision_reply', '')
        return f"【工作站常驻优化】\n标题: {item['title']}\n来源: {item['source']}\n优先级: {item['priority']}\n说明: {item['description']}\n" + (f'Boss 补充: {reply}\n' if reply else '') + '请在影子副本内完成最小安全实现, 并运行全量验证。'

    def _publish(self, event_type: str, item: dict) -> None:
        """Broadcast one optimization event, isolating callback failures."""
        if not self.owner.broadcast:
            return
        try:
            self.owner.broadcast(event_type, {'item': dict(item)})
        except Exception as exc:
            policy.log(f'工作站优化事件广播失败: {exc}')
