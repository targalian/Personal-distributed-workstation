#!/usr/bin/env python3
"""Loop Engineering - 全局开发态势看板 (只读, 人在回路外时的唯一观察入口)。

汇总五个信息源, 一屏回答「现在到哪了 / 谁在动什么 / 有什么待推 / 能不能推」:
  1. VERSION.json      - 已发布版本与 commit
  2. loop_status.json  - 迭代进度、下一步任务、跨 Agent notes
  3. git              - 未推送 commit、工作区脏文件 (按 Agent 归属分组)
  4. AGENT_LOCKS.md    - 当前文件占用登记
  5. .pending/         - repowiki 待消费队列

用法:
  python scripts/dev_status.py              # 全局概览
  python scripts/dev_status.py --notes      # 附完整 notes (跨 Agent 交接详情)
  python scripts/dev_status.py --verify     # 附门禁自检 (编译/文档清单)
"""
from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent

# 文件归属规则: (前缀/关键字, 归属 Agent) — 与 AGENT_LOCKS.md 第一节保持一致
OWNERSHIP = [
    (".qoder/repowiki/", "Quest"),
    (".qoder/skills/", "Quest"),
    ("docs/reference/", "Quest"),
    ("webui/", "Quest"),
    ("quicklan-main/", "Quest"),
    ("lan_mesh/", "Codex"),
    ("tests/", "Codex"),
    ("docs/design/", "Codex"),
    ("test_bug/", "Codex"),
    ("scripts/", "共享"),
    (".githooks/", "共享"),
]
SHARED_FILES = {"AGENTS.md", "AGENT_LOCKS.md", "loop_status.json", "VERSION.json",
                ".gitignore", "lan_mesh/model_pool.example.yaml"}


def git(*args: str) -> str:
    """执行 git 并返回 stdout (失败返回空串, 不抛异常)。"""
    try:
        out = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True,
                             text=True, encoding="utf-8", errors="replace", timeout=20)
        return (out.stdout or "").strip("\n")
    except Exception:
        return ""


def owner_of(path: str) -> str:
    """按 AGENT_LOCKS.md 规则判定文件归属 Agent。"""
    if path in SHARED_FILES:
        return "共享"
    for prefix, who in OWNERSHIP:
        if path.startswith(prefix):
            return who
    return "未分类"


def load_json(name: str) -> dict:
    """读取根目录 JSON 状态文件。"""
    p = ROOT / name
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def section(title: str) -> None:
    """打印分节标题。"""
    print()
    print(f"── {title} " + "─" * max(0, 58 - len(title)))


def show_version_and_loop() -> dict:
    """输出版本与迭代进度, 返回 loop_status 供后续复用。"""
    ver = load_json("VERSION.json")
    loop = load_json("loop_status.json")
    head = git("rev-parse", "--short", "HEAD")
    branch = git("branch", "--show-current")

    section("版本与迭代")
    print(f"  版本      : {ver.get('version', '?')}  "
          f"(记录 commit {ver.get('commit', '?')} / 发布 {ver.get('released_at', '?')})")
    print(f"  仓库      : 分支 {branch or '?'} @ HEAD {head or '?'}")
    if ver.get("commit") and head and not head.startswith(str(ver.get("commit"))[:7]):
        print("  [!] VERSION.json 的 commit 与 HEAD 不一致 (推送脚本会自动补一次同步提交)")

    phase = loop.get("current_phase", "?")
    status = loop.get("phase_status", "?")
    flag = "OK" if status == "completed" else "..."
    print(f"  当前阶段  : {phase}  [{status}] {flag}")
    print(f"  迭代计数  : {loop.get('iteration_count', '?')} 轮  "
          f"(已完成任务 {len(loop.get('completed', []))} 项, "
          f"失败 {len(loop.get('failed', []))} / 阻塞 {len(loop.get('blocked', []))})")
    return loop


def show_next_tasks(loop: dict) -> None:
    """输出路线图下一步。"""
    section("下一步任务 (loop_status.next_tasks)")
    tasks = loop.get("next_tasks") or []
    if not tasks:
        print("  (空)")
    for i, t in enumerate(tasks, 1):
        text = t if len(t) <= 100 else t[:99] + "…"
        print(f"  {i}. {text}")


