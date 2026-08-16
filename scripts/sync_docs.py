#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""docs/design 模块清单一致性校验/生成器 (D2-docs-sync)。

用途: 让 docs/design 各功能域的「模块清单」表与代码永远一致。
  - check 模式 (默认): 对比代码 docstring 与各 README 的
    <!-- AUTO:module-list --> 区块, 漂移则输出 diff 并退出非零。
    pre-push hook 第 8 项调用本模式, 漂移时阻断上库。
  - --write 模式: 用代码 docstring 重写区块 (修复漂移用);
    遇到「## 模块清单」标题下尚无标记的文档会自动初始化。

映射表 MAPPING 是「文件 → 功能域」的唯一事实源:
  - 增删 lan_mesh 脚本后, 在 MAPPING 登记一行再跑 --write, 清单即同步。
  - 非 Python 条目 (html/ps1/目录) 以元组 (域, 一句话描述) 手写登记。

用法:
  python scripts/sync_docs.py            # 校验 (pre-push 自动调用)
  python scripts/sync_docs.py --write    # 重写/初始化清单区块
"""

import argparse
import ast
import difflib
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DESIGN = ROOT / "docs" / "design"

DOMAINS = [
    "01-network-discovery", "02-station-core", "03-task-orchestration",
    "04-execution-engine", "05-resources-secrets", "06-interaction",
    "07-data-sync", "08-infrastructure", "09-frontend",
    "10-test-loop", "11-scripts-subprojects",
]

# ── 映射表: 文件 → 功能域 (增删脚本时在此登记) ──────────
# py 条目: 字符串值 = 域编号, 描述自动取模块 docstring 首行
# 非 py 条目: 元组 (域编号, 手写一句话描述)
MAPPING = {
    # 01-network-discovery — 发现/协议/认证/重试
    "lan_mesh/discovery.py": "01-network-discovery",
    "lan_mesh/protocol.py": "01-network-discovery",
    "lan_mesh/auth.py": "01-network-discovery",
    "lan_mesh/http_retry.py": "01-network-discovery",
    # 02-station-core — Station 基础设施
    "lan_mesh/station_controller.py": "02-station-core",
    "lan_mesh/station_director.py": "02-station-core",
    "lan_mesh/station_api.py": "02-station-core",
    "lan_mesh/station_routes_common.py": "02-station-core",
    "lan_mesh/station_routes_basic.py": "02-station-core",
    "lan_mesh/station_routes_tasks.py": "02-station-core",
    "lan_mesh/station_routes_resources.py": "02-station-core",
    "lan_mesh/station_routes_pm.py": "02-station-core",
    "lan_mesh/station_routes_chat.py": "02-station-core",
    "lan_mesh/station_routes_projects.py": "02-station-core",
    "lan_mesh/station_routes_worker.py": "02-station-core",
    "lan_mesh/database.py": "02-station-core",
    # 03-task-orchestration — 任务编排与 PM
    "lan_mesh/task.py": "03-task-orchestration",
    "lan_mesh/orchestrator.py": "03-task-orchestration",
    "lan_mesh/pm_agent.py": "03-task-orchestration",
    "lan_mesh/pm_dispatcher.py": "03-task-orchestration",
    "lan_mesh/pm_monitor.py": "03-task-orchestration",
    "lan_mesh/pm_planner.py": "03-task-orchestration",
    "lan_mesh/pm_state.py": "03-task-orchestration",
    "lan_mesh/task_templates.py": "03-task-orchestration",
    "lan_mesh/project.py": "03-task-orchestration",
    # 04-execution-engine — Worker/Agent/工具/技能
    "lan_mesh/worker.py": "04-execution-engine",
    "lan_mesh/agent_runtime.py": "04-execution-engine",
    "lan_mesh/agent_card.py": "04-execution-engine",
    "lan_mesh/agent_prompt.py": "04-execution-engine",
    "lan_mesh/tool_registry.py": "04-execution-engine",
    "lan_mesh/mcp_client.py": "04-execution-engine",
    "lan_mesh/mcp_gateway.py": "04-execution-engine",
    "lan_mesh/sandbox.py": "04-execution-engine",
    "lan_mesh/skill_registry.py": "04-execution-engine",
    # 05-resources-secrets — 模型资源/密钥/版本同步
    "lan_mesh/model_resources.py": "05-resources-secrets",
    "lan_mesh/model_router.py": "05-resources-secrets",
    "lan_mesh/balance_probe.py": "05-resources-secrets",
    "lan_mesh/secret_sync.py": "05-resources-secrets",
    "lan_mesh/version_sync.py": "05-resources-secrets",
    "lan_mesh/collect_config.py": "05-resources-secrets",
    # 06-interaction — 聊天/机器人/角色卡
    "lan_mesh/chat_handler.py": "06-interaction",
    "lan_mesh/bot_gateway.py": "06-interaction",
    "lan_mesh/role_cards.py": "06-interaction",
    # 07-data-sync — 共享/云同步/主机信息
    "lan_mesh/shared_folder.py": "07-data-sync",
    "lan_mesh/cloud_sync.py": "07-data-sync",
    "lan_mesh/host_info.py": "07-data-sync",
    # 08-infrastructure — 配置/日志/事件/评级/自检
    "lan_mesh/config.py": "08-infrastructure",
    "lan_mesh/logger.py": "08-infrastructure",
    "lan_mesh/event_bus.py": "08-infrastructure",
    "lan_mesh/error_tracker.py": "08-infrastructure",
    "lan_mesh/host_rating.py": "08-infrastructure",
    "lan_mesh/preflight.py": "08-infrastructure",
    "lan_mesh/api.py": "08-infrastructure",
    "lan_mesh/worker_routes_basic.py": "08-infrastructure",
    "lan_mesh/worker_routes_pm.py": "08-infrastructure",
    "lan_mesh/worker_routes_p2p.py": "08-infrastructure",
    # 09-frontend — Web UI (非 py 条目)
    "lan_mesh/web/templates/dashboard.html": (
        "09-frontend", "Station Web 控制台 (7 Tab)"),
    "lan_mesh/web/static/": ("09-frontend", "CSS/JS 静态资源"),
    # 10-test-loop — 测试与验证循环
    "tests/test_core.py": "10-test-loop",
    "test_bug/run_loop.py": "10-test-loop",
    "test_bug/dev_loop.py": "10-test-loop",
    "test_bug/nightly_loop.py": "10-test-loop",
    "test_bug/api_tests.py": "10-test-loop",
    "test_bug/ui_tests.py": "10-test-loop",
    "test_bug/discover_tests.py": "10-test-loop",
    "test_bug/ui_change_log.py": "10-test-loop",
    "test_bug/daily_loop.bat": ("10-test-loop", "每日循环定时任务形态 (Windows)"),
    "test_bug/loop_config.yaml": ("10-test-loop", "循环配置"),
    "test_bug/test_checklist.csv": ("10-test-loop", "UI 变更清单 (UI-0xx 编号)"),
    "test_bug/setup_scheduler.bat": ("10-test-loop", "定时任务安装脚本"),
    "loop_status.json": ("10-test-loop", "迭代状态机 (根目录)"),
    ".githooks/": ("10-test-loop", "commit-msg / pre-push / post-merge 钩子"),
    # 11-scripts-subprojects — 运维脚本/技能库/独立子项目
    "scripts/sync_docs.py": "11-scripts-subprojects",
    "scripts/update_version.py": "11-scripts-subprojects",
    "scripts/sync_push.ps1": ("11-scripts-subprojects",
                              "★ 双库同步推送脚本 (上库唯一入口)"),
    "scripts/start_workstation.bat": ("11-scripts-subprojects",
                                      "跨平台一键启动 Station (bat/ps1/sh)"),
    "scripts/start_workstation.ps1": ("11-scripts-subprojects",
                                      "跨平台一键启动 Station (bat/ps1/sh)"),
    "scripts/start_workstation.sh": ("11-scripts-subprojects",
                                     "跨平台一键启动 Station (bat/ps1/sh)"),
    "skills/": ("11-scripts-subprojects",
                "技能库资产 (SKILL.md 格式, 中央分发)"),
    "quicklan-main/": ("11-scripts-subprojects",
                       "独立子项目: Tauri + React 桌面文件共享应用"),
}

AUTO_OPEN = "<!-- AUTO:module-list -->"
AUTO_CLOSE = "<!-- /AUTO:module-list -->"
HEADER = "| 文件/目录 | 职责一句话 |"

# 扫描范围: 这些目录下的 py 文件必须在 MAPPING 登记 (__init__.py 除外)
SCAN_GLOBS = ["lan_mesh/*.py", "tests/*.py", "test_bug/*.py", "scripts/*.py"]


def display_name(path: str) -> str:
    """清单表显示名: lan_mesh 内只显示文件名, 其余保留相对路径。"""
    if path.startswith("lan_mesh/") and "/" not in path[len("lan_mesh/"):]:
        return path.rsplit("/", 1)[-1]
    return path


def extract_doc(path: pathlib.Path):
    """提取模块 docstring 首行; 无 docstring 返回 None。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    doc = ast.get_docstring(tree)
    if not doc:
        return None
    return doc.splitlines()[0].strip()


