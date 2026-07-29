#!/usr/bin/env python3
"""
Dev Loop Engineering — 开发+测试闭环编排器

将"感知→决策→执行→验证"的开发循环自动化:
  ① 感知: 运行测试Loop, 获取当前失败项和CSV状态
  ② 决策: 按优先级排序, 生成本轮开发任务单
  ③ 执行: 开发者(人/AI)根据任务单修复
  ④ 验证: 修复后复测, 自动更新CSV和loop_status.json

用法:
  python test_bug/dev_loop.py --plan              # 感知+决策: 生成任务单
  python test_bug/dev_loop.py --verify            # 验证: 复测所有"已修复"项
  python test_bug/dev_loop.py --verify BUG-004    # 验证指定Bug
  python test_bug/dev_loop.py --status            # 查看当前Loop状态
  python test_bug/dev_loop.py --close BUG-005     # 手动关闭已确认修复的Bug
  python test_bug/dev_loop.py --cycle             # 完整循环: plan→等待→verify
"""
import argparse
import json
import sys
from datetime import datetime, date
from pathlib import Path

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from test_bug.run_loop import load_config, load_csv, save_csv, CSV_HEADERS
from test_bug.api_tests import APITestSuite, TestResult

# ── 路径常量 ──────────────────────────────────────────────────
TEST_BUG_DIR = Path(__file__).parent
LOOP_STATUS_PATH = PROJECT_ROOT / "loop_status.json"
TASK_SHEET_PATH = TEST_BUG_DIR / "current_tasks.json"

# 优先级权重 (数字越小越优先)
PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


# ── 状态管理 ──────────────────────────────────────────────────

def load_loop_status() -> dict:
    """加载 loop_status.json。"""
    if LOOP_STATUS_PATH.exists():
        with open(LOOP_STATUS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"version": 1, "completed": [], "failed": [], "blocked": [],
            "next_tasks": [], "iteration_count": 0, "total_files_modified": 0}


def save_loop_status(status: dict):
    """保存 loop_status.json。"""
    status["last_run"] = datetime.now().isoformat() + "Z"
    with open(LOOP_STATUS_PATH, "w", encoding="utf-8") as f:
        json.dump(status, f, ensure_ascii=False, indent=2)


# ── ① 感知 ───────────────────────────────────────────────────

def sense(base_url: str, config: dict) -> dict:
    """运行测试, 感知当前项目状态。"""
    print("\n[① 感知] 运行测试Loop...")

    api_timeout = config.get("server", {}).get("api_timeout", 15)
    llm_timeout = config.get("server", {}).get("llm_timeout", 90)

    suite = APITestSuite(base_url, api_timeout, llm_timeout)
    results = suite.run_all()

    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed and "SKIP" not in r.message]
    skipped = [r for r in results if "SKIP" in r.message]

    print(f"    测试完成: PASS={len(passed)} FAIL={len(failed)} SKIP={len(skipped)}")

    return {
        "results": results,
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
    }


# ── ② 决策 ───────────────────────────────────────────────────

