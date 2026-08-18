"""主机级工作站单实例守护 (E6)。

确保同一主机同时只运行一个工作站 (station) 进程, 杜绝重复实例:

- 锁文件 ~/.lan_mesh/station.lock 记录 {pid, commit, commit_time, port, started_at}
- 无锁 / 锁 PID 已死 (僵尸锁) → 接管并写锁, 继续启动
- 锁有效 + 同版本 → 取消启动 (已是最新, 勿重复拉起)
- 锁有效 + 当前更新 → 关闭旧进程后接管 (升级场景: git pull 后重启)
- 锁有效 + 当前更旧 → 取消启动 (已有更新版本实例在运行)

dev-reload 场景: 新进程由旧进程拉起 (旧进程已请求退出),
同版本也允许接管, 由新进程顶上。

设计要点:
- 锁放用户数据目录 (~/.lan_mesh) 而非工作区, 跨工作区/跨 checkout 生效
- 进程被强杀/崩溃不清理锁, 留僵尸锁由后续接管逻辑覆盖
- 进程探测/关闭仅作用于同主机同用户进程, 不触碰其他用户
"""

import json
import os
import socket
import subprocess
import time
from pathlib import Path

from .logger import get_logger

logger = get_logger("singleton")

LOCK_NAME = "station.lock"


def _lock_path() -> Path:
    """锁文件路径: 与 DB 同目录 (主机级, 跨工作区生效)。"""
    return Path.home() / ".lan_mesh" / LOCK_NAME


def _pid_alive(pid: int) -> bool:
    """探测进程是否存活 (Windows tasklist, POSIX kill(0))。"""
    if not pid or pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
        return str(pid) in out
    except Exception:
        return True  # 无法探测时保守视为存活


def _kill_process(pid: int) -> bool:
    """强制结束进程树 (含 dev-reload 子进程)。"""
    if not pid or pid <= 0:
        return False
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                capture_output=True, text=True, timeout=20,
            )
        else:
            os.kill(pid, 9)
        return True
    except Exception as e:
        logger.warning("[单实例] 关闭旧进程失败 (PID %d): %s", pid, e)
        return False