def find_unmapped() -> list:
    """扫描范围内存在但未在 MAPPING 登记的 py 文件 (增删脚本漏登记检测)。"""
    known = {p for p, v in MAPPING.items() if not isinstance(v, tuple)}
    found = set()
    for glob in SCAN_GLOBS:
        for f in sorted(ROOT.glob(glob)):
            if f.name == "__init__.py":
                continue
            # 统一为相对 ROOT 的 posix 路径, 与 MAPPING 键对齐
            found.add(f.relative_to(ROOT).as_posix())
    return sorted(found - known)


def validate() -> list:
    """结构校验: 未登记/缺 docstring/映射失效 → 返回错误列表。"""
    errors = []
    for path in find_unmapped():
        errors.append(
            f"[unmapped] {path} 未在 sync_docs.py MAPPING 登记, "
            f"增删脚本后需登记一行并 --write 同步清单")
    for path, val in MAPPING.items():
        if isinstance(val, tuple):
            domain = val[0]
        else:
            domain = val
        if domain not in DOMAINS:
            errors.append(f"[bad-domain] {path} → 未知域 {domain}")
        if not isinstance(val, tuple):
            f = ROOT / path
            if not f.exists():
                errors.append(f"[missing] 映射登记的文件不存在: {path}")
            elif extract_doc(f) is None:
                errors.append(f"[no-docstring] {path} 缺模块 docstring, 请补一行说明")
    return errors


