# -*- coding: utf-8 -*-
"""一次性修正: 把 BUG-033 修复前对话派发的任务补绑到正式项目。

背景: iter-93 之前 `submit_task_from_chat` 不接受 project_id, 对话派发的任务
恒为无项目归属, 导致蓝图驱动规划 (iter-89) 与交付前验收自检 (iter-90) 对这些
任务全部失效。修复后新任务已能绑定, 但存量任务需要一次数据修正。

前置: **必须先停止 Station** (运行中的 Station 独占 SQLite 写锁,
否则报 attempt to write a readonly database)。

用法:
  powershell -File scripts/stop_workstation.ps1
  python scripts/fix_task_project_binding.py --task task-0cdc51ad40dd --project 821230b9-79d6-4004-a96d-2688e58a3016
  python scripts/fix_task_project_binding.py --list        # 只看哪些任务缺归属
"""
from __future__ import annotations

import argparse
import pathlib
import sys

DEFAULT_DB = pathlib.Path.home() / ".lan_mesh" / "secretary.sqlite3"


def build_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="补绑任务的 project_id (BUG-033 存量修正)")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 路径")
    parser.add_argument("--task", default="", help="待修正的 task_id")
    parser.add_argument("--project", default="", help="目标 project_id")
    parser.add_argument("--list", action="store_true",
                        help="仅列出无项目归属的任务, 不做修改")
    return parser


def main(argv: list | None = None) -> int:
    """入口: 列出或修正任务的项目归属。"""
    args = build_parser().parse_args(argv)
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    from lan_mesh.database import Database

    database = Database(args.db)
    if args.list or not (args.task and args.project):
        rows = [t for t in database.list_tasks(limit=200)
                if not getattr(t, "project_id", "")]
        print(f"[Task] 无项目归属的任务: {len(rows)} 个")
        for task in rows:
            print(f"  {task.task_id}  {task.status:<10} {task.name[:40]}")
        if not (args.task and args.project):
            return 0
    task = database.get_task(args.task)
    if task is None:
        print(f"[Task] 任务不存在: {args.task}", file=sys.stderr)
        return 2
    before = getattr(task, "project_id", "")
    task.project_id = args.project
    try:
        database.save_task(task)
    except Exception as exc:
        print(f"[Task] 写入失败 (Station 是否仍在运行?): {exc}", file=sys.stderr)
        return 2
    after = database.get_task(args.task).project_id
    print(f"[Task] {args.task} project_id: {before or '(空)'} -> {after}")
    return 0 if after == args.project else 2


if __name__ == "__main__":
    sys.exit(main())