def show_git_state() -> int:
    """输出未推送 commit 与按归属分组的脏文件, 返回脏文件总数。"""
    section("待推送 commit (本地领先远端)")
    ahead = git("log", "--oneline", "gitee/master..HEAD")
    if ahead:
        for line in ahead.splitlines():
            print(f"  {line}")
    else:
        print("  (无, 本地与 gitee/master 齐平)")

    section("工作区未提交改动 (按 Agent 归属)")
    porcelain = git("status", "--porcelain", "-z")
    if not porcelain:
        print("  (干净 — 可直接运行 scripts/sync_push.ps1)")
        return 0

    groups: dict[str, list[str]] = {}
    for entry in porcelain.split("\0"):
        if len(entry) < 4:
            continue
        # -z 格式: XY<space>path (路径原样, 不加引号/不转义非 ASCII)
        mark, path = entry[:2].strip(), entry[3:]
        groups.setdefault(owner_of(path), []).append(f"{mark:<2} {path}")

    total = 0
    for who in ("Codex", "Quest", "共享", "未分类"):
        items = groups.get(who)
        if not items:
            continue
        total += len(items)
        print(f"  [{who}] {len(items)} 个文件")
        for it in items[:8]:
            print(f"      {it}")
        if len(items) > 8:
            print(f"      ... 另有 {len(items) - 8} 个 (同归属, 已折叠)")
    return total


def show_locks_and_queue() -> None:
    """输出占用登记表当前占用行与 repowiki 待办队列。"""
    section("Agent 占用登记 (AGENT_LOCKS.md)")
    locks = ROOT / "AGENT_LOCKS.md"
    if not locks.exists():
        print("  [!] AGENT_LOCKS.md 缺失 — 并行开发前应先建立")
    else:
        rows = [ln for ln in locks.read_text(encoding="utf-8").splitlines()
                if ln.startswith("| Codex") or ln.startswith("| Quest")]
        for r in rows:
            cells = [c.strip() for c in r.strip("|").split("|")]
            if len(cells) >= 5:
                busy = "空闲" not in cells[4]
                tag = "占用中" if busy else "空闲"
                print(f"  {cells[0]:<6} [{tag}] 范围={cells[1]}  任务={cells[2]}")

    section("repowiki 待办队列 (post-commit → Quest)")
    pend = ROOT / ".qoder/repowiki/.pending/pending-commits.txt"
    lock = ROOT / ".qoder/repowiki/.pending/.lock"
    if not pend.exists():
        print("  (无待办 — wiki 已与代码同步)")
    else:
        lines = [ln for ln in pend.read_text(encoding="utf-8").splitlines() if ln.strip()]
        print(f"  {len(lines)} 条待 Quest 消费:")
        for ln in lines[-5:]:
            print(f"      {ln}")
    if lock.exists():
        print("  [lock] Quest 任务正在运行中 (10 分钟内不重复拉起)")


def show_verify() -> None:
    """运行只读门禁自检 (编译 + 文档清单一致性)。"""
    section("门禁自检")
    files = ["lan_mesh/pm_agent.py", "lan_mesh/station_controller.py",
             "lan_mesh/station_api.py", "lan_mesh/chat_handler.py",
             "lan_mesh/database.py", "lan_mesh/worker.py", "lan_mesh/orchestrator.py"]
    code = ("import py_compile;"
            f"[py_compile.compile(f, doraise=True) for f in {files!r}];"
            "print('compile OK')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=str(ROOT))
    print(f"  编译检查  : {'PASS' if r.returncode == 0 else 'FAIL — ' + r.stderr[-200:]}")

    r = subprocess.run([sys.executable, "scripts/sync_docs.py"], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", cwd=str(ROOT))
    print(f"  文档清单  : {'PASS' if r.returncode == 0 else 'FAIL (需 --write 修复漂移)'}")
    print("  完整回归  : python -m pytest -q   (约 110s, 未在本看板内自动运行)")


def main() -> int:
    """看板入口。"""
    ap = argparse.ArgumentParser(description="LAN Mesh 全局开发态势看板")
    ap.add_argument("--notes", action="store_true", help="附完整跨 Agent notes")
    ap.add_argument("--verify", action="store_true", help="附门禁自检")
    args = ap.parse_args()

    print("=" * 64)
    print(" LAN Mesh — 全局开发态势看板")
    print("=" * 64)

    loop = show_version_and_loop()
    show_next_tasks(loop)
    dirty = show_git_state()
    show_locks_and_queue()
    if args.verify:
        show_verify()

    if args.notes:
        section("跨 Agent 交接 notes (完整)")
        for seg in str(loop.get("notes", "")).split("["):
            if seg.strip():
                print(f"  [{seg.strip()}" if not seg.startswith(" ") else f"  {seg.strip()}")

    section("下一步动作")
    if dirty:
        print("  有未提交改动 → 运行:  powershell -File scripts/ship.ps1")
        print("  (该脚本按归属分批提交并推送, 每步询问 y/n; 只看不做加 -DryRun)")
    else:
        ahead = git("log", "--oneline", "gitee/master..HEAD")
        if ahead:
            print("  工作区干净但有未推送 commit → 运行:  powershell -File scripts/ship.ps1")
        else:
            print("  全部已同步, 无待办动作。")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