def build_tables() -> dict:
    """按域分组, 生成 {域: [(显示名, 描述)]}, 排序稳定。"""
    tables = {d: [] for d in DOMAINS}
    for path, val in MAPPING.items():
        if isinstance(val, tuple):
            domain, desc = val
        else:
            domain = val
            desc = extract_doc(ROOT / path)
        tables[domain].append((display_name(path), desc))
    for domain in tables:
        tables[domain].sort(key=lambda r: r[0].lower())
    return tables


def render(table: list) -> str:
    """渲染 AUTO 区块文本 (含标记)。"""
    lines = [AUTO_OPEN, HEADER, "|---|---|"]
    for name, desc in table:
        desc = (desc or "").replace("|", "\\|")
        lines.append(f"| {name} | {desc} |")
    lines.append(AUTO_CLOSE)
    return "\n".join(lines) + "\n"


def read_raw(path: pathlib.Path):
    """按字节读文件, 返回 (归一化文本, 原是否为 CRLF)。"""
    raw = path.read_bytes().decode("utf-8")
    had_crlf = "\r\n" in raw
    return raw.replace("\r\n", "\n"), had_crlf


def write_raw(path: pathlib.Path, text: str, had_crlf: bool):
    """按字节写回, 保持原换行风格 (只动区块, 不产生整文件 diff)。"""
    out = text.replace("\n", "\r\n") if had_crlf else text
    path.write_bytes(out.encode("utf-8"))


