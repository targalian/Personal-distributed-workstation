"""
S2: 版本记录与升级提醒 — 单机版本文件 + 局域网版本比对 + 领先节点通知

核心机制:
1. 项目根 VERSION.json 记录当前发布版本 (版本号/commit/说明/升级提示)
2. 每个节点在 UDP 发现包中携带自身代码版本 (commit + commit 时间戳)
3. 领先检测: 某节点版本严格领先所有在线节点时, 由该节点主动通过
   HTTP 通知落后节点执行 git pull 升级并重启
4. 落后自检: 节点发现他人版本领先自己时同样发出提醒事件 (双保险)

版本比较采用 commit 提交时间戳 (同仓库线性历史下可靠全序);
commit 相同视为同版本, 时间戳缺失视为不可比较 (不告警)。
"""
import json
import subprocess
import threading
import time
from pathlib import Path

from .logger import get_logger

logger = get_logger("version")

# 项目根目录 (lan_mesh/ 的上一级)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_VERSION_FILE = _PROJECT_ROOT / "VERSION.json"

# 进程内缓存 (git 调用有开销, 发现包每 3 秒一次不宜每次起子进程)
_cache_lock = threading.Lock()
_cache: dict = {}
_cache_ts = 0.0
_CACHE_TTL = 60.0


def read_version_file(path: Path = _VERSION_FILE) -> dict:
    """读取 VERSION.json 版本记录文件。缺失/损坏时返回空结构不抛异常。"""
    empty = {"version": "", "commit": "", "released_at": "", "note": "",
             "upgrade_hint": "git pull 后重启节点"}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return empty
        merged = dict(empty)
        merged.update({k: v for k, v in data.items() if k in empty})
        return merged
    except (OSError, json.JSONDecodeError, ValueError):
        return empty


def _git_head_info() -> tuple[str, float]:
    """读取当前 git HEAD 的短 commit 与提交时间戳。非 git 环境返回 ("", 0)。"""
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=_PROJECT_ROOT, capture_output=True, text=True, timeout=5)
        ctime = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            cwd=_PROJECT_ROOT, capture_output=True, text=True, timeout=5)
        if commit.returncode != 0 or ctime.returncode != 0:
            return "", 0.0
        return commit.stdout.strip(), float(ctime.stdout.strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return "", 0.0


def local_version_info(force: bool = False) -> dict:
    """返回本机版本信息 (VERSION.json 记录 + git HEAD 实时补充)。

    优先级: commit/commit_time 以 git HEAD 为准 (反映实际运行代码),
    version/note/upgrade_hint 以 VERSION.json 为准 (人类可读记录);
    git 不可用时整体回退 VERSION.json (commit_time 记 0 表示不可比较)。
    """
    global _cache, _cache_ts
    with _cache_lock:
        if not force and _cache and (time.time() - _cache_ts) < _CACHE_TTL:
            return dict(_cache)

        record = read_version_file()
        commit, commit_time = _git_head_info()
        if not commit:
            # 非 git 环境: 退回版本文件记录 (时间戳未知 → 不参与比较)
            commit = record.get("commit", "")
            commit_time = 0.0
        info = {
            "version": record.get("version", ""),
            "commit": commit,
            "commit_time": commit_time,
            "released_at": record.get("released_at", ""),
            "note": record.get("note", ""),
            "upgrade_hint": record.get("upgrade_hint", ""),
        }
        _cache = dict(info)
        _cache_ts = time.time()
        return info


def compare_versions(mine: dict, other: dict) -> str:
    """比较两个版本。返回 'ahead' / 'behind' / 'equal' / 'unknown'。

    依据 commit 提交时间戳全序比较; commit 相同即同版本;
    任一方 commit 或时间戳缺失则不可比较。
    """
    mc, mt = mine.get("commit", ""), float(mine.get("commit_time", 0) or 0)
    oc, ot = other.get("commit", ""), float(other.get("commit_time", 0) or 0)
    if not mc or not oc:
        return "unknown"
    if mc == oc:
        return "equal"
    if not mt or not ot:
        return "unknown"
    return "ahead" if mt > ot else "behind"


def find_leader(versions: list[dict]) -> dict | None:
    """在一组主机版本中找出严格领先所有其他主机的唯一节点。

    入参每项需含 device_id/commit/commit_time。
    存在并列或不可比较时返回 None (宁可漏报不可误报)。
    """
    if len(versions) < 2:
        return None
    for cand in versions:
        if not cand.get("commit"):
            continue
        leads_all = True
        for other in versions:
            if other.get("device_id") == cand.get("device_id"):
                continue
            rel = compare_versions(cand, other)
            if rel != "ahead":
                leads_all = False
                break
        if leads_all:
            return cand
    return None


class UpgradeNotifier:
    """升级通知去重器 — 同一目标同一版本只通知一次, 避免广播风暴。"""

    def __init__(self):
        self._notified: dict[str, str] = {}   # device_id -> 已通知的 commit
        self._lock = threading.Lock()

    def should_notify(self, device_id: str, commit: str) -> bool:
        """判断并登记: 该目标的该版本是否尚未通知过。"""
        if not device_id or not commit:
            return False
        with self._lock:
            if self._notified.get(device_id) == commit:
                return False
            self._notified[device_id] = commit
            return True

    def reset(self):
        with self._lock:
            self._notified.clear()
