"""
Loop Engineering — 前端 UI 静态白盒验证

不依赖浏览器, 直接分析 dashboard.html 的结构完整性:
  - 所有 Tab 对应的 Panel 是否存在
  - 所有 onclick 引用的函数是否在 <script> 中定义
  - 所有 getElementById 引用的 ID 是否在 HTML 中声明
  - WebSocket 消息类型是否有对应处理
  - 移动端导航覆盖率

用法:
  python test_bug/ui_tests.py
  python test_bug/ui_tests.py --html lan_mesh/web/templates/dashboard.html
"""
import argparse
import re
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_HTML = PROJECT_ROOT / "lan_mesh" / "web" / "templates" / "dashboard.html"


class UITestSuite:
    """前端 HTML/JS 静态分析测试。"""

    def __init__(self, html_path: Path):
        self.html_path = html_path
        self.html = ""
        self.script = ""
        self.results: list[dict] = []

    def load(self) -> bool:
        if not self.html_path.exists():
            self.results.append({"id": "UI-000", "name": "HTML文件存在", "passed": False, "msg": str(self.html_path)})
            return False
        self.html = self.html_path.read_text(encoding="utf-8")
        # 提取 <script> 内容
        m = re.search(r"<script>(.*?)</script>", self.html, re.DOTALL)
        self.script = m.group(1) if m else ""
        self.results.append({"id": "UI-000", "name": "HTML文件存在", "passed": True, "msg": f"{len(self.html)} chars"})
        return True

    def _record(self, uid: str, name: str, passed: bool, msg: str = ""):
        self.results.append({"id": uid, "name": name, "passed": passed, "msg": msg})

    # ── 测试项 ──────────────────────────────────────────────────

    def test_tab_panel_mapping(self):
        """UI-001: 所有 data-tab 都有对应 panel-{tab}。"""
        tabs = set(re.findall(r'data-tab="(\w+)"', self.html))
        panels = set(re.findall(r'id="panel-(\w+)"', self.html))
        missing = tabs - panels
        self._record("UI-001", "Tab→Panel映射完整",
                     len(missing) == 0,
                     f"tabs={len(tabs)}, panels={len(panels)}, missing={missing or '无'}")

    def test_onclick_functions_defined(self):
        """UI-002: 所有 onclick 中的函数在 script 中有定义。"""
        # 提取 onclick="funcName(...)" 中的函数名
        onclick_fns = set(re.findall(r'onclick="(\w+)\(', self.html))
        # 提取 script 中定义的函数 (function xxx / async function xxx / const xxx=)
        defined_fns = set(re.findall(r'(?:async\s+)?function\s+(\w+)', self.script))
        defined_fns |= set(re.findall(r'(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(', self.script))
        # 也检查箭头函数赋值
        defined_fns |= set(re.findall(r'(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:\([^)]*\)|[^=])\s*=>', self.script))

        missing = onclick_fns - defined_fns
        # 排除内联表达式 (如 event.stopPropagation) 和浏览器内置函数
        builtin_fns = {"fetch", "alert", "confirm", "prompt", "setTimeout", "clearTimeout",
                       "requestAnimationFrame", "encodeURIComponent", "parseInt", "parseFloat"}
        missing = {f for f in missing if "." not in f and f not in builtin_fns}
        self._record("UI-002", "onclick函数均已定义",
                     len(missing) == 0,
                     f"onclick引用={len(onclick_fns)}, 未定义={missing or '无'}")

    def test_getelementbyid_exists(self):
        """UI-003: JS 中 getElementById 引用的 ID 在 HTML 中存在。"""
        # 从 script 中提取 getElementById('xxx') 和 getElementById("xxx")
        js_ids = set(re.findall(r"getElementById\(['\"]([^'\"]+)['\"]\)", self.script))
        # 从 HTML 中提取所有 id="xxx"
        html_ids = set(re.findall(r'id="([^"]+)"', self.html))
        # 动态生成的 ID (如 typing-indicator) 可能在 JS 中创建
        dynamic_ids = {"typing-indicator"}  # 已知动态 ID

        missing = js_ids - html_ids - dynamic_ids
        self._record("UI-003", "getElementById引用均存在",
                     len(missing) == 0,
                     f"JS引用={len(js_ids)}, HTML声明={len(html_ids)}, 缺失={missing or '无'}")

    def test_ws_message_handlers(self):
        """UI-004: WebSocket onmessage 处理了所有后端广播类型。"""
        # 从 script 中提取 msg.type==='xxx' 的类型
        handled_types = set(re.findall(r"msg\.type==='(\w+)'", self.script))
        # 后端已知广播类型 (从 station_api.py 分析)
        known_types = {
            "hosts", "heartbeat", "host_registered",
            "task_submitted", "task_updated",
            "agent_registered",
            "project_created", "project_updated", "project_archived",
            "secretary_activated", "secretary_deactivated",
            "secretary_assigned", "secretary_revoked",
            "skill_assigned", "skill_revoked", "skills_scanned",
            "chat_reply",
            "pm_registered", "pm_status_change", "team_update", "progress_report",
            "p2p_chat",
        }
        unhandled = known_types - handled_types
        self._record("UI-004", "WS消息类型全覆盖",
                     len(unhandled) == 0,
                     f"已处理={len(handled_types)}, 未处理={unhandled or '无'}")

    def test_mobile_nav_coverage(self):
        """UI-021: 移动端底部导航覆盖所有 Tab。"""
        # 桌面端 tabs
        desktop_tabs = set(re.findall(r'class="tab[^"]*"[^>]*data-tab="(\w+)"', self.html))
        # 移动端 nav items
        mobile_tabs = set(re.findall(r'class="mobile-nav-item[^"]*"[^>]*data-tab="(\w+)"', self.html))
        missing = desktop_tabs - mobile_tabs
        self._record("UI-021", "移动端导航覆盖率",
                     len(missing) == 0,
                     f"桌面={len(desktop_tabs)}, 移动={len(mobile_tabs)}, 缺失={missing or '无'}")

    def test_showtoast_duration_param(self):
        """UI-003b: showToast 函数签名是否支持 duration 参数。"""
        # 检查 showToast 定义
        match = re.search(r'function\s+showToast\s*\(([^)]*)\)', self.script)
        if match:
            params = match.group(1)
            has_duration = "duration" in params or "timeout" in params or "ms" in params
            # 检查是否有调用传了3个参数
            three_arg_calls = re.findall(r"showToast\([^,]+,[^,]+,[^)]+\)", self.script)
            if three_arg_calls and not has_duration:
                self._record("UI-003b", "showToast duration参数",
                             False,
                             f"有{len(three_arg_calls)}处传了3参数但函数不支持")
            else:
                self._record("UI-003b", "showToast duration参数", True, "参数一致")
        else:
            self._record("UI-003b", "showToast duration参数", False, "未找到showToast定义")

    def test_fetch_error_handling(self):
        """UI-002b: fetch 调用是否有 catch 处理。"""
        # 统计 fetch 调用数和 catch 数
        fetch_count = len(re.findall(r'\bfetch\s*\(', self.script))
        catch_count = len(re.findall(r'\.catch\s*\(|catch\s*\(', self.script))
        # 理想情况: catch >= fetch (有些 fetch 在 try-catch 中)
        ratio = catch_count / max(fetch_count, 1)
        self._record("UI-002b", "fetch错误处理覆盖率",
                     ratio >= 0.7,
                     f"fetch={fetch_count}, catch={catch_count}, ratio={ratio:.0%}")

    def test_no_alert_in_async_flow(self):
        """UI-006: 异步操作后是否仍使用 alert/prompt (应改为 Modal)。"""
        alert_count = len(re.findall(r'\balert\s*\(', self.script))
        prompt_count = len(re.findall(r'\bprompt\s*\(', self.script))
        confirm_count = len(re.findall(r'\bconfirm\s*\(', self.script))
        total_native = alert_count + prompt_count + confirm_count
        # 不强制为0 (confirm 用于危险操作确认是合理的), 但 prompt 应为0
        self._record("UI-006", "原生对话框使用量",
                     prompt_count == 0,
                     f"alert={alert_count}, prompt={prompt_count}, confirm={confirm_count}")

    # ── 运行 ────────────────────────────────────────────────────

    def run_all(self) -> list[dict]:
        if not self.load():
            return self.results
        self.test_tab_panel_mapping()
        self.test_onclick_functions_defined()
        self.test_getelementbyid_exists()
        self.test_ws_message_handlers()
        self.test_mobile_nav_coverage()
        self.test_showtoast_duration_param()
        self.test_fetch_error_handling()
        self.test_no_alert_in_async_flow()
        return self.results


