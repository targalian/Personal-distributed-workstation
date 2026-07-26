"""
测试项自动推导工具 — 从源码中机械式提取所有可测试点

原理: 扫描后端路由 + 前端事件 + WebSocket 类型, 自动生成测试项候选清单
用法: python test_bug/discover_tests.py
"""
import re
from pathlib import Path

ROOT = Path(__file__).parent.parent


def discover_api_endpoints():
    """① 从后端代码提取所有 API 端点。"""
    api_file = ROOT / "lan_mesh" / "station_api.py"
    code = api_file.read_text(encoding="utf-8")
    routes = re.findall(r'@router\.(get|post|put|delete)\("([^"]+)"', code)
    return [(method.upper(), path) for method, path in routes]


def discover_ui_interactions():
    """② 从前端 HTML 提取所有用户交互点。"""
    html_file = ROOT / "lan_mesh" / "web" / "templates" / "dashboard.html"
    html = html_file.read_text(encoding="utf-8")

    # onclick 事件
    onclicks = re.findall(r'onclick="(\w+)\(', html)
    # onchange 事件
    onchanges = re.findall(r'onchange="(\w+)\(', html)
    # onkeydown 事件
    onkeys = re.findall(r'onkeydown="[^"]*?(\w+)\(', html)
    # Tab 切换
    tabs = re.findall(r'data-tab="(\w+)"', html)

    return {
        "onclick": sorted(set(onclicks)),
        "onchange": sorted(set(onchanges)),
        "onkeydown": sorted(set(onkeys)),
        "tabs": sorted(set(tabs)),
    }


def discover_ws_types():
    """③ 从后端代码提取所有 WebSocket 广播类型。"""
    api_file = ROOT / "lan_mesh" / "station_api.py"
    code = api_file.read_text(encoding="utf-8")
    # _broadcast(state, "type_name", ...)
    types = re.findall(r'_broadcast\(state,\s*"(\w+)"', code)
    return sorted(set(types))


def discover_failure_modes(endpoint_count, interaction_count):
    """④ 对每个交互点套用常见故障模式。"""
    return [
        "超时: 请求无响应, 前端无 AbortController",
        "重复: HTTP + WebSocket 双重渲染",
        "吞错: catch 内部消化异常, 用户无感知",
        "状态不一致: 两个数据源返回矛盾结论",
        "竞态: 定时刷新覆盖用户操作",
        "断连: WebSocket 断开后消息丢失",
        "边界: 空数据/超长输入/特殊字符",
        "权限: Secretary 未激活时访问受保护端点",
    ]


def main():
    import io, sys
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    print("=" * 60)
    print("  Test Item Discovery - Auto-extracting testable points")
    print("=" * 60)

    # ① API 端点
    endpoints = discover_api_endpoints()
    print(f"\n[1] API Endpoints ({len(endpoints)}):")
    print("-" * 40)
    for method, path in endpoints:
        print(f"    {method:6s} {path}")

    # ② UI 交互
    interactions = discover_ui_interactions()
    total_ui = sum(len(v) for v in interactions.values())
    print(f"\n[2] UI Interactions ({total_ui}):")
    print("-" * 40)
    for category, items in interactions.items():
        print(f"    {category} ({len(items)}): {', '.join(items[:8])}{'...' if len(items) > 8 else ''}")

    # ③ WebSocket 类型
    ws_types = discover_ws_types()
    print(f"\n[3] WebSocket Broadcast Types ({len(ws_types)}):")
    print("-" * 40)
    for t in ws_types:
        print(f"    {t}")

    # ④ 故障模式
    modes = discover_failure_modes(len(endpoints), total_ui)
    print(f"\n[4] Failure Modes (applied to each interaction):")
    print("-" * 40)
    for m in modes:
        print(f"    - {m}")

    # 汇总
    print(f"\n{'=' * 60}")
    print(f"  Summary:")
    print(f"    API endpoints:      {len(endpoints)}")
    print(f"    UI interactions:    {total_ui}")
    print(f"    WS message types:   {len(ws_types)}")
    print(f"    Failure modes:      {len(modes)}")
    print(f"    ---")
    print(f"    Max test items:     {len(endpoints) + total_ui + len(ws_types)} (coverage)")
    print(f"    With failure modes: {(len(endpoints) + total_ui) * len(modes)} (exhaustive)")
    print(f"    Recommended:        20-30 (risk-based sampling)")
    print(f"{'=' * 60}")

    # 推导逻辑说明
    print(f"""
  How test items are derived:
  ┌─────────────────────────────────────────────────────┐
  │  Step 1: Enumerate all endpoints (mechanical)       │
  │  Step 2: Enumerate all UI click handlers            │
  │  Step 3: Enumerate all WS message types             │
  │  Step 4: For each item, apply failure modes:        │
  │          - Does it have timeout protection?         │
  │          - Can it produce duplicate rendering?       │
  │          - Does error get swallowed silently?        │
  │          - Is there a consistency guarantee?         │
  │  Step 5: Prioritize by user impact (P0-P3)          │
  │  Step 6: Write assertion (expected vs actual)       │
  └─────────────────────────────────────────────────────┘
""")


if __name__ == "__main__":
    main()
