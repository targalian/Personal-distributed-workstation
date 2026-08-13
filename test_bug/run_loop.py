#!/usr/bin/env python3
"""
Loop Engineering — 每日验证循环编排器

功能:
  1. 加载配置 (loop_config.yaml)
  2. 运行 API 白盒测试
  3. 对比 test_checklist.csv, 更新状态
  4. 对标记"已修复"的条目进行复测
  5. 生成每日报告 (reports/YYYY-MM-DD.md)
  6. 计算项目健康分数

用法:
  python test_bug/run_loop.py                  # 每日增量 Loop (复测已修复项)
  python test_bug/run_loop.py --full           # 全量扫描
  python test_bug/run_loop.py --retest BUG-001 # 复测指定条目
  python test_bug/run_loop.py --report-only    # 只生成报告不跑测试
  python test_bug/run_loop.py --ui-pending     # 只列出未检测的 UI 改动项
"""
import argparse
import csv
import os
import sys
import time
from datetime import datetime, date
from pathlib import Path

# 确保项目根目录在 sys.path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml

from test_bug.api_tests import APITestSuite, TestResult


# ── 配置加载 ──────────────────────────────────────────────────

def load_config() -> dict:
    config_path = Path(__file__).parent / "loop_config.yaml"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


# ── CSV 读写 ──────────────────────────────────────────────────

CSV_HEADERS = ["编号", "模块", "功能点", "测试类型", "预期行为", "实际表现",
               "严重级别", "状态", "发现日期", "修复日期", "备注"]

STATUS_MAP = {
    "未修复": "未修复",
    "已修复": "已修复",
    "复测通过": "复测通过",
    "复测失败": "复测失败",  # 回归!
    "关闭": "关闭",
    "未检测": "未检测",      # UI 改动待行为检测
    "检测通过": "检测通过",
    "检测失败": "检测失败",  # UI 改动检测发现回归
}


def load_csv(csv_path: Path) -> list[dict]:
    """加载测试清单 CSV (带安全校验防截断)。"""
    if not csv_path.exists():
        return []
    with open(csv_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    # 安全校验: 如果只读到极少行, 可能是编码/格式问题, 返回空避免覆盖
    if 0 < len(rows) < 3:
        print(f"[WARN] CSV 只读到 {len(rows)} 行, 可能格式异常, 跳过更新")
        return []
    return rows


def save_csv(csv_path: Path, rows: list[dict]):
    """保存测试清单 CSV。"""
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS, extrasaction="ignore")
        writer.writeheader()
        # 确保每行都有所有字段
        cleaned = []
        for row in rows:
            cleaned.append({k: row.get(k, "") for k in CSV_HEADERS})
        writer.writerows(cleaned)


def update_csv_from_results(csv_path: Path, results: list[TestResult]) -> dict:
    """根据测试结果更新 CSV 状态。

    Returns:
        {"regressions": [...], "confirmed": [...], "unchanged": [...]}
    """
    rows = load_csv(csv_path)
    if not rows:
        return {"regressions": [], "confirmed": [], "unchanged": []}

    # 建立 bug_id → result 映射
    result_map = {}
    for r in results:
        if r.bug_id.startswith("BUG-"):
            result_map[r.bug_id] = r

    regressions = []
    confirmed = []
    unchanged = []
    today = date.today().isoformat()

    for row in rows:
        bug_id = row.get("编号", "")
        if bug_id not in result_map:
            unchanged.append(bug_id)
            continue

        result = result_map[bug_id]
        old_status = row.get("状态", "")

        if old_status == "已修复":
            # 复测逻辑
            if result.passed:
                row["状态"] = "复测通过"
                row["备注"] = (row.get("备注", "") + f" | 复测通过@{today}").strip(" |")
                confirmed.append(bug_id)
            else:
                row["状态"] = "复测失败"
                row["备注"] = (row.get("备注", "") + f" | 回归!{result.message}@{today}").strip(" |")
                regressions.append(bug_id)
        elif old_status in ("未修复", "复测失败"):
            # 未修复的条目: 如果测试通过了, 可能已被修复但未标记
            if result.passed and "SKIP" not in result.message:
                row["备注"] = (row.get("备注", "") + f" | 测试已通过,建议标记修复@{today}").strip(" |")

    save_csv(csv_path, rows)
    return {"regressions": regressions, "confirmed": confirmed, "unchanged": unchanged}


# ── 报告生成 ──────────────────────────────────────────────────

