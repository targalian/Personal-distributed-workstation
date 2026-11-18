"""子任务产出文件引用、有限摘要与共享目录内的安全读取。"""

import hashlib
from pathlib import Path, PureWindowsPath

DEFAULT_LIMIT = 4000


def _safe_name(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:40]


def _contained_path(shared_folder: str, rel: str) -> Path:
    root = Path(shared_folder).expanduser().resolve()
    relative = Path(rel)
    if relative.is_absolute() or PureWindowsPath(rel).drive or rel.startswith("\\"):
        raise ValueError("产出引用必须是共享目录内的相对路径")
    target = (root / relative).resolve()
    if not target.is_relative_to(root):
        raise ValueError("产出引用不能越出共享目录")
    return target


def store_output(shared_folder: str, task_id: str, name: str, text: str) -> dict:
    """以 UTF-8 保存完整原文，返回路径、字符数与内容摘要。"""
    rel = f"deliverables/subtasks/{_safe_name(task_id)}_{_safe_name(name)}.md"
    target = _contained_path(shared_folder, rel)
    target.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    target.write_bytes(data)
    print(f"[PM] 子任务产出已保存: {rel} ({len(text)} 字符)")
    return {"path": str(target), "rel": rel, "chars": len(text),
            "sha256": hashlib.sha256(data).hexdigest()[:12]}


def summarize(text: str, limit: int = DEFAULT_LIMIT, ref: dict | None = None) -> str:
    """短文本原样返回；长文本保留开头及可选的完整文件引用。"""
    if limit < 0:
        raise ValueError("limit 不能为负数")
    if len(text) <= limit:
        return text
    source = f"完整内容: {ref['rel']}, " if ref else ""
    return text[:min(800, limit)] + f"\n…({source}共 {len(text)} 字符)"


def resolve(shared_folder: str, rel: str, limit: int = 20000) -> str:
    """读取共享目录内的 UTF-8 文件，限制字符数并拒绝目录越界。"""
    if limit < 0:
        raise ValueError("limit 不能为负数")
    target = _contained_path(shared_folder, rel)
    with target.open(encoding="utf-8", newline="") as stream:
        return stream.read(limit)
