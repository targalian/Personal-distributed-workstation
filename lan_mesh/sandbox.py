"""F2.2: 代码执行沙箱 — 安全隔离执行 Agent 生成的代码。

策略:
- 使用 subprocess 隔离执行 (独立进程, 非 eval)
- 超时保护 (默认 30s, 最大 120s)
- 工作目录隔离 (临时目录, 执行后清理)
- 可选 venv 隔离 (安装依赖时启用)
- 输出捕获 (stdout + stderr, 截断到 10KB)

用法:
    from .sandbox import CodeSandbox
    sb = CodeSandbox()
    result = sb.execute("print('hello')", language="python", timeout=30)
    # result: {"success": True, "stdout": "hello\n", "stderr": "", "exit_code": 0, "duration": 0.12}
"""

import os
import sys
import time
import shutil
import tempfile
import subprocess
import threading
from typing import Optional
from pathlib import Path

from .logger import get_logger

logger = get_logger("sandbox")

# ── 配置 ──────────────────────────────────────────────────────────

MAX_TIMEOUT = 120        # 最大允许超时 (秒)
DEFAULT_TIMEOUT = 30     # 默认超时
MAX_OUTPUT_BYTES = 10240  # 输出截断阈值 (10KB)
SANDBOX_BASE_DIR = os.path.join(tempfile.gettempdir(), "lan_mesh_sandbox")

# 语言 → 文件扩展名 + 执行命令
LANGUAGE_CONFIG = {
    "python": {"ext": ".py", "cmd": [sys.executable, "{file}"]},
    "javascript": {"ext": ".js", "cmd": ["node", "{file}"]},
    "bash": {"ext": ".sh", "cmd": ["bash", "{file}"]},
    "powershell": {"ext": ".ps1", "cmd": ["powershell", "-ExecutionPolicy", "Bypass", "-File", "{file}"]},
}


class SandboxResult:
    """沙箱执行结果。"""

    def __init__(self, success: bool, stdout: str, stderr: str,
                 exit_code: int, duration: float, timed_out: bool = False):
        self.success = success
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code
        self.duration = duration
        self.timed_out = timed_out

    def to_dict(self) -> dict:
        return {
            "success": self.success,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
            "duration": round(self.duration, 3),
            "timed_out": self.timed_out,
        }