def generate_report(results: list[TestResult], csv_rows: list[dict],
                    update_info: dict, config: dict) -> str:
    """生成 Markdown 格式的每日报告。"""
    now = datetime.now()
    weights = config.get("severity_weights", {"P0": 100, "P1": 50, "P2": 20, "P3": 5})

    # 统计
    total_bugs = len(csv_rows)
    fixed = sum(1 for r in csv_rows if r.get("状态") in ("已修复", "复测通过", "检测通过"))
    open_bugs = sum(1 for r in csv_rows if r.get("状态") in ("未修复", "复测失败", "检测失败"))
    ui_pending = [r for r in csv_rows if r.get("状态") == "未检测"]
    test_passed = sum(1 for r in results if r.passed)
    test_failed = sum(1 for r in results if not r.passed)
    test_skipped = sum(1 for r in results if "SKIP" in r.message)

    # 健康分数 (加权; 未检测项不计入扣分但单独提示)
    max_score = sum(weights.get(r.get("严重级别", "P3"), 5) for r in csv_rows
                    if r.get("状态") != "未检测")
    penalty = sum(weights.get(r.get("严重级别", "P3"), 5) for r in csv_rows
                  if r.get("状态") in ("未修复", "复测失败", "检测失败"))
    health_score = round((1 - penalty / max(max_score, 1)) * 100, 1)

    lines = [
        f"# Loop Engineering 每日报告",
        f"",
        f"**日期**: {now.strftime('%Y-%m-%d %H:%M:%S')}",
        f"**健康分数**: {health_score}%",
        f"",
        f"## 概览",
        f"",
        f"| 指标 | 数值 |",
        f"|------|------|",
        f"| Bug 总数 | {total_bugs} |",
        f"| 已修复/关闭 | {fixed} |",
        f"| 未修复/回归 | {open_bugs} |",
        f"| 本次测试通过 | {test_passed} |",
        f"| 本次测试失败 | {test_failed} |",
        f"| 跳过 (前置不满足) | {test_skipped} |",
        f"| 复测确认修复 | {len(update_info.get('confirmed', []))} |",
        f"| 回归 (复测失败) | {len(update_info.get('regressions', []))} |",
        f"| UI 改动待检测 | {len(ui_pending)} |",
        f"",
    ]

    # UI 改动待检清单
    if ui_pending:
        lines.append("## 📋 UI 改动待检测")
        lines.append("")
        lines.append("以下 UI 改动尚未做行为检测, 需在浏览器中验证后标记 (ui_change_log.py check):")
        lines.append("")
        for r in ui_pending:
            lines.append(f"- **{r['编号']}** [{r.get('严重级别', '?')}] {r.get('功能点', '')} — {r.get('预期行为', '')}")
        lines.append("")

    # 回归告警
    if update_info.get("regressions"):
        lines.append("## 🚨 回归告警")
        lines.append("")
        for bug_id in update_info["regressions"]:
            row = next((r for r in csv_rows if r.get("编号") == bug_id), {})
            lines.append(f"- **{bug_id}** [{row.get('严重级别', '?')}] {row.get('功能点', '')} — {row.get('备注', '')}")
        lines.append("")

    # 测试详情
    lines.append("## 测试详情")
    lines.append("")
    lines.append("| 编号 | 测试项 | 结果 | 耗时 | 信息 |")
    lines.append("|------|--------|------|------|------|")
    for r in results:
        icon = "✅" if r.passed else "❌"
        skip = " ⏭️" if "SKIP" in r.message else ""
        lines.append(f"| {r.bug_id} | {r.name} | {icon}{skip} | {r.duration_ms:.0f}ms | {r.message[:60]} |")
    lines.append("")

    # 未修复清单 (含 UI 检测失败回归项)
    open_rows = [r for r in csv_rows if r.get("状态") in ("未修复", "复测失败", "检测失败")]
    if open_rows:
        lines.append("## 待修复清单 (按严重级别)")
        lines.append("")
        for level in ["P0", "P1", "P2", "P3"]:
            level_rows = [r for r in open_rows if r.get("严重级别") == level]
            if level_rows:
                lines.append(f"### {level}")
                for r in level_rows:
                    lines.append(f"- **{r['编号']}** {r.get('功能点', '')} — {r.get('实际表现', '')[:80]}")
                lines.append("")

    return "\n".join(lines)


# ── 主流程 ────────────────────────────────────────────────────

