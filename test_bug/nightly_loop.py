#!/usr/bin/env python3
"""
Loop Engineering — 夜间自动巡检 (定时任务用)

设计为 Windows 任务计划程序调用, 每天凌晨 3:00 自动运行:
  1. 检测 Station Director 是否运行, 未运行则尝试启动
  2. 等待服务就绪
  3. 运行全量 API 白盒测试
  4. 运行前端 UI 静态分析
  5. 更新 test_checklist.csv (复测已修复项)
  6. 生成报告到 reports/
  7. 写入运行日志 logs/

用法 (手动测试):
  python test_bug/nightly_loop.py

由任务计划程序调用 (无需人工干预):
  schtasks /run /tn "LAN_Mesh_Loop_Engineering"
"""
import io
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# 修复 Windows 控制台编码
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

PROJECT_ROOT = Path(__file__).parent.parent
TEST_BUG_DIR = Path(__file__).parent
LOG_DIR = TEST_BUG_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str):
    """带时间戳的日志输出 (同时写文件和控制台)。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    # 追加到当日日志文件
    log_file = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def check_server_running(base_url: str) -> bool:
    """检测 Station Director 是否在运行。"""
    import requests
    try:
        r = requests.get(f"{base_url}/api/health", timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def try_start_server() -> bool:
    """尝试启动 Station Director (后台进程)。"""
    log("Server not running, attempting to start...")
    python_exe = sys.executable
    main_py = PROJECT_ROOT / "main.py"

    if not main_py.exists():
        log(f"ERROR: {main_py} not found, cannot start server")
        return False

    try:
        # 以后台方式启动, 不阻塞本脚本
        subprocess.Popen(
            [python_exe, str(main_py), "station"],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
        )
        log("Server process started, waiting 8s for initialization...")
        time.sleep(8)
        return True
    except Exception as e:
        log(f"ERROR: Failed to start server: {e}")
        return False


def run_api_tests(base_url: str) -> dict:
    """运行 API 白盒测试, 返回结果摘要。"""
    log("Running API whitebox tests...")
    sys.path.insert(0, str(PROJECT_ROOT))
    from test_bug.api_tests import APITestSuite

    suite = APITestSuite(base_url, api_timeout=15, llm_timeout=90)
    results = suite.run_all()

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)
    skipped = sum(1 for r in results if "SKIP" in r.message)

    log(f"API tests done: PASS={passed}, FAIL={failed}, SKIP={skipped}")

    # 记录失败项
    for r in results:
        if not r.passed:
            log(f"  FAIL [{r.bug_id}] {r.name}: {r.message}")

    return {"passed": passed, "failed": failed, "skipped": skipped, "results": results}


def run_ui_tests() -> dict:
    """运行前端 UI 静态分析。"""
    log("Running UI static analysis...")
    from test_bug.ui_tests import UITestSuite, DEFAULT_HTML

    suite = UITestSuite(DEFAULT_HTML)
    results = suite.run_all()

    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])

    log(f"UI tests done: PASS={passed}, FAIL={failed}")
    for r in results:
        if not r["passed"]:
            log(f"  FAIL [{r['id']}] {r['name']}: {r['msg']}")

    return {"passed": passed, "failed": failed, "results": results}


def update_csv_and_report(api_results: dict, base_url: str):
    """更新 CSV 并生成报告。"""
    log("Updating CSV and generating report...")
    from test_bug.run_loop import load_config, update_csv_from_results, generate_report, load_csv

    config = load_config()
    csv_path = TEST_BUG_DIR / config.get("loop", {}).get("csv_file", "test_checklist.csv")
    report_dir = TEST_BUG_DIR / config.get("loop", {}).get("report_dir", "reports")
    report_dir.mkdir(parents=True, exist_ok=True)

    # 更新 CSV
    update_info = update_csv_from_results(csv_path, api_results["results"])

    if update_info["confirmed"]:
        log(f"  Confirmed fixed: {', '.join(update_info['confirmed'])}")
    if update_info["regressions"]:
        log(f"  REGRESSION ALERT: {', '.join(update_info['regressions'])}")

    # 生成报告
    csv_rows = load_csv(csv_path)
    report = generate_report(api_results["results"], csv_rows, update_info, config)
    today = datetime.now().strftime("%Y-%m-%d")
    report_path = report_dir / f"{today}.md"
    report_path.write_text(report, encoding="utf-8")
    log(f"  Report saved: {report_path}")

    return update_info


def main():
    """夜间巡检主流程。"""
    log("=" * 50)
    log("  LAN Mesh Loop Engineering - Nightly Run")
    log("=" * 50)

    # 加载配置
    sys.path.insert(0, str(PROJECT_ROOT))
    from test_bug.run_loop import load_config
    config = load_config()
    base_url = config.get("server", {}).get("base_url", "http://localhost:45470")

    # Step 1: 检测服务是否运行
    if not check_server_running(base_url):
        log("Server is NOT running.")
        started = try_start_server()
        if started:
            # 再次确认
            if not check_server_running(base_url):
                log("ERROR: Server started but health check still fails. Aborting.")
                log("RESULT: ABORTED")
                return 1
            log("Server is now running.")
        else:
            log("Cannot start server. Running UI-only tests.")
            # 只跑 UI 静态测试
            ui_result = run_ui_tests()
            log(f"RESULT: UI_ONLY (server down), UI_FAIL={ui_result['failed']}")
            return 0 if ui_result["failed"] == 0 else 1

    # Step 2: API 测试
    api_result = run_api_tests(base_url)

    # Step 3: UI 测试
    ui_result = run_ui_tests()

    # Step 4: 更新 CSV + 报告
    update_info = update_csv_and_report(api_result, base_url)

    # Step 5: 总结
    total_fail = api_result["failed"] + ui_result["failed"]
    regressions = len(update_info.get("regressions", []))

    log("-" * 50)
    if regressions > 0:
        log(f"RESULT: REGRESSION ({regressions} items)")
    elif total_fail > 0:
        log(f"RESULT: ISSUES_FOUND (api_fail={api_result['failed']}, ui_fail={ui_result['failed']})")
    else:
        log("RESULT: ALL_PASS")
    log("-" * 50)

    return 1 if (regressions > 0 or total_fail > 0) else 0


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code)