def decide(sense_data: dict, csv_rows: list[dict], loop_status: dict) -> list[dict]:
    """根据感知结果, 决策本轮开发任务。"""
    print("\n[② 决策] 生成开发任务单...")

    tasks = []

    # 规则1: 回归最高优先 (复测失败的)
    regressions = [r for r in csv_rows if r.get("状态") == "复测失败"]
    for row in regressions:
        tasks.append({
            "id": row["编号"],
            "priority": "REGRESSION",
            "module": row.get("模块", ""),
            "title": row.get("功能点", ""),
            "description": row.get("实际表现", ""),
            "severity": row.get("严重级别", "P3"),
            "reason": "回归! 之前修复的问题再次出现",
        })

    # 规则2: 测试仍然失败的Bug
    failed_ids = {r.bug_id for r in sense_data["failed"] if r.bug_id.startswith("BUG-")}
    for row in csv_rows:
        bug_id = row.get("编号", "")
        if bug_id in failed_ids and row.get("状态") in ("未修复", "复测失败"):
            if not any(t["id"] == bug_id for t in tasks):
                tasks.append({
                    "id": bug_id,
                    "priority": "TEST_FAILING",
                    "module": row.get("模块", ""),
                    "title": row.get("功能点", ""),
                    "description": row.get("实际表现", ""),
                    "severity": row.get("严重级别", "P3"),
                    "reason": "自动化测试未通过",
                })

    # 规则3: 测试已通过但CSV仍标记"未修复"的 (建议关闭)
    passed_ids = {r.bug_id for r in sense_data["passed"] if r.bug_id.startswith("BUG-")}
    auto_close_candidates = []
    for row in csv_rows:
        bug_id = row.get("编号", "")
        if bug_id in passed_ids and row.get("状态") == "未修复":
            auto_close_candidates.append(bug_id)

    # 规则4: 未修复的Bug按优先级排序 (即使测试通过, 可能是UI类无法自动测的)
    open_bugs = [r for r in csv_rows
                 if r.get("状态") in ("未修复", "复测失败")
                 and not any(t["id"] == r.get("编号", "") for t in tasks)]
    open_bugs.sort(key=lambda r: PRIORITY_ORDER.get(r.get("严重级别", "P3"), 9))

    for row in open_bugs:
        bug_id = row.get("编号", "")
        test_status = "测试已通过(建议确认关闭)" if bug_id in passed_ids else "测试未通过/无法自动测试"
        tasks.append({
            "id": bug_id,
            "priority": "OPEN_BUG",
            "module": row.get("模块", ""),
            "title": row.get("功能点", ""),
            "description": row.get("实际表现", ""),
            "severity": row.get("严重级别", "P3"),
            "reason": test_status,
        })

    # 规则5: 如果Bug都处理完了, 推进新功能
    if not tasks and loop_status.get("next_tasks"):
        for feat_id in loop_status["next_tasks"]:
            tasks.append({
                "id": feat_id,
                "priority": "NEW_FEATURE",
                "module": "feature",
                "title": f"新功能: {feat_id}",
                "description": loop_status.get("notes", ""),
                "severity": "-",
                "reason": "所有Bug已关闭, 推进功能开发",
            })

    # 排序: REGRESSION > TEST_FAILING > OPEN_BUG(按severity) > NEW_FEATURE
    priority_rank = {"REGRESSION": 0, "TEST_FAILING": 1, "OPEN_BUG": 2, "NEW_FEATURE": 3}
    tasks.sort(key=lambda t: (priority_rank.get(t["priority"], 9),
                              PRIORITY_ORDER.get(t["severity"], 9)))

    print(f"    生成 {len(tasks)} 个任务")
    if auto_close_candidates:
        print(f"    💡 可自动关闭 (测试已通过): {', '.join(auto_close_candidates)}")

    return tasks, auto_close_candidates


# ── 任务单输出 ────────────────────────────────────────────────

def print_task_sheet(tasks: list[dict], auto_close: list[str]):
    """打印格式化的任务单。"""
    print(f"\n{'═' * 64}")
    print(f"  📋 开发任务单 — {date.today().isoformat()}")
    print(f"{'═' * 64}")

    if not tasks:
        print("\n  🎉 所有Bug已关闭, 无待办任务!")
        print(f"{'═' * 64}\n")
        return

    # 按优先级分组显示
    groups = {}
    for t in tasks:
        groups.setdefault(t["priority"], []).append(t)

    group_labels = {
        "REGRESSION": "🚨 回归 (立即修复)",
        "TEST_FAILING": "❌ 测试失败 (本轮修复)",
        "OPEN_BUG": "📌 待修复 (按优先级)",
        "NEW_FEATURE": "🚀 新功能",
    }

    idx = 1
    for group_key in ["REGRESSION", "TEST_FAILING", "OPEN_BUG", "NEW_FEATURE"]:
        group_tasks = groups.get(group_key, [])
        if not group_tasks:
            continue
        print(f"\n  {group_labels.get(group_key, group_key)}")
        print(f"  {'─' * 56}")
        for t in group_tasks:
            print(f"  {idx}. [{t['severity']}] {t['id']} — {t['title']}")
            print(f"     模块: {t['module']} | {t['reason']}")
            if t["description"]:
                print(f"     详情: {t['description'][:70]}")
            idx += 1

    if auto_close:
        print(f"\n  💡 建议关闭 (测试已通过, 运行 --close 确认):")
        print(f"     {', '.join(auto_close)}")

    print(f"\n{'═' * 64}")
    print(f"  修复后运行: python test_bug/dev_loop.py --verify")
    print(f"  关闭已修复: python test_bug/dev_loop.py --close BUG-xxx")
    print(f"{'═' * 64}\n")


