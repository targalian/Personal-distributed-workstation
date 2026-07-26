"""
Loop Engineering — 自动化白盒 API 测试

对 LAN Mesh Station Director 的各 API 端点进行白盒验证。
每个测试函数对应 test_checklist.csv 中的一个或多个 BUG 条目。

用法:
  python -m test_bug.api_tests                    # 运行全部
  python -m test_bug.api_tests --base-url http://192.168.1.100:45470
  python -m test_bug.api_tests --only BUG-001     # 只测指定编号
"""
import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import requests


@dataclass
class TestResult:
    """单项测试结果。"""
    bug_id: str
    name: str
    passed: bool
    message: str = ""
    duration_ms: float = 0
    response_code: Optional[int] = None
    timestamp: float = field(default_factory=time.time)


class APITestSuite:
    """LAN Mesh 白盒 API 测试套件。"""

    def __init__(self, base_url: str, api_timeout: int = 15, llm_timeout: int = 90):
        self.base = base_url.rstrip("/")
        self.timeout = api_timeout
        self.llm_timeout = llm_timeout
        self.results: list[TestResult] = []
        self._secretary_active = False

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _get(self, path: str, **kw) -> requests.Response:
        return requests.get(self._url(path), timeout=kw.pop("timeout", self.timeout), **kw)

    def _post(self, path: str, **kw) -> requests.Response:
        return requests.post(self._url(path), timeout=kw.pop("timeout", self.timeout), **kw)

    def _delete(self, path: str, **kw) -> requests.Response:
        return requests.delete(self._url(path), timeout=kw.pop("timeout", self.timeout), **kw)

    def _put(self, path: str, **kw) -> requests.Response:
        return requests.put(self._url(path), timeout=kw.pop("timeout", self.timeout), **kw)

    def _record(self, bug_id: str, name: str, passed: bool, msg: str = "", code: int = None, t0: float = 0):
        self.results.append(TestResult(
            bug_id=bug_id, name=name, passed=passed,
            message=msg, response_code=code,
            duration_ms=round((time.time() - t0) * 1000, 1) if t0 else 0,
        ))

    # ═══════════════════════════════════════════════════════════
    #  基础连通性
    # ═══════════════════════════════════════════════════════════

    def test_health(self):
        """前置检查: 服务是否可达。"""
        t0 = time.time()
        try:
            r = self._get("/api/health", timeout=5)
            ok = r.status_code == 200 and r.json().get("status") == "ok"
            self._secretary_active = r.json().get("secretary_active", False)
            self._record("SYS", "服务健康检查", ok, f"status={r.status_code}", r.status_code, t0)
            return ok
        except Exception as e:
            self._record("SYS", "服务健康检查", False, f"无法连接: {e}", None, t0)
            return False

    # ═══════════════════════════════════════════════════════════
    #  Station Director 面板
    # ═══════════════════════════════════════════════════════════

    def test_station_fleet(self):
        """BUG-005: Station 舰队 API 可达且返回结构正确。"""
        t0 = time.time()
        try:
            r = self._get("/api/station/fleet")
            d = r.json()
            ok = r.status_code == 200 and "hosts" in d and "online" in d and "tiers" in d
            self._record("BUG-005", "Station舰队API结构", ok,
                         f"hosts={len(d.get('hosts', []))}, online={d.get('online', 0)}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-005", "Station舰队API结构", False, str(e), None, t0)

    def test_station_events(self):
        """Station 事件流 API。"""
        t0 = time.time()
        try:
            r = self._get("/api/station/events?limit=10")
            d = r.json()
            ok = r.status_code == 200 and "events" in d
            self._record("BUG-017", "Station事件流API", ok, f"events={len(d.get('events', []))}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-017", "Station事件流API", False, str(e), None, t0)

    def test_secretary_status_consistency(self):
        """BUG-004: 两个 Secretary 状态端点一致性。"""
        t0 = time.time()
        try:
            r1 = self._get("/api/station/roles")
            r2 = self._get("/api/station/secretary-status")
            d1, d2 = r1.json(), r2.json()
            active_from_roles = (d1.get("secretary") == "active")
            active_from_status = d2.get("active", False)
            ok = active_from_roles == active_from_status
            msg = f"roles={active_from_roles}, secretary-status={active_from_status}"
            self._record("BUG-004", "Secretary状态双源一致性", ok, msg, r1.status_code, t0)
        except Exception as e:
            self._record("BUG-004", "Secretary状态双源一致性", False, str(e), None, t0)

    def test_recompute_ratings(self):
        """BUG-017: 重新评级 API 可调用。"""
        t0 = time.time()
        try:
            r = self._post("/api/station/rate")
            d = r.json()
            ok = r.status_code == 200 and "updated" in d
            self._record("BUG-017b", "重新评级API", ok, f"updated={d.get('updated', 0)}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-017b", "重新评级API", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  主机管理
    # ═══════════════════════════════════════════════════════════

    def test_hosts_list(self):
        """主机列表 API。"""
        t0 = time.time()
        try:
            r = self._get("/api/hosts")
            d = r.json()
            ok = r.status_code == 200 and "hosts" in d and "total" in d
            self._record("BUG-024", "主机列表API", ok, f"total={d.get('total', 0)}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-024", "主机列表API", False, str(e), None, t0)

    def test_network_info(self):
        """BUG-007: 网络信息 API 返回完整。"""
        t0 = time.time()
        try:
            r = self._get("/api/network")
            d = r.json()
            ok = r.status_code == 200 and "udp_port" in d and "local_ips" in d and "broadcast_targets" in d
            self._record("BUG-007", "网络信息API完整性", ok, f"ips={len(d.get('local_ips', []))}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-007", "网络信息API完整性", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  Secretary 聊天 (需 Secretary 激活)
    # ═══════════════════════════════════════════════════════════

    def test_chat_send(self):
        """BUG-001/018: 秘书聊天 API 可正常回复。"""
        if not self._secretary_active:
            self._record("BUG-001", "秘书聊天API", True, "SKIP: Secretary未激活")
            return
        t0 = time.time()
        try:
            r = self._post("/api/secretary/chat",
                           json={"message": "当前状态如何?"},
                           timeout=self.llm_timeout)
            d = r.json()
            ok = r.status_code == 200 and "reply" in d and len(d.get("reply", "")) > 0
            self._record("BUG-001", "秘书聊天API回复", ok,
                         f"reply_len={len(d.get('reply', ''))}", r.status_code, t0)
        except requests.Timeout:
            self._record("BUG-018", "秘书聊天超时保护", False, f"超过{self.llm_timeout}s未响应", None, t0)
        except Exception as e:
            self._record("BUG-001", "秘书聊天API回复", False, str(e), None, t0)

    def test_chat_history(self):
        """BUG-023: 聊天历史 API 结构正确。"""
        if not self._secretary_active:
            self._record("BUG-023", "聊天历史API", True, "SKIP: Secretary未激活")
            return
        t0 = time.time()
        try:
            r = self._get("/api/secretary/chat/history?limit=10")
            d = r.json()
            ok = r.status_code == 200 and "history" in d and isinstance(d["history"], list)
            # 验证无重复 (相邻两条不应完全相同)
            history = d.get("history", [])
            has_dup = False
            for i in range(1, len(history)):
                if (history[i].get("content") == history[i-1].get("content")
                        and history[i].get("role") == history[i-1].get("role")):
                    has_dup = True
                    break
            msg = f"history_len={len(history)}, has_adjacent_dup={has_dup}"
            self._record("BUG-023", "聊天历史无重复", ok and not has_dup, msg, r.status_code, t0)
        except Exception as e:
            self._record("BUG-023", "聊天历史API", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  任务管理 (需 Secretary 激活)
    # ═══════════════════════════════════════════════════════════

    def test_tasks_list(self):
        """任务列表 API。"""
        if not self._secretary_active:
            self._record("BUG-008", "任务列表API", True, "SKIP: Secretary未激活")
            return
        t0 = time.time()
        try:
            r = self._get("/api/tasks")
            d = r.json()
            ok = r.status_code == 200 and "tasks" in d
            self._record("BUG-008", "任务列表API", ok, f"tasks={len(d.get('tasks', []))}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-008", "任务列表API", False, str(e), None, t0)

    def test_task_dag(self):
        """BUG-009: DAG 图 API (需要有任务)。"""
        if not self._secretary_active:
            self._record("BUG-009", "DAG图API", True, "SKIP: Secretary未激活")
            return
        t0 = time.time()
        try:
            # 先获取任务列表
            r = self._get("/api/tasks")
            tasks = r.json().get("tasks", [])
            if not tasks:
                self._record("BUG-009", "DAG图API", True, "SKIP: 无任务可测DAG")
                return
            task_id = tasks[0]["task_id"]
            r2 = self._get(f"/api/tasks/{task_id}/graph")
            if r2.status_code == 404:
                self._record("BUG-009", "DAG图API", True, "SKIP: 任务无DAG图")
                return
            d = r2.json()
            ok = r2.status_code == 200 and "nodes" in d and "edges" in d
            # 检查节点是否有合理坐标
            nodes = d.get("nodes", [])
            all_zero = all(n.get("x", 0) == 0 and n.get("y", 0) == 0 for n in nodes) if nodes else False
            msg = f"nodes={len(nodes)}, edges={len(d.get('edges', []))}, all_at_origin={all_zero}"
            self._record("BUG-009", "DAG图API+布局", ok, msg, r2.status_code, t0)
        except Exception as e:
            self._record("BUG-009", "DAG图API", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  PM Agent / 团队
    # ═══════════════════════════════════════════════════════════

    def test_pm_agents(self):
        """BUG-012: PM Agent 列表 API。"""
        if not self._secretary_active:
            self._record("BUG-012", "PM Agent列表API", True, "SKIP: Secretary未激活")
            return
        t0 = time.time()
        try:
            r = self._get("/api/pm")
            d = r.json()
            ok = r.status_code == 200 and "pm_agents" in d
            self._record("BUG-012", "PM Agent列表API", ok, f"pm_count={len(d.get('pm_agents', []))}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-012", "PM Agent列表API", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  Agent 管理
    # ═══════════════════════════════════════════════════════════

    def test_agents_list(self):
        """Agent 列表 API。"""
        if not self._secretary_active:
            self._record("BUG-014", "Agent列表API", True, "SKIP: Secretary未激活")
            return
        t0 = time.time()
        try:
            r = self._get("/api/agents")
            d = r.json()
            ok = r.status_code == 200 and "agents" in d
            self._record("BUG-014", "Agent列表API", ok, f"agents={d.get('total', 0)}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-014", "Agent列表API", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  项目管理
    # ═══════════════════════════════════════════════════════════

    def test_projects_list(self):
        """BUG-020: 项目列表 API + 预算字段精度。"""
        if not self._secretary_active:
            self._record("BUG-020", "项目列表API", True, "SKIP: Secretary未激活")
            return
        t0 = time.time()
        try:
            r = self._get("/api/projects")
            d = r.json()
            ok = r.status_code == 200 and "projects" in d
            projects = d.get("projects", [])
            # 检查预算字段存在
            budget_ok = True
            for p in projects:
                if "budget_limit_usd" not in p or "budget_used_usd" not in p:
                    budget_ok = False
                    break
            self._record("BUG-020", "项目列表+预算字段", ok and budget_ok,
                         f"projects={len(projects)}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-020", "项目列表API", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  MCP 工具
    # ═══════════════════════════════════════════════════════════

    def test_tools_list(self):
        """MCP 工具列表 API。"""
        if not self._secretary_active:
            self._record("BUG-014b", "MCP工具列表API", True, "SKIP: Secretary未激活")
            return
        t0 = time.time()
        try:
            r = self._get("/tools/list")
            d = r.json()
            ok = r.status_code == 200 and "tools" in d
            self._record("BUG-014b", "MCP工具列表API", ok,
                         f"tools={len(d.get('tools', []))}, servers={len(d.get('servers', []))}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-014b", "MCP工具列表API", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  技能库
    # ═══════════════════════════════════════════════════════════

    def test_skills_list(self):
        """BUG-013: 技能列表 API。"""
        t0 = time.time()
        try:
            r = self._get("/api/station/skills")
            ok = r.status_code == 200 and isinstance(r.json(), list)
            skills = r.json() if ok else []
            self._record("BUG-013", "技能列表API", ok, f"skills={len(skills)}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-013", "技能列表API", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  Bot 通道
    # ═══════════════════════════════════════════════════════════

    def test_bot_channels(self):
        """BUG-014c: Bot 通道列表 API。"""
        t0 = time.time()
        try:
            r = self._get("/api/station/bot/channels")
            ok = r.status_code == 200 and isinstance(r.json(), list)
            channels = r.json() if ok else []
            self._record("BUG-014c", "Bot通道列表API", ok, f"channels={len(channels)}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-014c", "Bot通道列表API", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  P2P 主机通讯
    # ═══════════════════════════════════════════════════════════

    def test_p2p_messages(self):
        """BUG-015: P2P 消息 API (无目标时返回空)。"""
        t0 = time.time()
        try:
            # 用一个不存在的 device_id 测试 404 处理
            r = self._get("/api/p2p/messages/nonexistent-device")
            # 应该返回空消息列表或 404, 不应 500
            ok = r.status_code in (200, 404)
            self._record("BUG-015", "P2P消息API容错", ok, f"status={r.status_code}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-015", "P2P消息API容错", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  WebSocket 连通性
    # ═══════════════════════════════════════════════════════════

    def test_websocket(self):
        """BUG-016: WebSocket 端点可连接。"""
        t0 = time.time()
        try:
            import websocket
            ws_url = self.base.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
            ws = websocket.create_connection(ws_url, timeout=5)
            ws.close()
            self._record("BUG-016", "WebSocket可连接", True, "连接成功", 200, t0)
        except ImportError:
            # websocket-client 未安装, 用 HTTP 升级请求验证
            try:
                r = requests.get(self._url("/ws"), headers={
                    "Upgrade": "websocket", "Connection": "Upgrade",
                    "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
                    "Sec-WebSocket-Version": "13",
                }, timeout=5)
                # 101 = 升级成功; 403/426 也说明端点存在
                ok = r.status_code in (101, 403, 426, 200)
                self._record("BUG-016", "WebSocket端点存在", ok, f"status={r.status_code}", r.status_code, t0)
            except Exception as e:
                self._record("BUG-016", "WebSocket端点存在", False, str(e), None, t0)
        except Exception as e:
            self._record("BUG-016", "WebSocket可连接", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  Web UI 可达性
    # ═══════════════════════════════════════════════════════════

    def test_web_ui(self):
        """Web UI 首页可访问。"""
        t0 = time.time()
        try:
            r = self._get("/")
            ok = r.status_code == 200 and "LAN Mesh" in r.text
            self._record("BUG-005b", "Web UI首页可达", ok, f"html_len={len(r.text)}", r.status_code, t0)
        except Exception as e:
            self._record("BUG-005b", "Web UI首页可达", False, str(e), None, t0)

    # ═══════════════════════════════════════════════════════════
    #  运行入口
    # ═══════════════════════════════════════════════════════════

    def run_all(self, only: list[str] = None) -> list[TestResult]:
        """运行全部测试 (或指定 bug_id 列表)。"""
        all_tests = [
            self.test_health,
            self.test_station_fleet,
            self.test_station_events,
            self.test_secretary_status_consistency,
            self.test_recompute_ratings,
            self.test_hosts_list,
            self.test_network_info,
            self.test_chat_send,
            self.test_chat_history,
            self.test_tasks_list,
            self.test_task_dag,
            self.test_pm_agents,
            self.test_agents_list,
            self.test_projects_list,
            self.test_tools_list,
            self.test_skills_list,
            self.test_bot_channels,
            self.test_p2p_messages,
            self.test_websocket,
            self.test_web_ui,
        ]

        # 健康检查必须先行
        if not self.test_health():
            print("❌ 服务不可达, 中止测试。请确认 Station Director 已启动。")
            return self.results

        for test_fn in all_tests[1:]:  # 跳过已执行的 health
            if only:
                # 检查该测试是否关联指定的 bug_id
                # 通过执行后检查 results 中是否有匹配的 bug_id
                before = len(self.results)
                test_fn()
                after_results = self.results[before:]
                if not any(r.bug_id in only for r in after_results):
                    # 移除不相关的结果
                    self.results = self.results[:before]
            else:
                test_fn()

        return self.results


def main():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

    parser = argparse.ArgumentParser(description="LAN Mesh Loop Engineering - API 白盒测试")
    parser.add_argument("--base-url", default="http://localhost:45470", help="Station Director 地址")
    parser.add_argument("--timeout", type=int, default=15, help="API 超时(秒)")
    parser.add_argument("--llm-timeout", type=int, default=90, help="LLM 接口超时(秒)")
    parser.add_argument("--only", nargs="*", help="只测指定 BUG 编号 (如 BUG-001 BUG-004)")
    parser.add_argument("--json", action="store_true", help="输出 JSON 格式")
    args = parser.parse_args()

    suite = APITestSuite(args.base_url, args.timeout, args.llm_timeout)
    results = suite.run_all(only=args.only)

    if args.json:
        print(json.dumps([{
            "bug_id": r.bug_id, "name": r.name, "passed": r.passed,
            "message": r.message, "duration_ms": r.duration_ms,
            "response_code": r.response_code,
        } for r in results], ensure_ascii=False, indent=2))
    else:
        # 人类可读输出
        passed = sum(1 for r in results if r.passed)
        failed = sum(1 for r in results if not r.passed)
        skipped = sum(1 for r in results if "SKIP" in r.message)
        print(f"\n{'=' * 60}")
        print(f"  LAN Mesh Loop Engineering - Whitebox Test Report")
        print(f"  Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Target: {args.base_url}")
        print(f"{'=' * 60}\n")

        for r in results:
            icon = "PASS" if r.passed else "FAIL"
            skip = " [SKIP]" if "SKIP" in r.message else ""
            print(f"  [{icon:4s}] [{r.bug_id:10s}] {r.name:24s} {r.duration_ms:6.0f}ms  {r.message}{skip}")

        print(f"\n{'-' * 60}")
        print(f"  Total: {len(results)} | PASS: {passed} | FAIL: {failed} | SKIP: {skipped}")
        score = round(passed / max(len(results) - skipped, 1) * 100, 1)
        print(f"  Health Score: {score}%")
        print(f"{'-' * 60}\n")

    sys.exit(0 if all(r.passed for r in results) else 1)


if __name__ == "__main__":
    main()