BLOCK_RE = re.compile(
    re.escape(AUTO_OPEN) + r"\n.*?" + re.escape(AUTO_CLOSE), re.S)

# 「## 模块清单」标题后、下一个 ## 或 --- 前的整段 (初始化用)
SECTION_RE = re.compile(
    r"(^## 模块清单[ \t]*\n)(.*?)(?=^## |^---[ \t]*$)", re.S | re.M)


def get_block(text: str):
    """提取现有 AUTO 区块完整文本 (含标记); 无标记返回 None。"""
    m = BLOCK_RE.search(text)
    if not m:
        return None
    return m.group(0)


def init_block(text: str, new_block: str) -> str:
    """在「## 模块清单」标题下初始化 AUTO 区块 (无标记时)。"""
    m = SECTION_RE.search(text)
    if not m:
        raise ValueError("未找到「## 模块清单」标题, 无法初始化")
    return text[:m.start(2)] + "\n" + new_block + "\n" + text[m.end(2):]


def replace_block(text: str, new_block: str) -> str:
    """替换已有 AUTO 区块内容。"""
    return BLOCK_RE.sub(lambda _: new_block.rstrip("\n"), text)


def run_check() -> int:
    """校验模式: 结构错误与漂移都报错退出非零。"""
    errors = validate()
    if errors:
        print("[CHECK FAIL] 结构问题 (先修复再校验):")
        for e in errors:
            print(f"  {e}")
        return 1
    tables = build_tables()
    drift = 0
    for domain in DOMAINS:
        readme = DESIGN / domain / "README.md"
        if not readme.exists():
            print(f"[missing] 域文档不存在: {readme.as_posix()}")
            drift += 1
            continue
        text, _ = read_raw(readme)
        current = get_block(text)
        expected = render(tables[domain]).rstrip("\n")
        if current is None:
            print(f"[DRIFT] {readme.as_posix()}: 缺少 {AUTO_OPEN} 标记, "
                  f"请运行 --write 初始化")
            drift += 1
            continue
        if current.rstrip("\n") != expected:
            drift += 1
            print(f"[DRIFT] {readme.as_posix()}")
            diff = difflib.unified_diff(
                current.rstrip("\n").splitlines(), expected.splitlines(),
                fromfile="文档现有", tofile="代码生成", lineterm="")
            print("\n".join(diff))
    if drift:
        print(f"\n共 {drift} 处漂移 → 运行 python scripts/sync_docs.py --write 修复")
        return 1
    print("[PASS] docs/design 模块清单与代码一致 "
          f"({sum(len(v) for v in tables.values())} 条目, {len(DOMAINS)} 域)")
    return 0


def run_write() -> int:
    """重写/初始化所有域的 AUTO 区块。"""
    errors = validate()
    if errors:
        print("[WRITE FAIL] 结构问题 (先修复再写):")
        for e in errors:
            print(f"  {e}")
        return 1
    tables = build_tables()
    for domain in DOMAINS:
        readme = DESIGN / domain / "README.md"
        if not readme.exists():
            print(f"[missing] 域文档不存在, 跳过: {readme.as_posix()}")
            continue
        text, had_crlf = read_raw(readme)
        new_block = render(tables[domain]).rstrip("\n")
        if get_block(text) is None:
            text = init_block(text, new_block)
            print(f"[INIT] {readme.as_posix()}")
        else:
            old = get_block(text)
            if old.rstrip("\n") == new_block:
                print(f"[SAME] {readme.as_posix()}")
                continue
            text = replace_block(text, new_block)
            print(f"[WRITE] {readme.as_posix()}")
        write_raw(readme, text, had_crlf)
    print("[DONE] --write 完成, 请复核 git diff 后提交")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="用代码 docstring 重写/初始化清单区块")
    args = parser.parse_args()
    try:
        return run_write() if args.write else run_check()
    except ValueError as e:
        print(f"[FAIL] {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
