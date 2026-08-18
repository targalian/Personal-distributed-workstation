"""VERSION.json 自动同步脚本 (P2 #10: commit/released_at 对齐 HEAD + 可选 bump)

用法:
    python scripts/update_version.py                 # 同步 commit/released_at 到 HEAD
    python scripts/update_version.py --bump patch    # 递增版本号 (minor/major 同理)
    python scripts/update_version.py --note "..."    # 更新版本说明

规则:
- commit 与 released_at 自动对齐当前 git HEAD (幂等: 已一致则不改)
- version/note/upgrade_hint 默认保留 (人类可读记录, --bump/--note 可改)
- 退出码: 0=成功 (无论是否变更), 1=错误; stdout 末行输出 UPDATED / SAME
"""
import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "VERSION.json"


def _git(cmd: list) -> str:
    r = subprocess.run(["git", "-C", str(ROOT)] + cmd,
                       capture_output=True, text=True, timeout=10,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(cmd)} 失败: {r.stderr.strip()}")
    return r.stdout.strip()


def _bump(version: str, part: str) -> str:
    parts = version.split(".")
    while len(parts) < 3:
        parts.append("0")
    idx = {"major": 0, "minor": 1, "patch": 2}[part]
    parts[idx] = str(int(parts[idx] or 0) + 1)
    for i in range(idx + 1, 3):
        parts[i] = "0"
    return ".".join(parts)


def _is_version_sync_commit(commit: str) -> bool:
    """判断提交是否为 sync_push 的 VERSION.json 自动同步提交。

    收敛规则: 同步提交只改 VERSION.json, 记录目标应为真实代码提交
    (其父), HEAD 停在同步提交时不再触发新一轮同步。
    """
    try:
        subject = _git(["log", "-1", "--format=%s", commit])
    except Exception:
        return False
    return subject.startswith("chore(config): VERSION.json 自动同步")


def main():
    parser = argparse.ArgumentParser(description="VERSION.json 自动同步")
    parser.add_argument("--bump", choices=["major", "minor", "patch"],
                        help="递增版本号对应位 (低位归零)")
    parser.add_argument("--note", help="更新版本说明 note 字段")
    args = parser.parse_args()

    if not VERSION_FILE.is_file():
        print(f"[VersionSync] VERSION.json 不存在: {VERSION_FILE}")
        sys.exit(1)

    try:
        data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        print(f"[VersionSync] VERSION.json 解析失败: {e}")
        sys.exit(1)

    try:
        head = _git(["rev-parse", "--short", "HEAD"])
    except Exception as e:
        print(f"[VersionSync] 读取 git HEAD 失败: {e}")
        sys.exit(1)

    # 收敛: HEAD 为自动同步提交时, 同步目标应为真实代码提交 (其父)
    if _is_version_sync_commit(head):
        try:
            parent = _git(["rev-parse", "--short", head + "~1"])
        except Exception:
            parent = ""
        if data.get("commit") == parent:
            print("[VersionSync] VERSION.json 已是最新 (自动同步提交收敛)")
            print("SAME")
            return

    changed = False
    if args.bump:
        old = data.get("version", "0.0.0")
        data["version"] = _bump(old, args.bump)
        print(f"[VersionSync] 版本号 {old} -> {data['version']}")
        changed = True
    if args.note is not None:
        data["note"] = args.note
        changed = True

    today = date.today().isoformat()
    if data.get("commit") != head:
        print(f"[VersionSync] commit {data.get('commit', '')} -> {head}")
        data["commit"] = head
        changed = True
    if changed and data.get("released_at") != today:
        data["released_at"] = today
        changed = True

    if not changed:
        print("[VersionSync] VERSION.json 已是最新")
        print("SAME")
        return

    VERSION_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"[VersionSync] VERSION.json 已更新 (v{data.get('version', '')} "
          f"@ {head})")
    print("UPDATED")


if __name__ == "__main__":
    main()