class CodeSandbox:
    """代码执行沙箱。

    安全特性:
    - 独立子进程执行 (非 eval/exec)
    - 超时强制终止
    - 临时工作目录 (执行后可选清理)
    - 输出大小截断
    """

    def __init__(self, base_dir: str = None, keep_workdir: bool = False):
        """
        Args:
            base_dir: 沙箱工作目录根路径 (默认系统临时目录)
            keep_workdir: 执行后是否保留工作目录 (调试用)
        """
        self.base_dir = base_dir or SANDBOX_BASE_DIR
        self.keep_workdir = keep_workdir
        os.makedirs(self.base_dir, exist_ok=True)

    def execute(self, code: str, language: str = "python",
                timeout: int = DEFAULT_TIMEOUT,
                workdir: str = None,
                env: dict = None) -> SandboxResult:
        """执行代码并返回结果。

        Args:
            code: 要执行的代码字符串
            language: 编程语言 (python/javascript/bash/powershell)
            timeout: 超时秒数 (最大 120)
            workdir: 指定工作目录 (默认创建临时目录)
            env: 额外环境变量

        Returns:
            SandboxResult 对象
        """
        # 参数校验
        timeout = min(max(timeout, 1), MAX_TIMEOUT)
        lang_cfg = LANGUAGE_CONFIG.get(language.lower())
        if not lang_cfg:
            return SandboxResult(
                success=False, stdout="", 
                stderr=f"不支持的语言: {language} (支持: {list(LANGUAGE_CONFIG.keys())})",
                exit_code=-1, duration=0
            )

        # 创建工作目录
        if workdir:
            exec_dir = workdir
            os.makedirs(exec_dir, exist_ok=True)
            cleanup = False
        else:
            exec_dir = tempfile.mkdtemp(prefix="sb_", dir=self.base_dir)
            cleanup = not self.keep_workdir

        # 写入代码文件
        code_file = os.path.join(exec_dir, f"main{lang_cfg['ext']}")
        try:
            with open(code_file, "w", encoding="utf-8") as f:
                f.write(code)
        except Exception as e:
            return SandboxResult(
                success=False, stdout="", stderr=f"写入代码文件失败: {e}",
                exit_code=-1, duration=0
            )

        # 构建执行命令
        cmd = [c.replace("{file}", code_file) for c in lang_cfg["cmd"]]

        # 构建环境变量
        exec_env = os.environ.copy()
        exec_env["PYTHONIOENCODING"] = "utf-8"
        exec_env["SANDBOX_MODE"] = "1"
        if env:
            exec_env.update(env)

        # 执行
        start_time = time.time()
        timed_out = False

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=exec_dir,
                env=exec_env,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )

            try:
                stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired:
                # 超时: 强制终止进程树
                timed_out = True
                self._kill_process_tree(proc)
                stdout_bytes, stderr_bytes = proc.communicate(timeout=5)

            duration = time.time() - start_time

            stdout = stdout_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT_BYTES]
            stderr = stderr_bytes.decode("utf-8", errors="replace")[:MAX_OUTPUT_BYTES]
            exit_code = proc.returncode

            if timed_out:
                stderr += f"\n[TIMEOUT] 执行超过 {timeout}s, 已强制终止"

            return SandboxResult(
                success=(exit_code == 0 and not timed_out),
                stdout=stdout,
                stderr=stderr,
                exit_code=exit_code,
                duration=duration,
                timed_out=timed_out,
            )

        except FileNotFoundError as e:
            return SandboxResult(
                success=False, stdout="",
                stderr=f"执行器未找到: {e} (请确认 {language} 运行时已安装)",
                exit_code=-1, duration=time.time() - start_time
            )
        except Exception as e:
            return SandboxResult(
                success=False, stdout="",
                stderr=f"沙箱执行异常: {e}",
                exit_code=-1, duration=time.time() - start_time
            )
        finally:
            if cleanup:
                try:
                    shutil.rmtree(exec_dir, ignore_errors=True)
                except Exception:
                    pass

    def execute_with_venv(self, code: str, packages: list[str] = None,
                          timeout: int = 60) -> SandboxResult:
        """在隔离 venv 中执行 Python 代码 (可选安装依赖)。

        Args:
            code: Python 代码
            packages: 需要 pip install 的包列表
            timeout: 总超时 (含安装时间)
        """
        venv_dir = tempfile.mkdtemp(prefix="sb_venv_", dir=self.base_dir)
        try:
            # 创建 venv
            import venv
            venv.create(venv_dir, with_pip=True, clear=True)

            # 确定 python 路径
            if sys.platform == "win32":
                venv_python = os.path.join(venv_dir, "Scripts", "python.exe")
            else:
                venv_python = os.path.join(venv_dir, "bin", "python")

            # 安装依赖
            if packages:
                pip_cmd = [venv_python, "-m", "pip", "install", "--quiet"] + packages
                try:
                    pip_result = subprocess.run(
                        pip_cmd, capture_output=True, timeout=timeout // 2,
                        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
                    )
                    if pip_result.returncode != 0:
                        stderr = pip_result.stderr.decode("utf-8", errors="replace")
                        return SandboxResult(
                            success=False, stdout="",
                            stderr=f"依赖安装失败: {stderr[:500]}",
                            exit_code=pip_result.returncode, duration=0
                        )
                except subprocess.TimeoutExpired:
                    return SandboxResult(
                        success=False, stdout="",
                        stderr="依赖安装超时",
                        exit_code=-1, duration=0
                    )

            # 在 venv 中执行代码
            code_file = os.path.join(venv_dir, "main.py")
            with open(code_file, "w", encoding="utf-8") as f:
                f.write(code)

            start = time.time()
            proc = subprocess.run(
                [venv_python, code_file],
                capture_output=True,
                timeout=timeout,
                cwd=venv_dir,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            duration = time.time() - start

            return SandboxResult(
                success=(proc.returncode == 0),
                stdout=proc.stdout.decode("utf-8", errors="replace")[:MAX_OUTPUT_BYTES],
                stderr=proc.stderr.decode("utf-8", errors="replace")[:MAX_OUTPUT_BYTES],
                exit_code=proc.returncode,
                duration=duration,
            )

        except subprocess.TimeoutExpired:
            return SandboxResult(
                success=False, stdout="", stderr="venv 执行超时",
                exit_code=-1, duration=timeout, timed_out=True
            )
        except Exception as e:
            return SandboxResult(
                success=False, stdout="", stderr=f"venv 沙箱异常: {e}",
                exit_code=-1, duration=0
            )
        finally:
            try:
                shutil.rmtree(venv_dir, ignore_errors=True)
            except Exception:
                pass

    @staticmethod
    def _kill_process_tree(proc: subprocess.Popen):
        """终止进程树 (Windows: taskkill /T, Unix: killpg)。"""
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=5,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except Exception:
            proc.kill()

    def cleanup(self):
        """清理所有沙箱临时目录。"""
        try:
            if os.path.exists(self.base_dir):
                shutil.rmtree(self.base_dir, ignore_errors=True)
                os.makedirs(self.base_dir, exist_ok=True)
                logger.info("沙箱临时目录已清理")
        except Exception as e:
            logger.debug("沙箱清理失败: %s", e)


# ── 全局单例 ──────────────────────────────────────────────────────

sandbox = CodeSandbox()