def main():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description="LAN Mesh 前端 UI 静态白盒验证")
    parser.add_argument("--html", type=str, default=str(DEFAULT_HTML), help="dashboard.html 路径")
    args = parser.parse_args()

    suite = UITestSuite(Path(args.html))
    results = suite.run_all()

    print(f"\n{'=' * 60}")
    print(f"  Frontend UI Static Whitebox Verification")
    print(f"  File: {args.html}")
    print(f"{'=' * 60}\n")

    for r in results:
        icon = "PASS" if r["passed"] else "FAIL"
        print(f"  [{icon:4s}] [{r['id']:8s}] {r['name']:28s} {r['msg']}")

    passed = sum(1 for r in results if r["passed"])
    print(f"\n  Total: {len(results)} | PASS: {passed} | FAIL: {len(results) - passed}")

    # UI 改动待检提醒 (静态分析无法覆盖行为回归, 需人工/浏览器验证)
    try:
        from test_bug.ui_change_log import list_pending
        pending = list_pending()
        if pending:
            print(f"\n  📋 未检测 UI 改动 ({len(pending)} 项, 需在浏览器中验证后标记):")
            for p in pending:
                print(f"     [{p['编号']}] {p.get('功能点', '')} — {p.get('预期行为', '')}")
    except Exception:
        pass
    print()

    sys.exit(0 if all(r["passed"] for r in results) else 1)


if __name__ == "__main__":
    main()