def _wait_port_free(port: int, timeout: float = 8.0) -> bool:
    """等待端口释放 (旧进程关闭后 bind 前消除竞态)。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                s.connect(("127.0.0.1", port))
        except OSError:
            return True
        time.sleep(0.2)
    return False


def _port_holder(port: int) -> int:
    """返回监听指定端口的进程 PID (无则 0)。"""
    try:
        import psutil
        for c in psutil.net_connections(kind="tcp"):
            if c.status == "LISTEN" and c.laddr and c.laddr.port == port:
                return c.pid or 0
    except Exception:
        pass
    # psutil 不可用/失败: Windows netstat 兜底
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True,
                timeout=15).stdout
            for line in out.splitlines():
                if "LISTENING" in line and f":{port}" in line:
                    parts = line.split()
                    if parts:
                        try:
                            return int(parts[-1])
                        except ValueError:
                            return 0
    except Exception:
        pass
    return 0


def _is_station_process(pid: int) -> bool:
    """判断进程是否为工作站 (main.py station) 进程。"""
    if not pid or pid <= 0:
        return False
    try:
        import psutil
        cmd = psutil.Process(pid).cmdline()
    except Exception:
        return False
    joined = " ".join(cmd or [])
    return "main.py" in joined and "station" in joined


def _write_lock(pid: int, commit: str, commit_time: float, port: int):
    """写入锁文件 (记录本进程身份与版本, 供后续实例仲裁)。"""
    try:
        lock = _lock_path()
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({
            "pid": pid,
            "commit": commit,
            "commit_time": commit_time,
            "port": port,
            "started_at": time.time(),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.warning("[单实例] 锁文件写入失败: %s", e)


def _clear_lock(pid: int):
    """正常退出时清理自己的锁 (强杀/崩溃留僵尸锁, 由接管逻辑覆盖)。"""
    try:
        lock = _lock_path()
        if lock.exists():
            info = json.loads(lock.read_text(encoding="utf-8") or "{}")
            if info.get("pid") == pid:
                lock.unlink()
    except Exception:
        pass


def register_cleanup(pid: int = None):
    """注册 atexit 清理 (正常退出时移除自己的锁)。"""
    import atexit
    atexit.register(_clear_lock, pid or os.getpid())


def ensure_single_instance(port: int, commit: str = "", commit_time: float = 0.0,
                           dev_reload: bool = False) -> str:
    """主机级单实例仲裁, 在绑定端口前调用。

    Args:
        port: 计划绑定的 API 端口 (杀旧后等待其释放)
        commit: 本进程代码 commit (git HEAD; 非 git 环境可为空)
        commit_time: 本进程代码提交时间戳 (0 表示不可比较)
        dev_reload: 是否为 dev-reload 拉起的替代进程 (同版本允许接管)

    Returns:
        "proceed": 本进程接管 (旧进程已关闭/不存在, 锁已更新)
        "exit_same": 同版本实例已运行, 应取消启动
        "exit_newer": 已有更新版本实例在运行, 应取消启动
    """
    info = {}
    lock = _lock_path()
    if lock.exists():
        try:
            info = json.loads(lock.read_text(encoding="utf-8") or "{}")
        except Exception:
            pass

    old_pid = int(info.get("pid") or 0)
    old_commit = str(info.get("commit") or "")
    old_time = float(info.get("commit_time") or 0.0)

    # 无锁 / 僵尸锁 (PID 不存在) → 探测端口占用者, 再决定接管
    if not info or not old_pid or not _pid_alive(old_pid):
        holder = _port_holder(port)
        if holder and _is_station_process(holder):
            # 无锁旧版实例 (E6 之前版本无写锁能力): 关闭后接管
            logger.warning("[单实例] 检测到无锁工作站实例 (PID %d @ :%d), "
                           "关闭后接管", holder, port)
            _kill_process(holder)
            _wait_port_free(port, timeout=8)
        _write_lock(os.getpid(), commit, commit_time, port)
        return "proceed"

    # 版本判定一: commit 相同 → 同版本
    if commit and commit == old_commit:
        if dev_reload:
            # dev-reload: 旧进程已请求退出, 杀旧 (或等其自然退出) 后接管
            _kill_process(old_pid)
            _wait_port_free(port, timeout=5)
            _write_lock(os.getpid(), commit, commit_time, port)
            logger.info("[单实例] dev-reload 接管 (同版本 %s)", commit[:8])
            return "proceed"
        logger.warning("[单实例] 已存在同版本实例 (PID %d @ %s), 取消启动",
                       old_pid, commit[:8])
        return "exit_same"

    # 版本判定二: commit 不同 → 以提交时间戳仲裁新旧
    if commit_time > 0 and old_time > 0:
        if commit_time < old_time:
            logger.warning("[单实例] 已有更新版本实例 (PID %d @ %s), 取消启动",
                           old_pid, old_commit[:8])
            return "exit_newer"
        # 当前更新 → 关闭旧版实例后接管 (升级场景)
        logger.warning("[单实例] 检测到旧版实例 (PID %d @ %s), 关闭后接管",
                       old_pid, old_commit[:8])
        _kill_process(old_pid)
        _wait_port_free(port, timeout=8)
        _write_lock(os.getpid(), commit, commit_time, port)
        return "proceed"

    # 时间戳缺失 (非 git 环境): commit 均空视为同版本, 否则保守取消启动
    if not commit and not old_commit:
        if dev_reload:
            _kill_process(old_pid)
            _write_lock(os.getpid(), commit, commit_time, port)
            return "proceed"
        logger.warning("[单实例] 已存在实例 (PID %d), 版本未知无法比较, 取消启动",
                       old_pid)
        return "exit_same"

    logger.warning("[单实例] 已存在实例 (PID %d @ %s), 版本无法比较, 取消启动",
                   old_pid, old_commit[:8] or "(unknown)")
    return "exit_same"