def save_task_sheet(tasks: list[dict], auto_close: list[str]):
    """保存任务单到 JSON (供其他工具/AI读取)。"""
    sheet = {
        "generated_at": datetime.now().isoformat(),
        "tasks": tasks,
        "auto_close_candidates": auto_close,
        "total": len(tasks),
    }
    with open(TASK_SHEET_PATH, "w", encoding="utf-8") as f:
        json.dump(sheet, f, ensure_ascii=False, indent=2)


# ── ④ 验证 ───────────────────────────────────────────────────

def verify(base_url: str, config: dict, target_ids: list[str] = None) -> bool:
    """验证修复结果: 复测并更新CSV。"""
    print("\n[④ 验证] 复测修复结果...")

    csv_path = TEST_BUG_DIR / config.get("loop", {}).get("csv_file", "test_checklist.csv")
    csv_rows = load_csv(csv_path)

    # 确定复测范围
    if target_ids:
        retest_ids = target_ids
    else:
        # 默认复测所有"已修复"状态的
        retest_ids = [r["编号"] for r in csv_rows if r.get("状态") == "已修复"]
        if not retest_ids:
            print("    没有标记为'已修复'的条目, 改为全量测试")
            retest_ids = None

    api_timeout = config.get("server", {}).get("api_timeout", 15)
    llm_timeout = config.get("server", {}).get("llm_timeout", 90)

    suite = APITestSuite(base_url, api_timeout, llm_timeout)
    results = suite.run_all(only=retest_ids)

    # 更新CSV
    from test_bug.run_loop import update_csv_from_results
    update_info = update_csv_from_results(csv_path, results)

    # 输出结果
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    print(f"\n    复测结果: PASS={passed} FAIL={failed}")

    if update_info["confirmed"]:
        print(f"    ✅ 确认修复: {', '.join(update_info['confirmed'])}")
    if update_info["regressions"]:
        print(f"    🚨 回归: {', '.join(update_info['regressions'])}")

    # 更新 loop_status.json
    loop_status = load_loop_status()
    loop_status["iteration_count"] = loop_status.get("iteration_count", 0) + 1
    if update_info["regressions"]:
        loop_status["failed"] = list(set(loop_status.get("failed", []) + update_info["regressions"]))
    save_loop_status(loop_status)

    success = len(update_info["regressions"]) == 0 and failed == 0
    if success:
        print("    🎉 验证通过, 无回归!")
    else:
        print("    ⚠️  存在问题, 请继续修复后重新验证")

    return success


# ── 关闭Bug ──────────────────────────────────────────────────

def close_bugs(bug_ids: list[str], config: dict):
    """手动关闭已确认修复的Bug。"""
    csv_path = TEST_BUG_DIR / config.get("loop", {}).get("csv_file", "test_checklist.csv")
    csv_rows = load_csv(csv_path)
    today = date.today().isoformat()

    closed = []
    for row in csv_rows:
        if row.get("编号") in bug_ids:
            old_status = row.get("状态", "")
            row["状态"] = "复测通过"
            row["修复日期"] = row.get("修复日期") or today
            row["备注"] = (row.get("备注", "") + f" | 手动关闭@{today}(原:{old_status})").strip(" |")
            closed.append(row["编号"])

    if closed:
        save_csv(csv_path, csv_rows)
        print(f"  ✅ 已关闭: {', '.join(closed)}")
    else:
        print(f"  ⚠️  未找到匹配的Bug编号: {', '.join(bug_ids)}")


# ── 状态查看 ──────────────────────────────────────────────────

