"""WorkerPmService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from typing import Any
from . import worker as policy
from .component_service import ComponentService
from threading import RLock

class WorkerPmService(ComponentService):
    """WorkerPmService business operations with explicit state ownership."""
    OPERATIONS = ('start_pm', 'stop_pm', 'cancel_pm', 'pause_pm', 'inject_pm_input', 'get_pm_status', 'forward_progress_report')

    def __init__(self, server: Any, owner: object):
        super().__init__(server, owner)
        self._lock = RLock()
        self._transitioning = False
        self._retiring: list = []

    @staticmethod
    def _alive(pm: object) -> bool:
        return bool(getattr(pm, '_running', False) or any(
            thread is not None and thread.is_alive()
            for thread in (getattr(pm, '_thread', None), getattr(pm, '_progress_thread', None))))

    def start_pm(self, task_id: str, secretary_url: str, task_data: dict=None) -> dict:
        """Reserve the slot before constructor/network work; avoid callback deadlocks."""
        with self._lock:
            self._retiring = [pm for pm in self._retiring if self._alive(pm)]
            current = self.owner.state.pm_agent
            if self._transitioning or self._retiring or (current and self._alive(current)):
                return {'ok': False, 'message': 'PM Agent 已在运行或退出中'}
            self._transitioning = True
        try:
            return self._start_pm(task_id, secretary_url, task_data)
        except Exception as exc:
            candidate = self.owner.state.pm_agent
            if candidate:
                with self._lock:
                    self._retiring.append(candidate)
                    self.owner.state.pm_agent = None
                try:
                    candidate.cancel()
                except Exception:
                    pass
            print(f'[Worker] PM 启动失败: {task_id}: {exc}')
            return {'ok': False, 'message': str(exc)}
        finally:
            with self._lock:
                self._transitioning = False

    def _start_pm(self, task_id: str, secretary_url: str, task_data: dict=None) -> dict:
        """在本 Worker 上启动 PM Agent 接管指定任务。

        Args:
            task_id: 任务 ID
            secretary_url: Secretary API 地址 (用于上报状态/进度)
            task_data: 任务详情 (可选, 由 Secretary 传入)

        Returns:
            {ok, pm_id} 或 {ok: False, message}
        """
        if self.owner.state.pm_agent and self.owner.state.pm_agent._running:
            return {'ok': False, 'message': 'PM Agent 已在运行'}
        if not self.owner.state.agent_runtime:
            return {'ok': False, 'message': 'Agent 运行时未初始化'}
        import uuid as _uuid
        pm_id = f'pm-{_uuid.uuid4().hex[:12]}'
        if not task_data:
            try:
                import requests as _req
                resp = _req.get(f"{secretary_url.rstrip('/')}/api/tasks/{task_id}", timeout=10)
                if resp.status_code == 200:
                    task_data = resp.json()
            except Exception as e:
                return {'ok': False, 'message': f'获取任务详情失败: {e}'}
        if not task_data:
            return {'ok': False, 'message': '无法获取任务详情'}
        self.owner.state.pm_agent = policy.ProjectManagerAgent(pm_id=pm_id, agent_runtime=self.owner.state.agent_runtime, secretary_url=secretary_url, device_id=self.owner.state.device_id, device_name=self.owner.state.device_name)
        self.owner.state.pm_agent.start_task(task_data)
        policy.logger.info('PM Agent 已启动: %s, 任务: %s', pm_id, task_id)
        return {'ok': True, 'pm_id': pm_id, 'device_id': self.owner.state.device_id}

    def stop_pm(self, expected_pm_id: str="") -> dict:
        """Stop one reserved instance; cancellation callbacks run outside the lock."""
        return self._control('cancel', detach=True, expected_pm_id=expected_pm_id)

    def cancel_pm(self, expected_pm_id: str="") -> dict:
        """Cancel the current instance with an exit barrier."""
        return self._control('cancel', expected_pm_id=expected_pm_id)

    def pause_pm(self, expected_pm_id: str="") -> dict:
        """Pause the current instance with an exit barrier."""
        return self._control('pause', expected_pm_id=expected_pm_id)

    def _control(self, operation: str, detach: bool=False, expected_pm_id: str='') -> dict:
        with self._lock:
            pm = self.owner.state.pm_agent
            if self._transitioning:
                return {'ok': False, 'message': 'PM Agent 正在切换'}
            if not pm:
                return {'ok': False, 'message': 'PM Agent 未运行'}
            if expected_pm_id and pm.pm_id != expected_pm_id:
                return {'ok': False, 'message': 'PM 身份不匹配'}
            self._transitioning = True
        try:
            getattr(pm, operation)()
            with self._lock:
                self._retiring.append(pm)
                if detach and self.owner.state.pm_agent is pm:
                    self.owner.state.pm_agent = None
                    self.owner.state.sub_agents.clear()
            return {'ok': True, 'pm_id': pm.pm_id}
        except Exception as exc:
            return {'ok': False, 'message': str(exc)}
        finally:
            with self._lock:
                self._transitioning = False

    def inject_pm_input(self, input_data: dict, expected_pm_id: str="") -> dict:
        """向 PM Agent 注入来自 Secretary/Boss 的回复输入。

        用于反向沟通通道: Boss 回复 PM 的澄清请求后,
        Secretary 通过此方法将回复注入 PM。

        Args:
            input_data: Boss 回复数据

        Returns:
            {ok: True} 或 {ok: False, message}
        """
        if not self.owner.state.pm_agent:
            return {'ok': False, 'message': 'PM Agent 未运行'}
        if not self.owner.state.pm_agent._running:
            return {'ok': False, 'message': 'PM Agent 已停止'}
        if expected_pm_id and self.owner.state.pm_agent.pm_id != expected_pm_id:
            return {'ok': False, 'message': 'PM 身份不匹配'}
        self.owner.state.pm_agent.receive_input(input_data)
        return {'ok': True, 'pm_id': self.owner.state.pm_agent.pm_id}

    def get_pm_status(self) -> dict:
        """查询本 Worker 上的 PM Agent 运行状态。"""
        if not self.owner.state.pm_agent:
            return {'running': False}
        return self.owner.state.pm_agent.get_status()

    def forward_progress_report(self, report: dict) -> dict:
        """将子 Agent 的进度报告转发给 PM Agent。

        Args:
            report: 进度报告字典

        Returns:
            {ok: True} 或 {ok: False, message}
        """
        if not self.owner.state.pm_agent:
            return {'ok': False, 'message': 'PM Agent 未运行'}
        self.owner.state.pm_agent.receive_progress_report(report)
        return {'ok': True}
