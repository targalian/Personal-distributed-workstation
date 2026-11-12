"""Stable task identity and fail-closed recovery around a potentially remote PM launch."""

import hashlib
import json
import threading
import uuid
from typing import Any

from .component_service import ComponentService
from .service_runtime import ServiceError
from .task_scheduling_service import get_scheduling_service


class IdempotentSubmissionService(ComponentService):
    OPERATIONS = ('submit',)

    def __init__(self, server: Any, owner: object):
        super().__init__(server, owner)
        self._owner_id = uuid.uuid4().hex
        self._lock = threading.RLock()

    def submit(self, event_id: str, payload: dict) -> dict:
        if not event_id or len(event_id) > 512:
            raise ServiceError(422, '无效触发标识')
        if not self.owner.secretary_active:
            raise ServiceError(503, 'Secretary未激活')
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        task_id = 'task-' + hashlib.sha256(event_id.encode()).hexdigest()[:24]
        with self._lock:
            conn = self.owner.db._get_conn()
            with conn:
                conn.execute('INSERT OR IGNORE INTO periodic_submissions(event_id,fingerprint,task_id,state,owner) VALUES (?,?,?,?,?)',
                             (event_id, fingerprint, task_id, 'reserved', self._owner_id))
            row = dict(conn.execute('SELECT * FROM periodic_submissions WHERE event_id=?', (event_id,)).fetchone())
            if row['fingerprint'] != fingerprint:
                raise ServiceError(409, '同一触发标识对应不同任务内容')
            if row['state'] != 'reserved':
                return self._existing(row)
            scheduler = get_scheduling_service(self.owner)
            task = self.owner.db.get_task(task_id)
            if task is None:
                prepared = {**payload, 'input_data': {**payload.get('input_data', {}), '_trigger_event': event_id}}
                task = scheduler._create_task(prepared, task_id=task_id, initial_status='scheduled')
            with conn:
                claimed = conn.execute("UPDATE periodic_submissions SET state='dispatching',owner=? WHERE event_id=? AND state='reserved'",
                                       (self._owner_id, event_id)).rowcount
            if not claimed:
                return self._existing(dict(conn.execute('SELECT * FROM periodic_submissions WHERE event_id=?', (event_id,)).fetchone()))
            try:
                with conn:
                    conn.execute("UPDATE tasks SET status='pending' WHERE task_id=? AND status='scheduled'", (task_id,))
                from .submission_context import submission_scope
                with submission_scope(task_id):
                    result = scheduler.dispatch_existing(task_id)
            except Exception as exc:
                result = {'task_id': task_id, 'status': 'review_required', 'error': str(exc)}
                self._hold_pending(task_id)
            state = 'review_required' if result.get('status') == 'review_required' else 'completed'
            with conn:
                conn.execute('UPDATE periodic_submissions SET state=?,response_json=? WHERE event_id=?',
                             (state, json.dumps(result, ensure_ascii=False), event_id))
            return result

    def _hold_pending(self, task_id: str) -> None:
        with self.owner.db._get_conn() as conn:
            conn.execute("UPDATE tasks SET status='paused' WHERE task_id=? AND status IN ('pending','queued','scheduled')", (task_id,))

    def _existing(self, row: dict) -> dict:
        if row['state'] in {'completed', 'review_required'}:
            return json.loads(row['response_json'])
        if row['owner'] != self._owner_id:
            result = {'task_id': row['task_id'], 'status': 'review_required',
                      'error': '上次派发在确认前中断，请核对PM与产物；不会自动创建第二个任务'}
            self._hold_pending(row['task_id'])
            with self.owner.db._get_conn() as conn:
                conn.execute("UPDATE periodic_submissions SET state='review_required',response_json=? WHERE event_id=?",
                             (json.dumps(result, ensure_ascii=False), row['event_id']))
            return result
        return {'task_id': row['task_id'], 'status': 'dispatching'}