def show_status(config: dict):
    """显示当前Loop状态总览。"""
    csv_path = TEST_BUG_DIR / config.get("loop", {}).get("csv_file", "test_checklist.csv")
    csv_rows = load_csv(csv_path)
    loop_status = load_loop_status()

    # 统计
    total = len(csv_rows)
    by_status = {}
    for row in csv_rows:
        s = row.get("状态", "未知")
        by_status[s] = by_status.get(s, 0) + 1

    by_severity = {}
    open_rows = [r for r in csv_rows if r.get("状态") in ("未修复", "复测失败")]
    for row in open_rows:
        sev = row.get("严重级别", "?")
        by_severity[sev] = by_severity.get(sev, 0) + 1

    # 健康分数
    weights = config.get("severity_weights", {"P0": 100, "P1": 50, "P2": 20, "P3": 5})
    max_score = sum(weights.get(r.get("严重级别", "P3"), 5) for r in csv_rows)
    penalty = sum(weights.get(r.get("严重级别", "P3"), 5) for r in open_rows)
    health = round((1 - penalty / max(max_score, 1)) * 100, 1)

    print(f"\n{'═' * 50}")
    print(f"  📊 Dev Loop 状态总览")
    print(f"{'═' * 50}")
    print(f"  健康分数: {health}%")
    print(f"  迭代次数: {loop_status.get('iteration_count', 0)}")
    print(f"  上次运行: {loop_status.get('last_run', 'N/A')}")
    print(f"\n  Bug 统计 (共{total}项):")
    for status, count in sorted(by_status.items()):
        print(f"    {status}: {count}")
    print(f"\n  未修复按级别:")
    for sev in ["P0", "P1", "P2", "P3"]:
        if by_severity.get(sev):
            print(f"    {sev}: {by_severity[sev]}")
    print(f"\n  功能进度: {len(loop_status.get('completed', []))} 完成")
    if loop_status.get("next_tasks"):
        print(f"  下一任务: {', '.join(loop_status['next_tasks'])}")
    print(f"{'═' * 50}\n")


# ── 主入口 ────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Dev Loop Engineering — 开发+测试闭环编排器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python test_bug/dev_loop.py --plan              # 生成开发任务单
  python test_bug/dev_loop.py --verify            # 复测所有已修复项
  python test_bug/dev_loop.py --verify BUG-004    # 复测指定Bug
  python test_bug/dev_loop.py --close BUG-005 BUG-007  # 关闭已确认的Bug
  python test_bug/dev_loop.py --status            # 查看状态
  python test_bug/dev_loop.py --cycle             # 完整循环
        """,
    )
    parser.add_argument("--plan", action="store_true", help="感知+决策: 生成开发任务单")
    parser.add_argument("--verify", nargs="*", metavar="BUG_ID", help="验证修复结果")
    parser.add_argument("--close", nargs="+", metavar="BUG_ID", help="关闭已确认修复的Bug")
    parser.add_argument("--status", action="store_true", help="查看当前Loop状态")
    parser.add_argument("--cycle", action="store_true", help="完整循环: plan→提示→verify")
    args = parser.parse_args()

    config = load_config()
    base_url = config.get("server", {}).get("base_url", "http://localhost:45470")

    # 默认行为: 无参数时等同于 --plan
    if not any([args.plan, args.verify is not None, args.close, args.status, args.cycle]):
        args.plan = True

    if args.status:
        show_status(config)
        return

    if args.close:
        close_bugs(args.close, config)
        return

    if args.plan or args.cycle:
        # ① 感知
        sense_data = sense(base_url, config)
        # ② 决策
        csv_path = TEST_BUG_DIR / config.get("loop", {}).get("csv_file", "test_checklist.csv")
        csv_rows = load_csv(csv_path)
        loop_status = load_loop_status()
        tasks, auto_close = decide(sense_data, csv_rows, loop_status)
        # 输出任务单
        print_task_sheet(tasks, auto_close)
        save_task_sheet(tasks, auto_close)
        print(f"  任务单已保存: {TASK_SHEET_PATH}\n")

        if args.cycle and tasks:
            print("  ─── 修复完成后按 Enter 继续验证 (Ctrl+C 退出) ───")
            try:
                input()
            except (KeyboardInterrupt, EOFError):
                print("\n  已退出, 稍后可运行 --verify 验证")
                return
            # ④ 验证
            verify(base_url, config)
        return

    if args.verify is not None:
        target_ids = args.verify if args.verify else None
        success = verify(base_url, config, target_ids)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
