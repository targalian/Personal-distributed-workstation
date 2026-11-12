"""Admit a reserved periodic task only from its current submission call."""

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

_current: ContextVar[str] = ContextVar('periodic_submission', default='')


@contextmanager
def submission_scope(task_id: str) -> Iterator[None]:
    token = _current.set(task_id)
    try:
        yield
    finally:
        _current.reset(token)


def can_dispatch(database: object, task: object) -> bool:
    if not (getattr(task, 'input_data', {}) or {}).get('_trigger_event') or _current.get() == task.task_id:
        return True
    row = database._get_conn().execute('SELECT state FROM periodic_submissions WHERE task_id=?', (task.task_id,)).fetchone()
    return row is not None and row['state'] == 'completed'
