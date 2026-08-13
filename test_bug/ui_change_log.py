#!/usr/bin/env python3
"""
Loop Engineering — UI 改动待检登记工具

每当 dashboard.html (或其他前端模板/静态资源) 发生 UI 改动后,
在此登记一条「未检测」条目, 防止行为回归被遗漏。

状态流转: 未检测 → 检测通过 / 检测失败(回归)

用法:
  python test_bug/ui_change_log.py add "资源配置向导" --detail "新增配置弹窗" --level P2
  python test_bug/ui_change_log.py list               # 列出全部未检测项
  python test_bug/ui_change_log.py check UI-022       # 标记检测通过
  python test_bug/ui_change_log.py check UI-022 --note "实测正常"  # 通过并附说明
  python test_bug/ui_change_log.py check UI-022 --fail "弹窗不显示"  # 标记检测失败
"""
import argparse
import re
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from test_bug.run_loop import load_csv, save_csv, load_config

TEST_BUG_DIR = Path(__file__).parent

STATUS_PENDING = "未检测"
STATUS_PASSED = "检测通过"
STATUS_FAILED = "检测失败"


def _csv_path() -> Path:
    config = load_config()
    return TEST_BUG_DIR / config.get("loop", {}).get("csv_file", "test_checklist.csv")


def _next_ui_id(rows: list[dict]) -> str:
    """现有 UI-xxx 最大编号 + 1。"""
    nums = []
    for r in rows:
        m = re.match(r"UI-(\d+)", r.get("编号", ""))
        if m:
            nums.append(int(m.group(1)))
    return f"UI-{(max(nums) + 1) if nums else 1:03d}"


def add_change(name: str, detail: str = "", level: str = "P2") -> str:
    """登记一条 UI 改动待检项, 返回编号。"""
    if level not in ("P0", "P1", "P2", "P3"):
        raise ValueError(f"非法严重级别: {level}")
    csv_path = _csv_path()
    rows = load_csv(csv_path) or []
    uid = _next_ui_id(rows)
    today = date.today().isoformat()
    rows.append({
        "编号": uid,
        "模块": "前端",
        "功能点": f"[UI改动] {name}",
        "测试类型": "UI行为检测",
        "预期行为": detail or "改动后的交互行为符合预期",
        "实际表现": "待检测",
        "严重级别": level,
        "状态": STATUS_PENDING,
        "发现日期": today,
        "修复日期": "",
        "备注": f"UI改动登记@{today}",
    })
    save_csv(csv_path, rows)
    return uid


def list_pending(rows: list[dict] | None = None) -> list[dict]:
    """返回所有未检测条目。"""
    if rows is None:
        rows = load_csv(_csv_path()) or []
    return [r for r in rows if r.get("状态") == STATUS_PENDING]


def mark_checked(uid: str, failed: bool = False, note: str = "") -> dict:
    """将未检测条目标记为 检测通过/检测失败。"""
    csv_path = _csv_path()
    rows = load_csv(csv_path) or []
    row = next((r for r in rows if r.get("编号") == uid), None)
    if row is None:
        raise ValueError(f"未找到条目: {uid}")
    if row.get("状态") != STATUS_PENDING:
        raise ValueError(f"{uid} 状态为「{row.get('状态')}」, 只有未检测项可标记")
    today = date.today().isoformat()
    if failed:
        row["状态"] = STATUS_FAILED
        row["实际表现"] = note or "检测失败"
        row["备注"] = (row.get("备注", "") + f" | 检测失败@{today}: {note}").strip(" |")
    else:
        row["状态"] = STATUS_PASSED
        row["修复日期"] = today
        row["备注"] = (row.get("备注", "") + f" | 检测通过@{today}" + (f": {note}" if note else "")).strip(" |")
    save_csv(csv_path, rows)
    return row


def main():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="UI 改动待检登记工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add", help="登记 UI 改动为未检测")
    p_add.add_argument("name", help="改动点名称, 如「资源配置向导」")
    p_add.add_argument("--detail", default="", help="改动内容与预期行为说明")
    p_add.add_argument("--level", default="P2", choices=["P0", "P1", "P2", "P3"])

    sub.add_parser("list", help="列出全部未检测项")

    p_chk = sub.add_parser("check", help="标记检测结果")
    p_chk.add_argument("uid", help="条目编号, 如 UI-022")
    p_chk.add_argument("--fail", default="", metavar="NOTE",
                       help="标记为检测失败并附原因")
    p_chk.add_argument("--note", default="", help="检测通过时的补充说明")
    args = parser.parse_args()

    if args.cmd == "add":
        uid = add_change(args.name, args.detail, args.level)
        print(f"[OK] 已登记 {uid}: {args.name} (状态: {STATUS_PENDING})")
    elif args.cmd == "list":
        pending = list_pending()
        if not pending:
            print("[i] 无未检测的 UI 改动项")
            return
        print(f"未检测 UI 改动 ({len(pending)} 项):")
        for r in pending:
            print(f"  [{r['编号']}] [{r.get('严重级别', '?')}] {r.get('功能点', '')} — {r.get('预期行为', '')}")
    elif args.cmd == "check":
        note = args.fail if args.fail else args.note
        row = mark_checked(args.uid, failed=bool(args.fail), note=note)
        print(f"[OK] {args.uid} → {row['状态']}")


if __name__ == "__main__":
    main()
