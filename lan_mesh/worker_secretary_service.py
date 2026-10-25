"""WorkerSecretaryService: domain behavior separated from the public compatibility facade."""

from __future__ import annotations
from . import worker as policy
from .component_service import ComponentService

class WorkerSecretaryService(ComponentService):
    """WorkerSecretaryService business operations with explicit state ownership."""
    OPERATIONS = ('start_secretary', 'stop_secretary', 'get_secretary_status')

    def start_secretary(self, port: int=None) -> dict:
        """在本机启动 Secretary 子进程。

        Args:
            port: Secretary HTTP 端口 (默认自动查找)

        Returns:
            {ok, pid, port} 或 {ok: False, message}
        """
        if self.owner._secretary_process and self.owner._secretary_process.poll() is None:
            return {'ok': False, 'message': 'Secretary 已在运行', 'port': self.owner._secretary_port}
        if port is None:
            port = self.owner._find_available_port(45470)
        python_exe = policy.sys.executable
        project_root = str(policy.Path(__file__).parent.parent)
        cmd = [python_exe, 'main.py', 'secretary', '--port', str(port)]
        try:
            self.owner._secretary_process = policy.subprocess.Popen(cmd, stdout=policy.subprocess.DEVNULL, stderr=policy.subprocess.DEVNULL, cwd=project_root, creationflags=policy.subprocess.CREATE_NO_WINDOW if policy.os.name == 'nt' else 0)
            self.owner._secretary_port = port
            policy.logger.info('Secretary 子进程已启动: PID=%d, port=%d', self.owner._secretary_process.pid, port)
            return {'ok': True, 'pid': self.owner._secretary_process.pid, 'port': port}
        except Exception as e:
            policy.logger.error('启动 Secretary 失败: %s', e)
            return {'ok': False, 'message': str(e)}

    def stop_secretary(self) -> dict:
        """停止本机的 Secretary 子进程。"""
        if not self.owner._secretary_process or self.owner._secretary_process.poll() is not None:
            self.owner._secretary_process = None
            self.owner._secretary_port = None
            return {'ok': False, 'message': 'Secretary 未在运行'}
        try:
            self.owner._secretary_process.terminate()
            self.owner._secretary_process.wait(timeout=5)
        except policy.subprocess.TimeoutExpired:
            self.owner._secretary_process.kill()
            self.owner._secretary_process.wait(timeout=3)
        except Exception:
            pass
        policy.logger.info('Secretary 子进程已停止')
        self.owner._secretary_process = None
        self.owner._secretary_port = None
        return {'ok': True, 'message': 'Secretary 已停止'}

    def get_secretary_status(self) -> dict:
        """查询本机 Secretary 运行状态。"""
        if self.owner._secretary_process and self.owner._secretary_process.poll() is None:
            return {'running': True, 'pid': self.owner._secretary_process.pid, 'port': self.owner._secretary_port}
        return {'running': False}