def run_loop(full: bool = False, retest_ids: list[str] = None, report_only: bool = False,
             ui_pending_only: bool = False):
    """执行一次 Loop Engineering 循环。"""
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    config = load_config()
    server_cfg = config.get("server", {})
    loop_cfg = config.get("loop", {})

    base_url = server_cfg.get("base_url", "http://localhost:45470")
    api_timeout = server_cfg.get("api_timeout", 15)
    llm_timeout = server_cfg.get("llm_timeout", 90)

    test_bug_dir = Path(__file__).parent
    csv_path = test_bug_dir / loop_cfg.get("csv_file", "test_checklist.csv")
    report_dir = test_bug_dir / loop_cfg.get("report_dir", "reports")
    report_dir.mkdir(parents=True, exist_ok=True)

    today = date.today().isoformat()
    print(f"\n{'=' * 60}")
    print(f"  Loop Engineering - {today}")
    print(f"  Target: {base_url}")
    mode_str = 'FULL SCAN' if full else 'RETEST FIXED' if not retest_ids else 'TARGETED RETEST'
    print(f"  Mode: {mode_str}")
    print(f"{'=' * 60}\n")

    if ui_pending_only:
        # 只列出未检测的 UI 改动项
        from test_bug.ui_change_log import list_pending
        pending = list_pending(load_csv(csv_path) or [])
        if not pending:
            print("[i] 无未检测的 UI 改动项")
            return True
        print(f"未检测 UI 改动 ({len(pending)} 项):")
        for r in pending:
            print(f"  [{r['编号']}] [{r.get('严重级别', '?')}] {r.get('功能点', '')} — {r.get('预期行为', '')}")
        return len(pending) == 0

    if report_only:
        # 只从 CSV 生成报告
        csv_rows = load_csv(csv_path)
        report = generate_report([], csv_rows, {"regressions": [], "confirmed": [], "unchanged": []}, config)
        report_path = report_dir / f"{today}.md"
        report_path.write_text(report, encoding="utf-8")
        print(f"📄 报告已生成: {report_path}")
        return

    # 1. 确定测试范围
    only = None
    if retest_ids:
        only = retest_ids
    elif not full:
        # 增量模式: 只测"已修复"状态的条目
        csv_rows = load_csv(csv_path)
        fixed_ids = [r["编号"] for r in csv_rows if r.get("状态") == "已修复"]
        if not fixed_ids:
            print("[i] No fixed items to retest. Use --full for full scan.")
            # 仍然跑全量 API 测试生成报告
            only = None
        else:
            only = fixed_ids
            print(f"[*] Retest targets: {', '.join(fixed_ids)}")

    # 2. 运行测试
    suite = APITestSuite(base_url, api_timeout, llm_timeout)
    results = suite.run_all(only=only)

    # 3. 更新 CSV
    update_info = update_csv_from_results(csv_path, results)

    # 4. 生成报告
    csv_rows = load_csv(csv_path)  # 重新加载更新后的
    report = generate_report(results, csv_rows, update_info, config)
    report_path = report_dir / f"{today}.md"
    report_path.write_text(report, encoding="utf-8")

    # 5. 输出摘要
    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    print(f"\n{'-' * 60}")
    print(f"  Done: PASS={passed} | FAIL={failed}")
    if update_info["confirmed"]:
        print(f"  [OK] Confirmed fixed: {', '.join(update_info['confirmed'])}")
    if update_info["regressions"]:
        print(f"  [!!] REGRESSION: {', '.join(update_info['regressions'])}")
    print(f"  Report: {report_path}")
    print(f"{'-' * 60}\n")

    return len(update_info["regressions"]) == 0 and failed == 0


def main():
    parser = argparse.ArgumentParser(
        description="LAN Mesh Loop Engineering — 每日验证循环",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python test_bug/run_loop.py                  # 每日增量 (复测已修复项)
  python test_bug/run_loop.py --full           # 全量扫描所有端点
  python test_bug/run_loop.py --retest BUG-001 BUG-004  # 复测指定项
  python test_bug/run_loop.py --report-only    # 只从CSV生成报告
        """,
    )
    parser.add_argument("--full", action="store_true", help="全量扫描 (默认只复测已修复)")
    parser.add_argument("--retest", nargs="*", metavar="BUG_ID", help="复测指定 BUG 编号")
    parser.add_argument("--report-only", action="store_true", help="只生成报告,不运行测试")
    parser.add_argument("--ui-pending", action="store_true", help="只列出未检测的 UI 改动项")
    args = parser.parse_args()

    success = run_loop(full=args.full, retest_ids=args.retest, report_only=args.report_only,
                       ui_pending_only=args.ui_pending)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
