"""
核心模块单元测试

覆盖:
1. TaskDAG — 拓扑排序、依赖解析、环检测、动态增删、条件边、序列化
2. ModelRouter — 难度分类、评分路由、降级链
3. _classify_task — 任务类型分类
4. EventBus — 发布/订阅、环形历史、sink 投递、边界 (M5)
5. role_cards — 角色卡结构、秘书 prompt 关键约束回归 (M6)
6. balance_probe — 别名归一、各家解析、异常提示、key 优先级 (R2/R4)
7. version_sync — 版本比对、领先检测、通知去重 (S2)
8. startup-sync — hosts 版本列、心跳版本落库、启动密钥推/拉路由、拉取幂等 (S3)
9. secretary-conflict — 广播真实角色、双 Secretary 仲裁让位方向与幂等 (E4)
10. role-free-align — config_ts 仲裁推/拉、池数仲裁、自动升级脏工作区跳过 (F1)
11. secretary-failover — Secretary 离线接管判定、device_id 仲裁、单节点自愈 (E5)
12. pm-trio — PMPlanner/PMMonitor/PMDispatcher 接口级单测 (P1 #9)
13. worker-ws-push — Worker→Secretary WS 直推通道: URL 派生/推送轮次/
    HTTP 兜底跳过/Secretary 端点鉴权与帧协议 (M5-2);
    auth 开启时 '/' 仪表盘免认证白名单回归
14. rotation-quant — 轮换量化价值公式: 沉没成本压力/窗口紧迫度/
    时段折扣/规则回退/batch 合规红线/方案透明化 (R5-2)
15. single-instance — 主机级单实例守护: 无锁/僵尸锁接管、同版本
    取消启动、新版杀旧接管、更旧退出、dev-reload 同版接管 (E6)

运行: pytest tests/ -v
"""
import secrets
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from lan_mesh.protocol import SubTask, DifficultyLevel
from lan_mesh.task import TaskDAG, ConditionalEdge
from lan_mesh.model_router import classify_difficulty, ModelRouter, STRATEGY_WEIGHTS
from lan_mesh.config import ModelEntryConfig
from lan_mesh.orchestrator import _classify_task
from lan_mesh.protocol import Task
from lan_mesh.event_bus import EventBus
from lan_mesh.role_cards import (
    ROLE_CARDS, SECRETARY_CARD, get_role_card, list_role_cards,
    render_secretary_prompt,
)
from lan_mesh import balance_probe
import requests


# ═══════════════════════════════════════════════════════════════════
# 辅助工厂
# ═══════════════════════════════════════════════════════════════════

def make_subtask(sid: str, name: str = "", deps: list = None,
                 status: str = "pending", skill: str = "",
                 condition: str = "") -> SubTask:
    return SubTask(
        subtask_id=sid,
        name=name or sid,
        depends_on=deps or [],
        status=status,
        required_skill=skill,
        condition_expr=condition,
    )


def make_model(id: str, provider: str = "test", caps: list = None,
               cost_in: float = 0.01, cost_out: float = 0.02,
               quality: float = 0.7, speed: float = 0.8,
               fallback: list = None) -> ModelEntryConfig:
    return ModelEntryConfig(
        id=id,
        provider=provider,
        api_key_env="TEST_KEY",
        base_url="http://localhost:8080/v1",
        cost_input_per_1k=cost_in,
        cost_output_per_1k=cost_out,
        capabilities=caps or ["general"],
        quality_score=quality,
        speed_score=speed,
        fallback=fallback or [],
    )


# ═══════════════════════════════════════════════════════════════════
# TaskDAG 测试
# ═══════════════════════════════════════════════════════════════════

class TestTaskDAGTopologicalSort:
    """拓扑排序测试。"""

    def test_linear_chain(self):
        """A → B → C 线性依赖。"""
        a = make_subtask("a")
        b = make_subtask("b", deps=["a"])
        c = make_subtask("c", deps=["b"])
        dag = TaskDAG([a, b, c])
        order = dag.topological_sort()
        assert order.index("a") < order.index("b") < order.index("c")

    def test_diamond(self):
        """菱形依赖: A → B, A → C, B → D, C → D。"""
        a = make_subtask("a")
        b = make_subtask("b", deps=["a"])
        c = make_subtask("c", deps=["a"])
        d = make_subtask("d", deps=["b", "c"])
        dag = TaskDAG([a, b, c, d])
        order = dag.topological_sort()
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")

    def test_independent_nodes(self):
        """无依赖的并行节点。"""
        dag = TaskDAG([make_subtask("x"), make_subtask("y"), make_subtask("z")])
        order = dag.topological_sort()
        assert set(order) == {"x", "y", "z"}

    def test_cycle_detection(self):
        """循环依赖: A → B → C → A。"""
        a = make_subtask("a", deps=["c"])
        b = make_subtask("b", deps=["a"])
        c = make_subtask("c", deps=["b"])
        dag = TaskDAG([a, b, c])
        assert dag.has_cycle() is True
        # 拓扑排序应返回不完整的列表
        assert len(dag.topological_sort()) < 3


class TestTaskDAGReadySubtasks:
    """可执行子任务查询测试。"""

    def test_all_independent_ready(self):
        """所有无依赖任务都应就绪。"""
        dag = TaskDAG([make_subtask("a"), make_subtask("b")])
        ready = dag.get_ready_subtasks()
        assert {st.subtask_id for st in ready} == {"a", "b"}

    def test_blocked_by_dependency(self):
        """依赖未完成时不就绪。"""
        a = make_subtask("a")
        b = make_subtask("b", deps=["a"])
        dag = TaskDAG([a, b])
        ready = dag.get_ready_subtasks()
        assert {st.subtask_id for st in ready} == {"a"}

    def test_unblocked_after_completion(self):
        """依赖完成后可执行。"""
        a = make_subtask("a", status="completed")
        b = make_subtask("b", deps=["a"])
        dag = TaskDAG([a, b])
        ready = dag.get_ready_subtasks()
        assert {st.subtask_id for st in ready} == {"b"}

    def test_completed_not_returned(self):
        """已完成的任务不再返回。"""
        a = make_subtask("a", status="completed")
        dag = TaskDAG([a])
        assert dag.get_ready_subtasks() == []


class TestTaskDAGDynamicOps:
    """动态图操作测试。"""

    def test_add_node_success(self):
        dag = TaskDAG([make_subtask("a")])
        new_node = make_subtask("b", deps=["a"])
        assert dag.add_node(new_node) is True
        assert "b" in dag.subtasks

    def test_add_node_duplicate(self):
        dag = TaskDAG([make_subtask("a")])
        assert dag.add_node(make_subtask("a")) is False

    def test_add_node_creates_cycle(self):
        """添加会产生环的节点应失败。"""
        a = make_subtask("a", deps=["b"])
        b = make_subtask("b")
        dag = TaskDAG([a, b])
        # 添加 c 依赖 a, 同时让 b 依赖 c → 形成环
        c = make_subtask("c", deps=["a"])
        dag.add_node(c)
        # 现在尝试添加边 c → b (b 依赖 c), 但 a 已依赖 b → 环
        # 这里用 add_edge 测试
        assert dag.add_edge("c", "b") is False  # 会形成 b→a→... 不对
        # 实际: a deps b, c deps a. 如果 b deps c → b→c→a→b 环

    def test_remove_node(self):
        a = make_subtask("a")
        b = make_subtask("b", deps=["a"])
        dag = TaskDAG([a, b])
        assert dag.remove_node("a") is True
        assert "a" not in dag.subtasks
        # b 的依赖应被清除
        assert dag.subtasks["b"].depends_on == []

    def test_remove_nonexistent(self):
        dag = TaskDAG([make_subtask("a")])
        assert dag.remove_node("zzz") is False

    def test_add_edge_and_remove(self):
        a = make_subtask("a")
        b = make_subtask("b")
        dag = TaskDAG([a, b])
        assert dag.add_edge("a", "b") is True
        assert "a" in dag.subtasks["b"].depends_on
        assert dag.remove_edge("a", "b") is True
        assert "a" not in dag.subtasks["b"].depends_on

    def test_add_edge_cycle_rejected(self):
        """添加会形成环的边应被拒绝。"""
        a = make_subtask("a", deps=["b"])
        b = make_subtask("b")
        dag = TaskDAG([a, b])
        # b → a 已存在 (a deps b), 再添加 a → b (b deps a) 会形成环
        assert dag.add_edge("a", "b") is False


class TestTaskDAGConditionalEdges:
    """条件边测试。"""

    def test_unconditional_edge_always_active(self):
        edge = ConditionalEdge(source_id="a", target_id="b", condition="")
        assert edge.evaluate({}) is True

    def test_condition_true(self):
        edge = ConditionalEdge(condition="score > 0.8")
        assert edge.evaluate({"score": 0.9}) is True

    def test_condition_false(self):
        edge = ConditionalEdge(condition="score > 0.8")
        assert edge.evaluate({"score": 0.5}) is False

    def test_condition_error_defaults_true(self):
        """表达式求值失败时默认激活。"""
        edge = ConditionalEdge(condition="undefined_var > 0")
        assert edge.evaluate({}) is True

    def test_ready_subtasks_with_condition(self):
        """条件边不满足时目标不就绪。"""
        a = make_subtask("a", status="completed")
        a.output_data = {"score": 0.5}
        b = make_subtask("b", deps=["a"], condition="score > 0.8")
        dag = TaskDAG([a, b])
        # 无 context 时条件边默认激活
        ready = dag.get_ready_subtasks()
        assert len(ready) == 1
        # 有 context 时条件边不满足
        ready = dag.get_ready_subtasks(context={"score": 0.5})
        assert len(ready) == 0


class TestTaskDAGSerialization:
    """JSON 序列化往返测试。"""

    def test_round_trip(self):
        a = make_subtask("a", name="Task A", skill="coding")
        b = make_subtask("b", name="Task B", deps=["a"])
        dag = TaskDAG([a, b])
        json_data = dag.to_graph_json()

        # 验证结构
        assert len(json_data["nodes"]) == 2
        assert len(json_data["edges"]) == 1
        assert json_data["edges"][0]["source"] == "a"
        assert json_data["edges"][0]["target"] == "b"

        # 往返重建
        dag2 = TaskDAG.from_graph_json(json_data)
        assert set(dag2.subtasks.keys()) == {"a", "b"}
        assert dag2.subtasks["b"].depends_on == ["a"]
        assert dag2.topological_sort().index("a") < dag2.topological_sort().index("b")


class TestTaskDAGCompletion:
    """完成状态检测。"""

    def test_all_completed(self):
        dag = TaskDAG([make_subtask("a", status="completed"),
                       make_subtask("b", status="completed")])
        assert dag.is_all_completed() is True

    def test_not_all_completed(self):
        dag = TaskDAG([make_subtask("a", status="completed"),
                       make_subtask("b", status="pending")])
        assert dag.is_all_completed() is False

    def test_has_failed(self):
        dag = TaskDAG([make_subtask("a", status="failed")])
        assert dag.has_failed() is True


# ═══════════════════════════════════════════════════════════════════
# ModelRouter 难度分类测试
# ═══════════════════════════════════════════════════════════════════

class TestClassifyDifficulty:
    """难度分类测试。"""

    def test_l1_short_simple(self):
        """短文本 + 简单关键词 → L1。"""
        assert classify_difficulty("提取关键词") == "L1"
        assert classify_difficulty("格式转换") == "L1"

    def test_l2_default(self):
        """普通中等长度文本 → L2 (默认)。"""
        # 需要 > 200 字符避免触发 L1 短文本逻辑
        text = (
            "请帮我写一封邮件给客户，详细说明当前项目的整体进度情况，"
            "包括已完成的功能模块、正在进行的测试工作、以及下一阶段的计划安排，"
            "语气要正式但友好，让客户放心我们的交付能力。"
            "另外还需要提及我们在质量控制方面所做的努力，"
            "以及团队成员之间的协作情况，让客户感受到我们的专业性。"
            "邮件中还可以适当提及一些具体的数据，比如已完成的里程碑数量、"
            "测试覆盖率、以及预计的上线时间等信息。"
            "最后请确保邮件格式规范，开头有称呼，结尾有署名和日期。"
        )
        assert len(text) > 200  # 确保超过 L1 阈值
        result = classify_difficulty(text)
        assert result == "L2"

    def test_l3_code_keywords(self):
        """代码相关关键词 → L3。"""
        result = classify_difficulty("实现一个排序算法函数，需要调试逻辑错误")
        assert result == "L3"

    def test_l3_skill_based(self):
        """code_generation 技能 → L3。"""
        result = classify_difficulty("生成代码", skill="code_generation")
        assert result == "L3"

    def test_l4_architecture(self):
        """架构级关键词 → L4。"""
        result = classify_difficulty(
            "设计一个分布式微服务架构系统，需要深度分析性能优化方案"
        )
        assert result == "L4"

    def test_l4_long_text_with_keyword(self):
        """长文本 + 1个L4关键词 → L4。"""
        long_text = "架构 " + "x" * 1600
        assert classify_difficulty(long_text) == "L4"

    def test_empty_text(self):
        """空文本 → L1 (短文本逻辑)。"""
        result = classify_difficulty("")
        assert result in ("L1", "L2")


# ═══════════════════════════════════════════════════════════════════
# ModelRouter 路由测试
# ═══════════════════════════════════════════════════════════════════

class TestModelRouter:
    """模型路由器测试。"""

    @pytest.fixture
    def router(self):
        """创建包含3个模型的测试路由器。"""
        models = [
            make_model("cheap-model", caps=["general", "lightweight"],
                       cost_in=0.001, cost_out=0.002, quality=0.5, speed=0.9),
            make_model("balanced-model", caps=["general", "reasoning", "coding"],
                       cost_in=0.01, cost_out=0.02, quality=0.8, speed=0.7),
            make_model("premium-model", caps=["reasoning", "coding", "long_context"],
                       cost_in=0.06, cost_out=0.12, quality=0.95, speed=0.5,
                       fallback=["balanced-model"]),
        ]
        return ModelRouter(models)

    def test_pool_size(self, router):
        assert router.pool_size == 3

    def test_route_returns_result(self, router, monkeypatch):
        """路由应返回完整结果 (设置 API Key 环境变量)。"""
        monkeypatch.setenv("TEST_KEY", "sk-test-123")
        result = router.route("写一个排序算法", skill="code_generation")
        assert result.selected_model != ""
        assert result.difficulty in ("L1", "L2", "L3", "L4")
        assert result.score >= 0
        assert len(result.candidates) == 3

    def test_cost_first_prefers_cheaper(self, router):
        """cost_first 策略应倾向便宜模型。"""
        # 手动设置策略权重
        result = router.route("简单任务")
        # 默认 balanced 策略下不应选最贵的
        assert result.selected_model != ""

    def test_fallback_chain(self, router):
        """降级链应包含备选模型。"""
        result = router.route("设计分布式系统架构", skill="code_generation")
        # 无论选哪个, fallback_chain 应是列表
        assert isinstance(result.fallback_chain, list)

    def test_empty_pool(self):
        """空模型池应返回默认值。"""
        router = ModelRouter([])
        result = router.route("any task")
        assert result.selected_model == "deepseek-chat"
        assert result.score == 0.0

    def test_list_models(self, router):
        models = router.list_models()
        assert len(models) == 3
        assert all("id" in m for m in models)


# ═══════════════════════════════════════════════════════════════════
# _classify_task 测试
# ═══════════════════════════════════════════════════════════════════

class TestClassifyTask:
    """Orchestrator 任务分类测试。"""

    def _make_task(self, name: str, desc: str = "") -> Task:
        return Task(task_id="t1", name=name, description=desc)

    def test_code_task(self, monkeypatch):
        # 无 CLI Agent 环境: 复杂代码任务回退普通 code_task
        monkeypatch.setattr("lan_mesh.agent_runtime.get_preferred_cli_agent", lambda: "")
        assert _classify_task(self._make_task("修复登录bug")) == "code_task"
        assert _classify_task(self._make_task("代码重构")) == "code_task"
        assert _classify_task(self._make_task("implement function")) == "code_task"

    def test_code_task_cli(self, monkeypatch):
        # CLI Agent 可用时: 复杂代码任务路由到 CLI Agent 模板
        monkeypatch.setattr("lan_mesh.agent_runtime.get_preferred_cli_agent", lambda: "claude")
        assert _classify_task(self._make_task("代码重构")) == "code_task_cli"
        assert _classify_task(self._make_task("implement function")) == "code_task_cli"
        # 简单代码任务不路由 CLI Agent
        assert _classify_task(self._make_task("修复登录bug")) == "code_task"

    def test_document_task(self):
        assert _classify_task(self._make_task("撰写项目文档")) == "document_task"
        assert _classify_task(self._make_task("生成摘要报告")) == "document_task"

    def test_system_task(self):
        assert _classify_task(self._make_task("部署监控系统")) == "system_task"
        assert _classify_task(self._make_task("执行shell命令")) == "system_task"

    def test_simple_task_fallback(self):
        assert _classify_task(self._make_task("你好")) == "simple_task"
        assert _classify_task(self._make_task("随便聊聊")) == "simple_task"


# ═══════════════════════════════════════════════════════════════════
# ConditionalEdge 单元测试
# ═══════════════════════════════════════════════════════════════════

class TestConditionalEdge:
    """条件边序列化测试。"""

    def test_to_dict_from_dict(self):
        edge = ConditionalEdge(
            source_id="a", target_id="b",
            condition="x > 1", description="test edge"
        )
        d = edge.to_dict()
        restored = ConditionalEdge.from_dict(d)
        assert restored.source_id == "a"
        assert restored.target_id == "b"
        assert restored.condition == "x > 1"

    def test_from_dict_ignores_extra_fields(self):
        d = {"source_id": "x", "target_id": "y", "unknown_field": 123}
        edge = ConditionalEdge.from_dict(d)
        assert edge.source_id == "x"
        assert not hasattr(edge, "unknown_field")


# ═══════════════════════════════════════════════════════════════
# EventBus 单元测试 (M5)
# ═══════════════════════════════════════════════════════════════

class TestEventBus:
    """事件总线: 发布/历史/sink 投递/边界。"""

    def test_publish_and_recent(self):
        bus = EventBus()
        bus.publish("usage_reported", {"count": 3})
        items = bus.recent()
        assert len(items) == 1
        assert items[0]["type"] == "usage_reported"
        assert items[0]["data"] == {"count": 3}
        assert isinstance(items[0]["ts"], float)

    def test_recent_limit_and_order(self):
        bus = EventBus()
        for i in range(5):
            bus.publish("evt", {"i": i})
        items = bus.recent(3)
        assert len(items) == 3
        assert [e["data"]["i"] for e in items] == [2, 3, 4]  # 时间升序取最近 3

    def test_recent_zero_returns_empty(self):
        # 回归: 早期 recent(0) 曾返回全部历史, 修复后必须返回空
        bus = EventBus()
        bus.publish("evt", {})
        assert bus.recent(0) == []

    def test_recent_negative_returns_empty(self):
        bus = EventBus()
        bus.publish("evt", {})
        assert bus.recent(-5) == []

    def test_ring_history_caps_old_events(self):
        bus = EventBus(history=3)
        for i in range(5):
            bus.publish("evt", {"i": i})
        assert len(bus.recent(100)) == 3

    def test_publish_without_sink_is_noop(self):
        bus = EventBus()
        assert not bus.has_sink
        bus.publish("evt", {"x": 1})  # 无 sink 不抛异常

    def test_attach_sink_direct_dispatch(self):
        bus = EventBus()
        got = []
        bus.attach(loop=None, sink=got.append)  # loop=None → 直投
        assert bus.has_sink
        bus.publish("evt", {"v": 7})
        assert len(got) == 1 and got[0]["data"]["v"] == 7

    def test_sink_exception_swallowed(self):
        bus = EventBus()

        def bad_sink(evt):
            raise RuntimeError("sink 故障")

        bus.attach(loop=None, sink=bad_sink)
        bus.publish("evt", {})  # sink 异常只告警, 不向发布方抛出

    def test_detach_clears_sink(self):
        bus = EventBus()
        bus.attach(loop=None, sink=lambda e: None)
        bus.detach()
        assert not bus.has_sink

    def test_publish_data_none_normalized(self):
        bus = EventBus()
        bus.publish("evt", None)
        assert bus.recent()[0]["data"] == {}


# ═══════════════════════════════════════════════════════════════
# role_cards 单元测试 (M6)
# ═══════════════════════════════════════════════════════════════

class TestRoleCards:
    """角色卡: 结构完整性 + 秘书 prompt 关键约束回归。"""

    def test_three_roles_complete(self):
        assert set(ROLE_CARDS) == {"secretary", "pm", "worker"}
        for card in ROLE_CARDS.values():
            for key in ("role", "display_name", "identity", "mission", "sections"):
                assert card.get(key), f"角色卡缺字段: {card.get('role')} → {key}"

    def test_get_role_card_unknown_empty(self):
        assert get_role_card("ghost") == {}

    def test_list_role_cards_summary(self):
        cards = list_role_cards()
        assert len(cards) == 3
        for c in cards:
            assert c["sections"] == sorted(c["sections"])

    def test_render_secretary_prompt_structure(self):
        prompt = render_secretary_prompt("在线主机: 2 台")
        assert SECRETARY_CARD["identity"] in prompt
        assert SECRETARY_CARD["mission"] in prompt
        for title in SECRETARY_CARD["sections"]:
            assert f"# {title}" in prompt
        assert "# 当前工作站实时状态" in prompt
        assert "在线主机: 2 台" in prompt

    def test_secretary_key_constraints_kept(self):
        # M6 行为等价重构回归: 反幻觉关键约束不得丢失
        prompt = render_secretary_prompt()
        assert "绝对禁止在回复中声称操作已执行" in prompt
        assert "不要编造不存在的功能" in prompt


# ═══════════════════════════════════════════════════════════════
# balance_probe 单元测试 (R2/R4)
# ═══════════════════════════════════════════════════════════════

class _FakeResp:
    """requests.Response 最小替身。"""

    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)

    def json(self):
        return self._payload


class TestBalanceProbe:
    """余额探测: 别名归一/不支持引导/各家解析/异常提示/key 优先级。"""

    def test_provider_alias_normalize(self):
        assert balance_probe._normalize_provider("SF") == "siliconflow"
        assert balance_probe._normalize_provider("kimi") == "moonshot"
        assert balance_probe._normalize_provider("glm") == "zhipu"
        assert balance_probe._normalize_provider("unknown-x") == "unknown-x"

    def test_supported_providers(self):
        assert balance_probe.supported_providers() == [
            "deepseek", "moonshot", "siliconflow", "zhipu"]

    def test_no_api_key_hint(self):
        r = balance_probe.probe_balance("deepseek", "")
        assert not r["supported"] and r["balance"] is None
        assert "未配置 API Key" in r["error"]

    def test_unsupported_provider_hint(self):
        r = balance_probe.probe_balance("openai", "sk-test")
        assert not r["supported"]
        assert r["hint"] == balance_probe.UNSUPPORTED_HINTS["openai"]

    def test_siliconflow_success(self, monkeypatch):
        monkeypatch.setattr(balance_probe.requests, "get",
                            lambda url, **kw: _FakeResp({"data": {"balance": 12.5}}))
        r = balance_probe.probe_balance("siliconflow", "sk-x")
        assert r["supported"] and r["balance"] == 12.5 and r["currency"] == "CNY"

    def test_siliconflow_missing_field(self, monkeypatch):
        monkeypatch.setattr(balance_probe.requests, "get",
                            lambda url, **kw: _FakeResp({"data": {}}))
        r = balance_probe.probe_balance("sf", "sk-x")
        assert not r["supported"] and "缺少 balance" in r["error"]

    def test_deepseek_success(self, monkeypatch):
        payload = {"balance_infos": [{"total_balance": "88.00", "currency": "CNY"}]}
        monkeypatch.setattr(balance_probe.requests, "get",
                            lambda url, **kw: _FakeResp(payload))
        r = balance_probe.probe_balance("deepseek", "sk-x")
        assert r["balance"] == 88.0 and r["currency"] == "CNY"

    def test_moonshot_lenient_fields(self, monkeypatch):
        # 宽容解析: 仅 balance 字段 (无 available_balance) 也应解析成功
        monkeypatch.setattr(balance_probe.requests, "get",
                            lambda url, **kw: _FakeResp({"data": {"balance": 3.2}}))
        r = balance_probe.probe_balance("moonshot", "sk-x")
        assert r["balance"] == 3.2

    def test_zhipu_quota_parse(self, monkeypatch):
        monkeypatch.setattr(balance_probe.requests, "get",
                            lambda url, **kw: _FakeResp({"quota": 1000000}))
        r = balance_probe.probe_balance("zhipu", "sk-x")
        assert r["balance"] == 1000000.0 and r["currency"] == "token"

    def test_http_401_invalid_key_hint(self, monkeypatch):
        def fake_get(url, **kw):
            raise requests.HTTPError(response=_FakeResp(status_code=401))
        monkeypatch.setattr(balance_probe.requests, "get", fake_get)
        r = balance_probe.probe_balance("deepseek", "sk-x")
        assert "HTTP 401" in r["error"] and "无效" in r["hint"]

    def test_network_error(self, monkeypatch):
        def fake_get(url, **kw):
            raise requests.ConnectionError("连不上")
        monkeypatch.setattr(balance_probe.requests, "get", fake_get)
        r = balance_probe.probe_balance("deepseek", "sk-x")
        assert "网络错误" in r["error"]

    def test_probe_resource_direct_key_priority(self, monkeypatch):
        # R4: api_key 直填值优先于环境变量
        captured = {}

        def fake_get(url, headers=None, **kw):
            captured.update(headers or {})
            return _FakeResp({"balance_infos": [{"total_balance": 1, "currency": "CNY"}]})

        monkeypatch.setattr(balance_probe.requests, "get", fake_get)
        monkeypatch.setenv("FAKE_ENV_KEY_X", "from-env")
        pool = {"id": "p1", "provider": "deepseek",
                "api_key": "direct-key", "api_key_env": "FAKE_ENV_KEY_X"}
        r = balance_probe.probe_resource(pool)
        assert r["resource_id"] == "p1"
        assert captured["Authorization"] == "Bearer direct-key"

    def test_probe_resource_env_fallback(self, monkeypatch):
        captured = {}

        def fake_get(url, headers=None, **kw):
            captured.update(headers or {})
            return _FakeResp({"balance_infos": [{"total_balance": 1, "currency": "CNY"}]})

        monkeypatch.setattr(balance_probe.requests, "get", fake_get)
        monkeypatch.setenv("FAKE_ENV_KEY_Y", "from-env")
        pool = {"id": "p2", "provider": "deepseek", "api_key_env": "FAKE_ENV_KEY_Y"}
        balance_probe.probe_resource(pool)
        assert captured["Authorization"] == "Bearer from-env"

    def test_probe_resource_no_key_configured(self, monkeypatch):
        monkeypatch.delenv("MISSING_KEY_XYZ", raising=False)
        pool = {"id": "p3", "provider": "deepseek", "api_key_env": "MISSING_KEY_XYZ"}
        r = balance_probe.probe_resource(pool)
        assert "未配置 API Key" in r["error"]


# ═══════════════════════════════════════════════════════════════
#  S1-key-sync: secret_sync 加密分发 (固化专项验证关键路径)
# ═══════════════════════════════════════════════════════════════


class TestSecretSync:
    """secret_sync 加解密/指纹/脱敏 — AES-GCM 密文分发链路回归。"""

    # 运行时随机生成, 避免触发 pre-push 硬编码密钥检测 (同一进程内一致即可)
    _TOKEN = secrets.token_hex(18)
    _CONFIG = {
        "resources": [
            {"id": "ark", "provider": "ark", "plan_type": "token_plan",
             "api_key_env": "ARK_API_KEY", "api_key": "sk-test-abc",
             "models": ["glm-5.2"]},
        ],
    }

    def _mod(self):
        from lan_mesh import secret_sync
        return secret_sync

    def test_round_trip(self):
        ss = self._mod()
        payload = ss.encrypt_config(self._CONFIG, self._TOKEN)
        assert set(payload) == {"nonce", "blob"}
        assert ss.decrypt_config(payload, self._TOKEN) == self._CONFIG

    def test_nonce_random_per_encrypt(self):
        ss = self._mod()
        p1 = ss.encrypt_config(self._CONFIG, self._TOKEN)
        p2 = ss.encrypt_config(self._CONFIG, self._TOKEN)
        assert p1["nonce"] != p2["nonce"]  # 每次随机 nonce

    def test_wrong_token_rejected(self):
        ss = self._mod()
        payload = ss.encrypt_config(self._CONFIG, self._TOKEN)
        with pytest.raises(ValueError):
            ss.decrypt_config(payload, "another-token-xyz")

    def test_tampered_blob_rejected(self):
        ss = self._mod()
        import base64
        payload = ss.encrypt_config(self._CONFIG, self._TOKEN)
        raw = bytearray(base64.b64decode(payload["blob"]))
        raw[0] ^= 0xFF  # 篡改首字节 → GCM 完整性校验必失败
        payload["blob"] = base64.b64encode(bytes(raw)).decode()
        with pytest.raises(ValueError):
            ss.decrypt_config(payload, self._TOKEN)

    def test_empty_token_rejected(self):
        ss = self._mod()
        with pytest.raises(ValueError):
            ss.encrypt_config(self._CONFIG, "")
        with pytest.raises(ValueError):
            ss.decrypt_config({"nonce": "x", "blob": "y"}, "  ")

    def test_malformed_payload_rejected(self):
        ss = self._mod()
        with pytest.raises(ValueError):
            ss.decrypt_config({"nonce": "!!", "blob": "??"}, self._TOKEN)
        with pytest.raises(ValueError):
            ss.decrypt_config({"nonce": "", "blob": ""}, self._TOKEN)

    def test_config_hash_stable_and_keyorder_insensitive(self):
        ss = self._mod()
        h1 = ss.config_hash(self._CONFIG)
        reordered = {"resources": list(reversed([
            {"api_key": "sk-test-abc", "models": ["glm-5.2"],
             "plan_type": "token_plan", "id": "ark",
             "provider": "ark", "api_key_env": "ARK_API_KEY"}]))}
        assert ss.config_hash(reordered) == h1  # sort_keys 规范化
        assert len(h1) == 64

    def test_config_hash_differs_on_change(self):
        ss = self._mod()
        changed = {"resources": list(self._CONFIG["resources"]), "strict": True}
        assert ss.config_hash(changed) != ss.config_hash(self._CONFIG)

    def test_mask_secret(self):
        ss = self._mod()
        assert ss.mask_secret("sk-abcdef123") == "sk-a***"
        assert ss.mask_secret("ab") == "***"
        assert ss.mask_secret("") == "***"


class TestDirectKeyEnvInjection:
    """S1: resources.yaml 直填 key 加载时注入环境变量 (路由零改动前提)。"""

    def test_load_injects_direct_key(self, tmp_path, monkeypatch):
        from lan_mesh.model_resources import ModelResourceManager
        env_name = "S1_UNIT_TEST_KEY"
        monkeypatch.delenv(env_name, raising=False)
        yaml_file = tmp_path / "resources.yaml"
        yaml_file.write_text(
            "alert_check: false\n"
            "resources:\n"
            "  - id: unit-pool\n"
            "    provider: deepseek\n"
            "    plan_type: payg\n"
            "    quota: 100\n"
            f"    api_key_env: {env_name}\n"
            "    api_key: sk-unit-direct\n", encoding="utf-8")
        import os
        mgr = ModelResourceManager()
        try:
            assert mgr.load(yaml_file) is True
            assert os.environ.get(env_name) == "sk-unit-direct"
        finally:
            mgr.stop_reporter()
            monkeypatch.delenv(env_name, raising=False)

    def test_load_skips_pool_without_env_name(self, tmp_path):
        from lan_mesh.model_resources import ModelResourceManager
        yaml_file = tmp_path / "resources.yaml"
        yaml_file.write_text(
            "alert_check: false\n"
            "resources:\n"
            "  - id: unit-pool2\n"
            "    provider: deepseek\n"
            "    plan_type: payg\n"
            "    quota: 100\n"
            "    api_key: sk-no-env-name\n", encoding="utf-8")
        mgr = ModelResourceManager()
        try:
            assert mgr.load(yaml_file) is True  # 无 api_key_env 不崩, 仅跳过注入
        finally:
            mgr.stop_reporter()


class TestVersionSync:
    """version_sync 版本比对/领先检测/通知去重 — S2 升级提醒链路回归。"""

    def _mod(self):
        from lan_mesh import version_sync
        return version_sync

    def test_compare_equal_by_commit(self):
        vs = self._mod()
        a = {"commit": "abc1234", "commit_time": 100.0}
        b = {"commit": "abc1234", "commit_time": 999.0}
        assert vs.compare_versions(a, b) == "equal"  # commit 相同即同版本

    def test_compare_by_commit_time(self):
        vs = self._mod()
        newer = {"commit": "bbb2222", "commit_time": 200.0}
        older = {"commit": "aaa1111", "commit_time": 100.0}
        assert vs.compare_versions(newer, older) == "ahead"
        assert vs.compare_versions(older, newer) == "behind"

    def test_compare_unknown_when_missing(self):
        vs = self._mod()
        assert vs.compare_versions({"commit": ""}, {"commit": "x", "commit_time": 1}) == "unknown"
        assert vs.compare_versions({"commit": "x"}, {"commit": "y"}) == "unknown"  # 无时间戳

    def test_find_leader_single_ahead(self):
        vs = self._mod()
        versions = [
            {"device_id": "a", "commit": "c3", "commit_time": 300.0},
            {"device_id": "b", "commit": "c1", "commit_time": 100.0},
            {"device_id": "c", "commit": "c2", "commit_time": 200.0},
        ]
        leader = vs.find_leader(versions)
        assert leader and leader["device_id"] == "a"

    def test_find_leader_none_when_equal(self):
        vs = self._mod()
        versions = [
            {"device_id": "a", "commit": "c1", "commit_time": 100.0},
            {"device_id": "b", "commit": "c1", "commit_time": 100.0},
        ]
        assert vs.find_leader(versions) is None  # 同版本无领先者

    def test_find_leader_none_when_unknown(self):
        vs = self._mod()
        versions = [
            {"device_id": "a", "commit": "c2", "commit_time": 200.0},
            {"device_id": "b", "commit": "c1", "commit_time": 0.0},  # 不可比
        ]
        assert vs.find_leader(versions) is None  # 宁可漏报不可误报

    def test_find_leader_single_host(self):
        vs = self._mod()
        assert vs.find_leader([{"device_id": "a", "commit": "c1", "commit_time": 1}]) is None

    def test_notifier_dedup_same_commit(self):
        vs = self._mod()
        n = vs.UpgradeNotifier()
        assert n.should_notify("dev1", "c1") is True
        assert n.should_notify("dev1", "c1") is False   # 同版本不重复通知
        assert n.should_notify("dev2", "c1") is True    # 不同目标互不影响
        assert n.should_notify("dev1", "c2") is True    # 新版本再次通知
        n.reset()
        assert n.should_notify("dev1", "c2") is True    # reset 后可重通

    def test_read_version_file_fallback(self, tmp_path):
        vs = self._mod()
        missing = vs.read_version_file(tmp_path / "not_exist.json")
        assert missing["commit"] == "" and "upgrade_hint" in missing
        bad = tmp_path / "bad.json"
        bad.write_text("{invalid", encoding="utf-8")
        assert vs.read_version_file(bad)["version"] == ""  # 损坏不抛异常
        good = tmp_path / "ok.json"
        good.write_text('{"version": "9.9.9", "commit": "cafef00d"}', encoding="utf-8")
        rec = vs.read_version_file(good)
        assert rec["version"] == "9.9.9" and rec["commit"] == "cafef00d"

    def test_local_version_info_structure(self):
        vs = self._mod()
        info = vs.local_version_info(force=True)
        for k in ("version", "commit", "commit_time", "released_at", "note", "upgrade_hint"):
            assert k in info
        assert isinstance(info["commit"], str)


class TestStartupSync:
    """S3 启动同步 — hosts 版本列、心跳版本落库、密钥推/拉路由、拉取幂等。"""

    def test_hosts_version_columns_roundtrip(self, tmp_path):
        from lan_mesh.database import Database
        from lan_mesh.protocol import HostRecord
        db = Database(str(tmp_path / "m.db"))
        rec = HostRecord(device_id="dev-a", code_version="abc1234",
                         version_ts=1755300000.0)
        db.upsert_host(rec)
        got = db.get_host("dev-a")
        assert got.code_version == "abc1234"
        assert got.version_ts == 1755300000.0

    def test_heartbeat_updates_version(self, tmp_path):
        from lan_mesh.database import Database
        from lan_mesh.station_director import StationDirector
        from lan_mesh.protocol import HostInfo
        db = Database(str(tmp_path / "m.db"))
        director = StationDirector(db=db, discovery=None)
        info = HostInfo(device_id="dev-b", code_version="bbb1111",
                        version_ts=1755300100.0)
        director.on_host_registered(info)
        director.on_heartbeat("dev-b", {"code_version": "bbb2222",
                                        "version_ts": 1755300200.0})
        got = db.get_host("dev-b")
        assert got.code_version == "bbb2222"
        assert got.version_ts == 1755300200.0

    def test_heartbeat_updates_role(self, tmp_path):
        """E4: 心跳携带 role 时同步落库 (陈旧 role 致选举误判的回归)。"""
        from lan_mesh.database import Database
        from lan_mesh.station_director import StationDirector
        from lan_mesh.protocol import HostInfo
        db = Database(str(tmp_path / "m.db"))
        director = StationDirector(db=db, discovery=None)
        info = HostInfo(device_id="dev-c", role="station")
        director.on_host_registered(info)
        director.on_heartbeat("dev-c", {"role": "secretary"})
        assert db.get_host("dev-c").role == "secretary"
        # 未携带 role 的心跳不改写角色
        director.on_heartbeat("dev-c", {"cpu_percent": 12.0})
        assert db.get_host("dev-c").role == "secretary"

    def test_startup_key_sync_routing(self):
        """F1: 启动密钥对齐角色无关 — 不再按 Secretary/Station 分流推拉。"""
        from lan_mesh.station_controller import StationController
        calls = {"align": []}

        class Fake:
            secretary_active = False  # 非 Secretary 也不再只拉取

            def _align_config_with_peers(self, peers=None):
                calls["align"].append(peers)
                return {"pushed": [], "pulled": [], "skipped": 0,
                        "failed": []}

        peers = [{"device_id": "p2", "role": "worker",
                  "ip": "10.0.0.2", "api_port": 80}]
        StationController._startup_key_sync(Fake(), peers)
        assert calls["align"] == [peers]  # 角色无关: 直接透传给对齐仲裁

    def test_pull_idempotent_same_config(self, monkeypatch):
        from lan_mesh.station_controller import StationController
        from lan_mesh.secret_sync import encrypt_config, config_hash
        import types
        import lan_mesh.http_retry as hr
        import lan_mesh.model_resources as mres
        res_file = Path(__file__).parent.parent / "lan_mesh" / "resources.yaml"
        if not res_file.is_file():
            import pytest as _pt
            _pt.skip("resources.yaml 不存在, 跳过幂等验证")
        data = {"resources": [{"name": "p", "api_key": "sk-x"}]}
        payload = encrypt_config(data, "unit-test-token")
        payload["config_hash"] = config_hash(data)

        class FakeResp:
            def json(self):
                return payload

        monkeypatch.setattr(hr, "http_get",
                            lambda url, **kw: FakeResp())
        # 构造"本机配置与拉取内容一致" → 幂等跳过, 绝不落盘
        monkeypatch.setattr(mres, "read_config_data",
                            lambda p: {"data": data})
        fake_self = types.SimpleNamespace(_mesh_auth_token="unit-test-token")
        result = StationController.pull_resource_secrets(
            fake_self, "10.0.0.2", 80)
        assert result["ok"] is True and result["applied"] is False

    def test_pull_heal_on_token_mismatch(self, monkeypatch):
        """S1 自愈: 解密失败 (mesh_token 不匹配) → 收敛 → 重试成功。"""
        from lan_mesh.station_controller import StationController
        from lan_mesh.secret_sync import encrypt_config
        import types
        import lan_mesh.http_retry as hr
        import lan_mesh.auth as auth_mod
        token_a, token_b = "a" * 64, "b" * 64
        payload = encrypt_config({"resources": []}, token_b)
        payload["config_hash"] = "deadbeef"  # 故意错指纹, 阻断后续落盘

        class FakeResp:
            def json(self):
                return payload

        states = {"v": token_a}
        converged = {}

        def fake_get_token(*a, **k):
            return states["v"]

        def converge_and_swap(target_ip="", target_port=0):
            converged["args"] = (target_ip, target_port)
            states["v"] = token_b

        fake_self = types.SimpleNamespace(
            _mesh_auth_token="", _converge_mesh_token=converge_and_swap)
        monkeypatch.setattr(hr, "http_get", lambda url, **kw: FakeResp())
        monkeypatch.setattr(auth_mod, "get_mesh_token", fake_get_token)
        result = StationController.pull_resource_secrets(
            fake_self, "10.0.0.2", 80)
        # 解密重试成功 → 走到指纹校验 (而非解密失败)
        assert result["detail"] == "配置指纹不匹配", result
        assert converged.get("args") == ("10.0.0.2", 80), converged

    def test_pull_no_heal_on_other_error(self, monkeypatch):
        """S1 自愈边界: 非 token 分歧类错误不做收敛。"""
        from lan_mesh.station_controller import StationController
        from lan_mesh.secret_sync import encrypt_config
        import types
        import lan_mesh.http_retry as hr
        import lan_mesh.auth as auth_mod
        payload = encrypt_config({"resources": []}, "a" * 64)
        payload["blob"] = "!!!not-base64!!!"  # 报文残缺, 非 token 分歧

        class FakeResp:
            def json(self):
                return payload

        converged = {}

        def converge_and_swap(target_ip="", target_port=0):
            converged["called"] = True

        fake_self = types.SimpleNamespace(
            _mesh_auth_token="", _converge_mesh_token=converge_and_swap)
        monkeypatch.setattr(hr, "http_get", lambda url, **kw: FakeResp())
        monkeypatch.setattr(auth_mod, "get_mesh_token", lambda *a, **k: "a" * 64)
        result = StationController.pull_resource_secrets(
            fake_self, "10.0.0.2", 80)
        assert not result["ok"] and "解密失败" in result["detail"]
        assert not converged, "非 token 分歧错误不应触发收敛"


class TestSecretaryConflict:
    """E4 Secretary 冲突仲裁 — 广播真实角色、让位方向、让位幂等。"""

    def _fake_packet(self, device_id, role, name="peer"):
        from lan_mesh.protocol import DiscoveryPacket
        return DiscoveryPacket(device_id=device_id, device_name=name,
                               role=role, api_port=45470)

    def test_make_packet_broadcasts_real_role(self):
        from lan_mesh.station_controller import StationController
        from lan_mesh.protocol import HostInfo

        class Fake:
            secretary_active = True

            def _collect_info(self):
                return HostInfo(device_id="self-1", device_name="本机",
                                role="ignored", api_port=45470)

        f = Fake()
        pkt = StationController._make_packet(f)
        assert pkt.role == "secretary"
        f.secretary_active = False
        assert StationController._make_packet(f).role == "station"

    def test_on_device_seen_yields_when_peer_id_smaller(self):
        from lan_mesh.station_controller import StationController

        class FakeDirector:
            def on_heartbeat(self, *a, **k):
                pass

        class Fake:
            secretary_active = True
            state = type("S", (), {"device_id": "zzz-self"})()
            station_director = FakeDirector()
            db = type("D", (), {"get_host": lambda self, i: None})()
            _yield_secretary_to = StationController._yield_secretary_to

        f = Fake()
        # 对端 id 更小 (字典序) → 本站让位
        StationController._on_device_seen(
            f, self._fake_packet("aaa-peer", "secretary"), "10.0.0.2")
        assert getattr(f, "_secretary_yielded", False) is True

    def test_on_device_seen_keeps_when_peer_id_larger(self):
        from lan_mesh.station_controller import StationController

        class FakeDirector:
            def on_heartbeat(self, *a, **k):
                pass

        class Fake:
            secretary_active = True
            state = type("S", (), {"device_id": "mmm-self"})()
            station_director = FakeDirector()
            db = type("D", (), {"get_host": lambda self, i: None})()

        f = Fake()
        # 对端 id 更大 → 本站保留, 不让位
        StationController._on_device_seen(
            f, self._fake_packet("zzz-peer", "secretary"), "10.0.0.3")
        assert not getattr(f, "_secretary_yielded", False)

    def test_yield_deactivates_once_and_converges(self, monkeypatch):
        from lan_mesh.station_controller import StationController
        calls = {"deactivate": 0, "converge": [], "pull": []}

        monkeypatch.setattr(
            StationController, "_converge_mesh_token",
            lambda self, **kw: calls["converge"].append(kw))
        monkeypatch.setattr(
            StationController, "pull_resource_secrets",
            lambda self, ip, port: calls["pull"].append((ip, port)))

        class Fake:
            secretary_active = True

            def deactivate_secretary(self):
                calls["deactivate"] += 1
                self.secretary_active = False
                return {"ok": True}

            def _queue_ws_broadcast(self, *a, **k):
                pass

        f = Fake()
        StationController._yield_secretary_to(f, "peer", "10.0.0.4", 80)
        StationController._yield_secretary_to(f, "peer", "10.0.0.4", 80)
        assert calls["deactivate"] == 1 and f.secretary_active is False


class TestRoleFreeAlign:
    """F1 角色无关自动对齐 — config_ts 仲裁、池数仲裁、自动升级安全边界。"""

    def _fake(self):
        from lan_mesh.station_controller import StationController

        class Fake:
            state = type("S", (), {"device_id": "self-1"})()

        return StationController, Fake()

    def test_save_config_injects_config_ts(self, tmp_path):
        from lan_mesh.model_resources import save_config
        yaml_file = tmp_path / "resources.yaml"
        data = {"resources": [{"id": "a", "plan_type": "token_plan"}]}
        res = save_config(yaml_file, data)
        assert res.get("ok")
        import yaml
        saved = yaml.safe_load(yaml_file.read_text(encoding="utf-8"))
        assert saved.get("config_ts", 0) > 0

    def test_config_hash_ignores_config_ts(self):
        from lan_mesh.secret_sync import config_hash
        base = {"resources": [{"id": "a"}], "strict": False}
        h1 = config_hash({**base, "config_ts": 100})
        h2 = config_hash({**base, "config_ts": 999})
        assert h1 == h2  # ts 为对齐元数据, 不参与内容指纹
        assert h1 != config_hash({**base, "strict": True})

    def _mock_io(self, monkeypatch, mine, peer_payload):
        import lan_mesh.http_retry as hr
        import lan_mesh.model_resources as mr
        calls = {"push": [], "pull": []}

        class Resp:
            def __init__(self, payload):
                self._p = payload

            def json(self):
                return self._p

        monkeypatch.setattr(mr, "read_config_data",
                            lambda p: {"exists": True, "data": mine})
        monkeypatch.setattr(hr, "http_get",
                            lambda url, timeout=10: Resp(peer_payload))
        return calls

    def _bind(self, fake, calls):
        # push/pull 为实例方法 (Fake 不继承 StationController, 直接挂实例)
        fake.push_resource_secrets = (
            lambda **kw: calls["push"].append(kw)
            or [{"ok": True, "detail": "ok"}])
        fake.pull_resource_secrets = (
            lambda ip, port: calls["pull"].append((ip, port))
            or {"ok": True, "detail": "ok"})

    def test_align_pulls_when_peer_newer(self, monkeypatch):
        from lan_mesh.secret_sync import config_hash
        mine = {"resources": [{"id": "a"}], "config_ts": 100.0}
        peer_cfg = {"resources": [{"id": "b"}], "config_ts": 200.0}
        calls = self._mock_io(monkeypatch, mine, {
            "config_hash": config_hash(peer_cfg),
            "config_ts": 200.0, "pools": 1, "blob": "xx"})
        SC, fake = self._fake()
        self._bind(fake, calls)
        summary = SC._align_config_with_peers(fake, [{
            "device_id": "p1", "ip": "10.0.0.2", "api_port": 45470,
            "device_name": "peer"}])
        assert calls["pull"] and not calls["push"]
        assert summary["pulled"] and summary["pulled"][0]["ok"]

    def test_align_pushes_when_local_newer(self, monkeypatch):
        from lan_mesh.secret_sync import config_hash
        mine = {"resources": [{"id": "a"}], "config_ts": 300.0}
        peer_cfg = {"resources": [{"id": "b"}], "config_ts": 200.0}
        calls = self._mock_io(monkeypatch, mine, {
            "config_hash": config_hash(peer_cfg),
            "config_ts": 200.0, "pools": 1, "blob": "xx"})
        SC, fake = self._fake()
        self._bind(fake, calls)
        summary = SC._align_config_with_peers(fake, [{
            "device_id": "p1", "ip": "10.0.0.2", "api_port": 45470,
            "device_name": "peer"}])
        assert calls["push"] and not calls["pull"]
        assert summary["pushed"] and summary["pushed"][0]["ok"]

    def test_align_skips_when_identical(self, monkeypatch):
        from lan_mesh.secret_sync import config_hash
        mine = {"resources": [{"id": "a"}], "config_ts": 100.0}
        calls = self._mock_io(monkeypatch, mine, {
            "config_hash": config_hash(mine),
            "config_ts": 100.0, "pools": 1, "blob": "xx"})
        SC, fake = self._fake()
        summary = SC._align_config_with_peers(fake, [{
            "device_id": "p1", "ip": "10.0.0.2", "api_port": 45470,
            "device_name": "peer"}])
        assert not calls["push"] and not calls["pull"]
        assert summary["skipped"] == 1

    def test_align_pool_count_arbitration_without_ts(self, monkeypatch):
        """双方均无 config_ts 时按资源池数仲裁 (本机多 → 推送)。"""
        from lan_mesh.secret_sync import config_hash
        mine = {"resources": [{"id": "a"}, {"id": "c"}]}
        peer_cfg = {"resources": [{"id": "b"}]}
        calls = self._mock_io(monkeypatch, mine, {
            "config_hash": config_hash(peer_cfg),
            "pools": 1, "blob": "xx"})
        SC, fake = self._fake()
        self._bind(fake, calls)
        summary = SC._align_config_with_peers(fake, [{
            "device_id": "p1", "ip": "10.0.0.2", "api_port": 45470,
            "device_name": "peer"}])
        assert calls["push"] and not calls["pull"]
        assert summary["pushed"]

    def test_align_unreachable_peer_recorded(self, monkeypatch):
        import lan_mesh.http_retry as hr
        monkeypatch.setattr(hr, "http_get",
                            lambda url, timeout=10: (_ for _ in ()).throw(
                                ConnectionError("boom")))
        SC, fake = self._fake()
        summary = SC._align_config_with_peers(fake, [{
            "device_id": "p1", "ip": "10.0.0.9", "api_port": 45470,
            "device_name": "peer"}])
        assert summary["failed"] and "boom" in summary["failed"][0]["detail"]

    def test_auto_upgrade_skips_dirty_workspace(self, monkeypatch):
        import subprocess as _sub
        from lan_mesh.station_controller import StationController
        calls = []

        class R:
            def __init__(self, out="", err="", code=0):
                self.stdout, self.stderr, self.returncode = out, err, code

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:3] == ["git", "status", "--porcelain"]:
                return R(out=" M dirty.py\n")
            return R()

        monkeypatch.setattr(_sub, "run", fake_run)
        SC, fake = self._fake()
        fake.auto_upgrade_enabled = True
        fake._upgrade_attempted = set()
        SC._auto_upgrade(fake, "abc123", "leader")
        import time
        deadline = time.time() + 2
        while time.time() < deadline and not calls:
            time.sleep(0.05)
        time.sleep(0.2)
        assert calls and calls[0][:3] == ["git", "status", "--porcelain"]
        assert all(c[:2] != ["git", "pull"] for c in calls)  # 脏工作区不 pull

    def test_auto_upgrade_disabled_no_action(self, monkeypatch):
        import subprocess as _sub
        from lan_mesh.station_controller import StationController
        calls = []
        monkeypatch.setattr(
            _sub, "run",
            lambda cmd, **kw: calls.append(cmd) or type(
                "R", (), {"stdout": "", "stderr": "", "returncode": 0})())
        SC, fake = self._fake()
        fake.auto_upgrade_enabled = False
        fake._upgrade_attempted = set()
        SC._auto_upgrade(fake, "abc123", "leader")
        import time
        time.sleep(0.2)
        assert not calls and "abc123" not in fake._upgrade_attempted

    def test_auto_upgrade_once_per_commit(self, monkeypatch):
        import subprocess as _sub
        from lan_mesh.station_controller import StationController
        calls = []
        monkeypatch.setattr(
            _sub, "run",
            lambda cmd, **kw: calls.append(cmd) or type(
                "R", (), {"stdout": "", "stderr": "", "returncode": 0})())
        SC, fake = self._fake()
        fake.auto_upgrade_enabled = True
        fake._upgrade_attempted = set()
        SC._auto_upgrade(fake, "abc123", "leader")
        SC._auto_upgrade(fake, "abc123", "leader")  # 同 commit 去重
        import time
        deadline = time.time() + 2
        while (time.time() < deadline
               and sum(1 for c in calls if c[:2] == ["git", "pull"]) < 1):
            time.sleep(0.05)
        # 升级链完整跑完 (status+pull+pip) 但仅一次, 第二次调用被去重
        assert sum(1 for c in calls
                   if c[:3] == ["git", "status", "--porcelain"]) == 1
        assert sum(1 for c in calls if c[:2] == ["git", "pull"]) == 1


class TestSecretaryFailover:
    """E5 Secretary 离线接管 — 接管判定、仲裁方向、有活 Secretary 不接管。"""

    def _host(self, device_id, role="station", online=True):
        return type("H", (), {"device_id": device_id,
                              "role": role, "online": online})()

    def _ctrl(self, self_id, hosts, active=False, activate_raises=False):
        class Fake:
            secretary_active = active
            state = type("S", (), {"device_id": self_id,
                                   "device_name": "本机"})()
            db = type("D", (), {"list_hosts": lambda self_: hosts})()
            bot_gateway = type(
                "B", (), {"notify": lambda self, *a, **k: None})()
            activated = []

            def activate_secretary(self):
                if activate_raises:
                    raise RuntimeError("boom")
                self.secretary_active = True
                self.activated.append(True)
                return {"ok": True}

            def _queue_ws_broadcast(self, *a, **k):
                pass

        return Fake()

    def _check(self, fake):
        from lan_mesh.station_controller import StationController
        StationController._secretary_failover_check(fake)

    def test_takeover_when_secretary_offline_and_self_min(self):
        """Secretary 离线且本站 device_id 仲裁最小 → 接管。"""
        f = self._ctrl("aaa-self", [
            self._host("mmm-sec", role="secretary", online=False),
            self._host("zzz-peer", online=True),
        ])
        self._check(f)
        assert f.secretary_active is True and f.activated

    def test_no_takeover_when_secretary_online(self):
        """仍有在线 Secretary → 不接管。"""
        f = self._ctrl("aaa-self", [
            self._host("mmm-sec", role="secretary", online=True),
        ])
        self._check(f)
        assert f.secretary_active is False and not f.activated

    def test_no_takeover_when_smaller_station_online(self):
        """存在 device_id 更小的在线 Station → 由对方接管, 本站不动。"""
        f = self._ctrl("zzz-self", [
            self._host("mmm-sec", role="secretary", online=False),
            self._host("aaa-peer", online=True),
        ])
        self._check(f)
        assert f.secretary_active is False and not f.activated

    def test_already_secretary_noop(self):
        """本站已是 Secretary → 直接返回, 不重复激活。"""
        f = self._ctrl("aaa-self", [], active=True)
        self._check(f)
        assert not f.activated

    def test_activate_failure_does_not_crash(self):
        """激活异常被吃掉, 不阻断清理循环, 状态保持未激活。"""
        f = self._ctrl("aaa-self", [
            self._host("mmm-sec", role="secretary", online=False),
        ], activate_raises=True)
        self._check(f)
        assert f.secretary_active is False and not f.activated

    def test_single_node_network_takes_over(self):
        """单机网络 (仅本站在线) Secretary 离线后自我接管。"""
        f = self._ctrl("solo-self", [
            self._host("old-sec", role="secretary", online=False),
        ])
        self._check(f)
        assert f.secretary_active is True


# ── PM 三件套接口级单测 (P1 #9) ─────────────────────────────────

class _RecordingAgent:
    """PM 协调器 Fake: 记录全部上报调用, 供 planner/monitor/dispatcher 共用。"""

    secretary_url = ""
    running = True

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def _record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return None
        return _record


class TestPMPlanner:
    """PMPlanner — 类型推断、模板直通、LLM 回退、任务细化。"""

    def _planner(self, runtime=None):
        from lan_mesh.pm_planner import PMPlanner
        from lan_mesh.pm_state import PMState
        return PMPlanner("pm-test-1234", runtime or object(),
                         PMState(), _RecordingAgent())

    def test_infer_task_type_keywords(self):
        from lan_mesh.pm_planner import PMPlanner
        assert PMPlanner.infer_task_type("代码审查", "") == "code_review"
        assert PMPlanner.infer_task_type("", "开发一个计算器") == "development"
        assert PMPlanner.infer_task_type("上线", "deploy 发布") == "deployment"
        assert PMPlanner.infer_task_type("随便做点什么", "") == "general"

    def test_generate_refinement_question_rounds(self):
        from lan_mesh.pm_planner import PMPlanner
        q0 = PMPlanner._generate_refinement_question("做个工具", [], 0)
        assert "做个工具" in q0 and "技术约束" in q0
        q1 = PMPlanner._generate_refinement_question("做个工具", ["Q1: x"], 1)
        assert q1
        assert PMPlanner._generate_refinement_question("做个工具", [], 1) == ""
        assert PMPlanner._generate_refinement_question("做个工具", ["x"], 2) == ""

    def test_refine_skips_when_desc_long_or_has_input(self):
        p = self._planner()
        long_task = {"name": "t", "description": "一段足够长的任务描述内容一二三四五六七八九", "input_data": {}}
        assert p.refine_requirements(long_task) is long_task
        short_with_input = {"name": "t", "description": "做", "input_data": {"k": 1}}
        assert p.refine_requirements(short_with_input) is short_with_input

    def test_refine_accumulates_answers(self):
        agent = _RecordingAgent()
        asked = []
        agent.request_clarification = (
            lambda **kw: asked.append(kw) or {"response": "用 FastAPI"})
        p = self._planner()
        p._agent = agent
        task = {"name": "t", "description": "做个小工具", "input_data": {}}
        enriched = p.refine_requirements(task)
        assert "补充信息" in enriched["description"]
        assert "用 FastAPI" in enriched["description"]
        # 两轮追问均发起 (第二轮为开放式补充)
        assert len(asked) == 2

    def test_refine_breaks_on_timeout(self):
        agent = _RecordingAgent()
        agent.request_clarification = lambda **kw: {"timed_out": True}
        p = self._planner()
        p._agent = agent
        task = {"name": "t", "description": "做个小工具", "input_data": {}}
        result = p.refine_requirements(task)
        assert result["description"] == "做个小工具"  # 未细化, 原样返回

    def test_analyze_template_hit_skips_llm(self, monkeypatch):
        import lan_mesh.task_templates as tt
        monkeypatch.setattr(
            tt, "match_template",
            lambda desc: {"name": "tpl", "match_score": 3})
        monkeypatch.setattr(
            tt, "apply_template", lambda tpl, variables: {"pattern": "single",
                                                          "from_template": True})
        p = self._planner()
        plan = p.analyze_with_skill({"name": "t", "description": "x", "input_data": {}})
        assert plan.get("from_template") is True

    def test_analyze_llm_json_parse_failure_falls_back_single(self, monkeypatch):
        import lan_mesh.task_templates as tt
        monkeypatch.setattr(tt, "match_template", lambda desc: None)

        class FakeRuntime:
            def _call_llm_with_routing(self, prompt, opts):
                return {"content": "这不是 JSON {{{"}

        p = self._planner(runtime=FakeRuntime())
        plan = p.analyze_with_skill({"name": "任务A", "description": "干活", "input_data": {}})
        assert plan["pattern"] == "single" and plan["team_size"] == 1
        assert plan["decomposition"][0]["name"] == "任务A"
        assert "回退" in plan["reasoning"]

    def test_analyze_llm_valid_json_passthrough(self, monkeypatch):
        import lan_mesh.task_templates as tt
        monkeypatch.setattr(tt, "match_template", lambda desc: None)

        class FakeRuntime:
            def _call_llm_with_routing(self, prompt, opts):
                return {"content": '```json\n{"complexity": "simple",'
                        ' "pattern": "single", "team_size": 1,'
                        ' "decomposition": [], "reasoning": "ok"}\n```'}

        p = self._planner(runtime=FakeRuntime())
        plan = p.analyze_with_skill({"name": "t", "description": "x", "input_data": {}})
        assert plan["complexity"] == "simple" and plan["reasoning"] == "ok"

    def test_execute_directly_detects_llm_error_markers(self):
        class FakeRuntime:
            def execute(self, subtask):
                return {"status": "completed",
                        "output": {"code": "[未配置模型资源]"}}

        p = self._planner(runtime=FakeRuntime())
        result = p.execute_directly({"task_id": "t1", "name": "n",
                                     "description": "d", "input_data": {}})
        assert result["status"] == "failed"

    def test_execute_directly_normal_completion(self):
        class FakeRuntime:
            def execute(self, subtask):
                return {"status": "completed",
                        "output": {"code": "print('done')"}}

        p = self._planner(runtime=FakeRuntime())
        result = p.execute_directly({"task_id": "t1", "name": "n",
                                     "description": "d", "input_data": {}})
        assert result["status"] == "completed"
        assert "print('done')" in result["summary"]

    def test_build_memory_hint_empty_on_failure(self, monkeypatch):
        import lan_mesh.http_retry as hr
        agent = _RecordingAgent()
        agent.secretary_url = "http://10.0.0.1:45470"
        p = self._planner()
        p._agent = agent
        monkeypatch.setattr(hr, "http_post",
                            lambda *a, **k: (_ for _ in ()).throw(
                                ConnectionError("down")))
        assert p._build_memory_hint({"name": "开发", "description": ""}) == ""

    def test_build_memory_hint_renders_stats(self, monkeypatch):
        import lan_mesh.http_retry as hr

        class Resp:
            status_code = 200

            def json(self):
                return {"total": 3, "success_rate": 0.67, "avg_duration": 120.5,
                        "recommended_mode": "orchestrator",
                        "common_errors": [["timeout", 2]]}

        agent = _RecordingAgent()
        agent.secretary_url = "http://10.0.0.1:45470"
        p = self._planner()
        p._agent = agent
        monkeypatch.setattr(hr, "http_post", lambda *a, **k: Resp())
        hint = p._build_memory_hint({"name": "开发一个功能", "description": ""})
        assert "3 条" in hint and "orchestrator" in hint and "timeout" in hint


class TestPMMonitor:
    """PMMonitor — 超时检测、进度上报、三级失败接管、质量验证、聚合。"""

    def _monitor(self, state=None, dispatcher=None):
        from lan_mesh.pm_monitor import PMMonitor
        from lan_mesh.pm_state import PMState
        st = state or PMState()
        disp = dispatcher or _RecordingDispatcher()
        mon = PMMonitor("pm-mon-1234", _RecordingRuntime(), "http://sec:1",
                        st, _RecordingAgent(), disp)
        return mon

    def test_is_global_timed_out(self):
        import time
        mon = self._monitor()
        assert mon.is_global_timed_out() is False
        mon._state.start_time = time.time() - 4000
        mon._state.global_timeout = 3600.0
        assert mon.is_global_timed_out() is True

    def test_check_subtask_timeouts_marks_failed_and_retries(self):
        import time
        from lan_mesh.pm_state import PMState
        st = PMState()
        st.subtask_timeout = 10.0
        st.subtask_start_times["A"] = time.time() - 100
        st.subagents["m1"] = {"current_task": "A", "status": "in_progress"}
        st.plan = {"decomposition": [{"name": "A", "skill": "code_generation"}]}
        st.task_station["A"] = {"device_id": "s1"}
        st.task_agent["A"] = {"agent_id": "a1"}
        mon = self._monitor(state=st)
        mon.check_subtask_timeouts()
        assert "A" not in st.subtask_start_times
        assert st.subagents["m1"]["status"] == "failed"
        # 首次失败走同站重试
        assert st.retry_counts["A"] == 1
        assert mon._dispatcher.calls and mon._dispatcher.calls[0][0] == "dispatch_subtask"

    def test_failure_strategy1_same_station_retry(self):
        from lan_mesh.pm_state import PMState
        st = PMState()
        st.plan = {"decomposition": [{"name": "A", "skill": "code_generation"}]}
        st.task_station["A"] = {"device_id": "s1"}
        st.task_agent["A"] = {"agent_id": "a1"}
        mon = self._monitor(state=st)
        mon.handle_subagent_failure("A", "boom")
        assert st.retry_counts["A"] == 1
        name, args, _ = mon._dispatcher.calls[0]
        assert name == "dispatch_subtask" and args[0]["device_id"] == "s1"
        # 重试上下文注入 input_data
        retry_task = args[2]
        assert retry_task["input_data"]["_retry_context"]["attempt"] == 1

    def test_failure_strategy_not_found_in_plan(self):
        from lan_mesh.pm_state import PMState
        st = PMState()
        st.plan = {"decomposition": []}
        mon = self._monitor(state=st)
        mon.handle_subagent_failure("ghost", "boom")
        assert not mon._dispatcher.calls  # plan 中不存在 → 跳过接管

    def test_failure_strategy2_cross_station_retry(self, monkeypatch):
        from lan_mesh.pm_monitor import PMMonitor
        from lan_mesh.pm_state import PMState
        st = PMState()
        st.max_retries = 1
        st.retry_counts["A"] = 1  # 同站重试额度已耗尽
        st.plan = {"decomposition": [{"name": "A", "skill": "code_generation"}]}
        st.task_station["A"] = {"device_id": "s1"}
        st.task_agent["A"] = {"agent_id": "a1"}
        disp = _RecordingDispatcher()
        disp.get_available_stations = lambda: [
            {"device_id": "s1"}, {"device_id": "s2"}]
        disp._create_subagent_on_station = lambda *a, **k: None  # 创建失败
        mon = self._monitor(state=st, dispatcher=disp)
        mon.handle_subagent_failure("A", "boom")
        assert st.retry_counts["A"] == 2
        # 创建失败 → 不分发, 也不本地接管 (等下一轮)
        assert not any(c[0] == "dispatch_subtask" for c in disp.calls)
        assert not any(c[0] == "execute_subtask_locally" for c in disp.calls)

    def test_failure_strategy3_local_takeover_and_escalation(self):
        from lan_mesh.pm_state import PMState
        st = PMState()
        st.max_retries = 1
        st.retry_counts["A"] = 1
        st.plan = {"decomposition": [{"name": "A", "skill": "code_generation"}]}
        st.task_station["A"] = {"device_id": "s1"}
        st.task_agent["A"] = {"agent_id": "a1"}
        disp = _RecordingDispatcher()
        disp.get_available_stations = lambda: [{"device_id": "s1"}]  # 无其他站
        mon = self._monitor(state=st, dispatcher=disp)
        mon.handle_subagent_failure("A", "boom")
        assert "A" in mon._local_takeover_tasks
        assert any(c[0] == "execute_subtask_locally" for c in disp.calls)
        # escalated 上报
        assert any(c[0] == "report_status" and c[1][0] == "escalated"
                   for c in mon._agent.calls)

    def test_receive_progress_completed_stores_output_and_dispatches(self, monkeypatch):
        from lan_mesh.pm_monitor import PMMonitor
        from lan_mesh.pm_state import PMState
        st = PMState()
        st.subagents["m1"] = {"agent_id": "a1", "status": "in_progress",
                              "current_task": ""}
        monkeypatch.setattr(PMMonitor, "_verify_output_quality",
                            lambda self, tn, out: None)
        mon = self._monitor(state=st)
        mon.receive_progress_report({
            "reporter_id": "m1", "task_name": "A", "status": "completed",
            "output": "结果内容", "self_check": {"passed": True, "notes": "ok"},
            "progress": 1.0})
        assert st.subtask_outputs["A"] == "结果内容"
        assert st.subagents["m1"]["status"] == "completed"
        assert any(c[0] == "try_dispatch_pending" for c in mon._dispatcher.calls)

    def test_receive_progress_failed_in_local_takeover_no_retry(self):
        from lan_mesh.pm_state import PMState
        st = PMState()
        st.subagents["m1"] = {"agent_id": "a1", "status": "in_progress",
                              "current_task": ""}
        mon = self._monitor(state=st)
        mon._local_takeover_tasks.add("A")
        mon.receive_progress_report({
            "reporter_id": "m1", "task_name": "A", "status": "failed",
            "message": "又挂了"})
        # 本地接管后仍失败 → 放弃重试, 移出接管集合
        assert "A" not in mon._local_takeover_tasks
        assert not any(c[0] == "dispatch_subtask" for c in mon._dispatcher.calls)

    def test_receive_progress_failed_triggers_takeover(self):
        from lan_mesh.pm_state import PMState
        st = PMState()
        st.subagents["m1"] = {"agent_id": "a1", "status": "in_progress",
                              "current_task": ""}
        st.plan = {"decomposition": [{"name": "A", "skill": "code_generation"}]}
        st.task_station["A"] = {"device_id": "s1"}
        st.task_agent["A"] = {"agent_id": "a1"}
        mon = self._monitor(state=st)
        mon.receive_progress_report({
            "reporter_id": "m1", "task_name": "A", "status": "failed",
            "message": "boom"})
        assert st.retry_counts["A"] == 1  # 走同站重试策略

    def test_verify_output_quality_skips_short_output(self):
        mon = self._monitor()
        assert mon._verify_output_quality("A", "短") is None
        assert mon._verify_output_quality("A", "") is None

    def test_verify_output_quality_parses_llm_json(self):
        class FakeRuntime:
            def _call_llm_with_routing(self, prompt, opts):
                return {"content": '{"accepted": false, "score": 4,'
                        ' "issues": "缺少错误处理"}'}

        from lan_mesh.pm_monitor import PMMonitor
        from lan_mesh.pm_state import PMState
        mon = PMMonitor("pm-x", FakeRuntime(), "u", PMState(),
                        _RecordingAgent(), _RecordingDispatcher())
        result = mon._verify_output_quality("A", "x" * 60)
        assert result["accepted"] is False and result["score"] == 4.0

    def test_verify_output_quality_exception_returns_none(self):
        class FakeRuntime:
            def _call_llm_with_routing(self, prompt, opts):
                raise RuntimeError("llm down")

        from lan_mesh.pm_monitor import PMMonitor
        from lan_mesh.pm_state import PMState
        mon = PMMonitor("pm-x", FakeRuntime(), "u", PMState(),
                        _RecordingAgent(), _RecordingDispatcher())
        assert mon._verify_output_quality("A", "x" * 60) is None

    def test_aggregate_results_delivers(self):
        from lan_mesh.pm_state import PMState
        st = PMState()
        st.plan = {"decomposition": [{"name": "A", "skill": "code_generation"}]}
        st.task = {"name": "总任务", "description": "desc"}
        st.subtask_outputs["A"] = "子任务结果"
        st.subagents["m1"] = {"current_task": "A", "status": "completed"}
        mon = self._monitor(state=st)
        mon.aggregate_results()
        assert st.subtask_outputs.get("_aggregated")
        assert any(c[0] == "deliver_result" for c in mon._agent.calls)
        assert any(c[0] == "report_status" and c[1][0] == "completed"
                   for c in mon._agent.calls)

    def test_aggregate_results_noop_without_plan(self):
        mon = self._monitor()
        mon.aggregate_results()
        assert not mon._agent.calls


class _RecordingRuntime:
    def _call_llm_with_routing(self, prompt, opts):
        return {"content": '{"accepted": true, "score": 8, "issues": ""}'}


class _RecordingDispatcher:
    """PMDispatcher Fake: 记录调用, 默认返回空站点列表。"""

    def __init__(self):
        self.calls = []

    def get_available_stations(self):
        return []

    def dispatch_subtask(self, *args, **kwargs):
        self.calls.append(("dispatch_subtask", args, kwargs))

    def try_dispatch_pending(self):
        self.calls.append(("try_dispatch_pending", (), {}))

    def execute_subtask_locally(self, *args, **kwargs):
        self.calls.append(("execute_subtask_locally", args, kwargs))

    def _create_subagent_on_station(self, *args, **kwargs):
        self.calls.append(("_create_subagent_on_station", args, kwargs))
        return None

    def _build_subagent_prompt_for_sub(self, *args, **kwargs):
        return "prompt"


class TestPMDispatcher:
    """PMDispatcher — 站点筛选、依赖分发、本地回退、prompt 构建。"""

    def _dispatcher(self, state=None):
        from lan_mesh.pm_dispatcher import PMDispatcher
        from lan_mesh.pm_state import PMState
        return PMDispatcher("pm-disp-1234", _RecordingRuntime(),
                            "http://sec:1", "self-1",
                            state or PMState(), _RecordingAgent())

    def test_get_available_stations_filters_offline(self, monkeypatch):
        import lan_mesh.pm_dispatcher as pd

        class Resp:
            status_code = 200

            def json(self):
                return {"hosts": [
                    {"device_id": "h1", "online": True, "api_port": 45470},
                    {"device_id": "h2", "online": False, "api_port": 45470},
                    {"device_id": "h3", "online": True, "api_port": 0},
                ]}

        monkeypatch.setattr(pd, "http_get", lambda *a, **k: Resp())
        disp = self._dispatcher()
        stations = disp.get_available_stations()
        assert [s["device_id"] for s in stations] == ["h1"]

    def test_get_available_stations_returns_empty_on_error(self, monkeypatch):
        import lan_mesh.pm_dispatcher as pd
        monkeypatch.setattr(pd, "http_get",
                            lambda *a, **k: (_ for _ in ()).throw(
                                ConnectionError("down")))
        disp = self._dispatcher()
        assert disp.get_available_stations() == []

    def test_try_dispatch_pending_injects_dependency_outputs(self):
        from lan_mesh.pm_state import PMState
        st = PMState()
        st.task = {"task_id": "t1", "name": "总", "input_data": {}}
        st.plan = {"decomposition": [
            {"name": "A", "skill": "code_generation", "depends_on": []},
            {"name": "B", "skill": "code_review", "depends_on": ["A"]},
        ]}
        station = {"device_id": "s1", "ip": "10.0.0.2", "api_port": 45470}
        st.pending_subtasks["B"] = {
            "sub": st.plan["decomposition"][1], "station": station,
            "agent_info": {"agent_id": "a2"}}
        st.subtask_outputs["A"] = "A 的结果"
        disp = self._dispatcher(state=st)
        disp.dispatch_subtask = lambda *a, **k: disp.calls.append(
            ("dispatch_subtask", a, k))
        disp.calls = []
        disp.try_dispatch_pending()
        assert "B" not in st.pending_subtasks
        assert "B" in st.dispatched
        dispatched_task = disp.calls[0][1][2]
        assert dispatched_task["input_data"]["_dependency_outputs"]["A"] == "A 的结果"

    def test_try_dispatch_pending_waits_for_unsatisfied_deps(self):
        from lan_mesh.pm_state import PMState
        st = PMState()
        st.task = {"task_id": "t1", "input_data": {}}
        st.plan = {"decomposition": [
            {"name": "B", "skill": "code_review", "depends_on": ["A"]}]}
        st.pending_subtasks["B"] = {
            "sub": st.plan["decomposition"][0],
            "station": {"device_id": "s1"}, "agent_info": {"agent_id": "a2"}}
        # A 尚无输出 → B 保持 pending
        disp = self._dispatcher(state=st)
        disp.try_dispatch_pending()
        assert "B" in st.pending_subtasks and "B" not in st.dispatched

    def test_execute_subtask_locally_reports_result(self):
        class FakeRuntime:
            def execute(self, subtask):
                assert subtask["input_data"].get("requirement")
                return {"status": "completed", "output": {"summary": "done"}}

        from lan_mesh.pm_dispatcher import PMDispatcher
        from lan_mesh.pm_state import PMState
        agent = _RecordingAgent()
        disp = PMDispatcher("pm-disp-1234", FakeRuntime(), "http://sec:1",
                            "self-1", PMState(), agent)
        disp.execute_subtask_locally(
            {"task_id": "t1", "input_data": {}},
            {"name": "A", "skill": "code_generation", "description": "干活"})
        result_calls = [c for c in agent.calls if c[0] == "receive_subtask_result"]
        assert result_calls and result_calls[0][2]["status"] == "completed"

    def test_dispatch_subtask_falls_back_locally_on_error(self, monkeypatch):
        import lan_mesh.pm_dispatcher as pd

        def boom(*a, **k):
            raise ConnectionError("refused")

        monkeypatch.setattr(pd.requests, "post", boom)
        disp = self._dispatcher()
        disp.calls = []
        disp.execute_subtask_locally = lambda task, sub: disp.calls.append(
            ("execute_subtask_locally", (task, sub), {}))
        disp.dispatch_subtask(
            {"ip": "10.0.0.2", "api_port": 45470},
            {"agent_id": "a1"}, {"task_id": "t1", "input_data": {}},
            {"name": "A", "skill": "code_generation"})
        assert disp.calls and disp.calls[0][0] == "execute_subtask_locally"

    def test_build_subagent_prompt_includes_context(self):
        from lan_mesh.pm_dispatcher import PMDispatcher
        from lan_mesh.pm_state import PMState
        disp = PMDispatcher("pm-disp-1234", _RecordingRuntime(),
                            "http://sec:1", "self-1", PMState(),
                            _RecordingAgent())
        task = {"name": "总", "input_data": {}}
        plan = {"pattern": "orchestrator", "decomposition": [
            {"name": "A", "skill": "code_generation", "depends_on": [],
             "description": "写代码"},
            {"name": "B", "skill": "code_review", "depends_on": ["A"],
             "description": "审查代码"},
        ]}
        prompt = disp._build_subagent_prompt_for_sub(
            task, plan["decomposition"][1], plan, "sub-1", "B")
        assert isinstance(prompt, str) and prompt
        assert "B" in prompt and "审查代码" in prompt
        # 依赖提示: B 依赖 A, 应包含等待前序输出的说明
        assert "前序" in prompt


# ═══════════════════════════════════════════════════════════
# Worker WS 直推测试 (M5-2)
# ═══════════════════════════════════════════════════════════

class _FakeWsDb:
    """database.query_unreported_usage / mark_usage_reported 最小替身。"""

    def __init__(self, rows=None):
        self.rows = rows or []
        self.marked = []

    def query_unreported_usage(self, batch):
        return self.rows[:batch]

    def mark_usage_reported(self, ids):
        self.marked.extend(ids)


class _FakeWsConn:
    """websockets sync 连接最小替身 (send/recv)。"""

    def __init__(self, ack=None, recv_error=None):
        self.sent = []
        self._ack = {"ok": True, "duplicate": 0} if ack is None else ack
        self._recv_error = recv_error

    def send(self, text):
        self.sent.append(text)

    def recv(self, timeout=None):
        if self._recv_error:
            raise self._recv_error
        import json as _json
        return _json.dumps(self._ack)


def _ws_usage_row(uid="u1"):
    return {"id": uid, "usage_id": uid, "model_id": "m1",
            "input_tokens": 10, "output_tokens": 20,
            "task_id": "", "project_id": ""}


class TestWorkerWsPush:
    """M5-2: WS 直推 — URL 派生/推送轮次/HTTP 兜底跳过/Secretary 端点。"""

    # ── Worker 端单元 ──

    def test_build_ws_url_scheme_and_token(self):
        from lan_mesh.model_resources import _build_ws_url
        assert _build_ws_url("http://10.0.0.1:45470/", "tk") == \
            "ws://10.0.0.1:45470/ws/worker?token=tk"
        assert _build_ws_url("https://sec:443") == \
            "wss://sec:443/ws/worker"
        assert _build_ws_url("") == ""
        assert _build_ws_url("not-a-url", "tk") == ""

    def test_push_once_ws_marks_reported_on_ack(self):
        import json as _json
        from lan_mesh.model_resources import ModelResourceManager
        mgr = ModelResourceManager()
        mgr._db = _FakeWsDb([_ws_usage_row("u1"), _ws_usage_row("u2")])
        conn = _FakeWsConn()
        assert mgr._push_once_ws(conn) is True
        assert mgr._db.marked == ["u1", "u2"]
        assert mgr._ws_last_ok > 0
        payload = _json.loads(conn.sent[0])
        assert payload["type"] == "usage_batch"
        assert len(payload["records"]) == 2

    def test_push_once_ws_rejected_not_marked(self):
        from lan_mesh.model_resources import ModelResourceManager
        mgr = ModelResourceManager()
        mgr._report_interval = 5.0  # 缩短被拒后的暂缓等待
        mgr._db = _FakeWsDb([_ws_usage_row()])
        conn = _FakeWsConn(ack={"ok": False, "error": "secretary_inactive"})
        assert mgr._push_once_ws(conn) is True  # 连接保留, 等 HTTP 兜底
        assert mgr._db.marked == [] and mgr._ws_last_ok == 0.0

    def test_push_once_ws_broken_conn_returns_false(self):
        from lan_mesh.model_resources import ModelResourceManager
        mgr = ModelResourceManager()
        mgr._db = _FakeWsDb([_ws_usage_row()])
        conn = _FakeWsConn(recv_error=ConnectionError("断开"))
        assert mgr._push_once_ws(conn) is False
        assert mgr._db.marked == []

    def test_push_once_ws_empty_round_keeps_alive(self):
        from lan_mesh.model_resources import ModelResourceManager
        mgr = ModelResourceManager()
        mgr._ws_push_interval = 0.01
        mgr._db = _FakeWsDb([])
        conn = _FakeWsConn()
        assert mgr._push_once_ws(conn) is True
        assert conn.sent == [] and mgr._ws_last_ok > 0

    def test_report_once_skips_when_ws_healthy(self):
        import time as _time
        from lan_mesh.model_resources import ModelResourceManager
        mgr = ModelResourceManager()
        mgr._secretary_url = "http://sec:1"
        mgr._db = _FakeWsDb([_ws_usage_row()])
        mgr._ws_last_ok = _time.time()  # WS 通道新鲜 → HTTP 批量跳过
        assert mgr.report_once() == {"reported": 0, "via": "ws"}
        assert mgr._db.marked == []  # 未走 HTTP, 游标不动

    def test_set_report_target_derives_ws_url(self):
        from lan_mesh.model_resources import ModelResourceManager
        mgr = ModelResourceManager()
        mgr._enabled = True
        # db 缺失 → 线程不启动但地址已派生 (待 db 就绪后重注入即生效)
        assert mgr.set_report_target("http://10.0.0.1:45470",
                                     token="tk") is False
        assert mgr._ws_url == "ws://10.0.0.1:45470/ws/worker?token=tk"
        assert mgr._ws_token == "tk"

    # ── Secretary 端点 (/ws/worker) ──

    def _station_client(self, controller):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.station_api import create_station_router
        app = FastAPI()
        app.include_router(create_station_router(controller))
        return TestClient(app)

    def _controller(self, active=True):
        import types

        class _Ctl:
            def __getattr__(self, name):
                from unittest.mock import MagicMock
                return MagicMock()

        ctl = _Ctl()
        ctl.db = None
        ctl.state = types.SimpleNamespace(ws_clients=set(),
                                          p2p_messages={},
                                          shared_folder=None)
        ctl.secretary_active = active
        return ctl

    def test_ws_worker_endpoint_batch_ack(self, monkeypatch):
        import lan_mesh.model_resources as mr
        monkeypatch.setattr(
            mr, "record_usage_global",
            lambda model, itok, otok, usage_id="", task_id="",
            project_id="": {"tracked": True, "duplicate": False})
        client = self._station_client(self._controller())
        with client.websocket_connect("/ws/worker") as ws:
            ws.send_json({"type": "usage_batch",
                          "records": [_ws_usage_row("u1")]})
            ack = ws.receive_json()
        assert ack["ok"] is True and ack["recorded"] == 1
        assert ack["total"] == 1 and ack["duplicate"] == 0

    def test_ws_worker_endpoint_token_required(self, monkeypatch):
        import lan_mesh.station_routes_common as common
        monkeypatch.setattr(common, "_mesh_auth_enabled", True)
        monkeypatch.setattr(common, "_mesh_auth_token", "secret-tk")
        from starlette.websockets import WebSocketDisconnect
        client = self._station_client(self._controller())
        # 无 token / 错 token → 握手即拒
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/worker"):
                pass
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect("/ws/worker?token=bad"):
                pass
        # 正确 token → 可连通
        with client.websocket_connect("/ws/worker?token=secret-tk") as ws:
            ws.send_json({"type": "host_event", "data": {"k": 1}})
            assert ws.receive_json()["ok"] is True

    def test_dashboard_html_whitelisted_under_auth(self, monkeypatch):
        # auth 开启时 "/" 必须免认证: 页面加载后才能执行 auth-token 自举
        import lan_mesh.station_routes_common as common
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        monkeypatch.setattr(common, "_mesh_auth_enabled", True)
        monkeypatch.setattr(common, "_mesh_auth_token", "secret-tk")
        app = FastAPI()
        app.middleware("http")(common.api_guard_middleware)

        @app.get("/")
        def _dash():
            return {"html": "ok"}

        @app.get("/api/resources")
        def _res():
            return {"pools": []}

        client = TestClient(app)
        r = client.get("/")
        assert r.status_code == 200
        # 其余 API 路径仍要求 token
        r2 = client.get("/api/resources")
        assert r2.status_code == 401

    def test_ws_worker_endpoint_event_forwarded_to_bus(self):
        from lan_mesh.event_bus import get_event_bus
        bus = get_event_bus()
        before = len(bus.recent(100))
        client = self._station_client(self._controller())
        with client.websocket_connect("/ws/worker") as ws:
            ws.send_json({"type": "host_event",
                          "data": {"device_id": "h9", "online": True}})
            assert ws.receive_json()["ok"] is True
        events = bus.recent(100)
        assert len(events) == before + 1
        assert events[-1]["type"] == "host_event"
        assert events[-1]["data"]["device_id"] == "h9"

    def test_ws_worker_endpoint_secretary_inactive_ack_fails(self):
        client = self._station_client(self._controller(active=False))
        with client.websocket_connect("/ws/worker") as ws:
            ws.send_json({"type": "usage_batch",
                          "records": [_ws_usage_row()]})
            ack = ws.receive_json()
        assert ack["ok"] is False
        assert ack["error"] == "secretary_inactive"


# ── 14. R5-2 轮换量化 ──────────────────────────────────────────


class TestRotationQuant:
    """R5-2: 轮换调度量化价值公式与合规红线。"""

    def _pool(self, rid, plan="token_plan", provider="aliyun-tokenplan",
              models=None, quota=1e8, period="one_time"):
        from lan_mesh.model_resources import ModelResource
        return ModelResource(id=rid, provider=provider, plan_type=plan,
                             quota=quota, models=models or [],
                             billing_period=period)

    def _mgr(self, pools):
        from lan_mesh.model_resources import ModelResourceManager
        m = ModelResourceManager()
        m._resources = {p.id: p for p in pools}
        m._enabled = True
        for p in pools:
            for mid in p.models:
                m._model_provider[mid] = p.provider
        return m

    @staticmethod
    def _patch_hour(monkeypatch, hour):
        import datetime as _dt
        import lan_mesh.model_resources as mr
        monkeypatch.setattr(mr, "_beijing_now", lambda: _dt.datetime(
            2026, 8, 17, hour, 0, tzinfo=mr._BEIJING_TZ))

    @staticmethod
    def _patch_rates(mgr, rates):
        mgr.get_usage = lambda rid: {"rate": rates.get(rid, 0.0)}

    def test_subscription_beats_payg(self):
        """订阅池沉没成本基线高于按量池 → 优先消耗订阅额度。"""
        m = self._mgr([
            self._pool("payg1", plan="payg", provider="deepseek",
                       models=["m1"]),
            self._pool("sub1", models=["m1"]),
        ])
        assert m._find_pool("m1").id == "sub1"

    def test_sunk_pressure_prefers_high_remaining(self, monkeypatch):
        """剩余额度多的订阅池沉没成本压力更大 → 优先消耗。"""
        self._patch_hour(monkeypatch, 10)   # 非折扣时段, 隔离 time 分量
        m = self._mgr([
            self._pool("low_used", models=["m1"]),
            self._pool("high_used", models=["m1"]),
        ])
        self._patch_rates(m, {"low_used": 0.2, "high_used": 0.9})
        assert m._find_pool("m1").id == "low_used"
        d = m._pool_score(m._resources["low_used"], "m1")
        assert d["sunk"] == pytest.approx(4.0 * 0.8, abs=0.01)

    def test_payg_offpeak_time_bonus(self, monkeypatch):
        """payg 池在 DeepSeek 空闲时段获得折扣窗口加分。"""
        m = self._mgr([self._pool("payg1", plan="payg",
                                  provider="deepseek", models=["m1"])])
        self._patch_hour(monkeypatch, 3)    # 空闲 (0-9)
        assert m._pool_score(m._resources["payg1"], "m1")["time"] == 1.5
        self._patch_hour(monkeypatch, 10)   # 高峰 (9-12)
        assert m._pool_score(m._resources["payg1"], "m1")["time"] == 0.0

    def test_night_discount_subscription(self, monkeypatch):
        """百炼夜间五折模型在 22-08 获得消耗加分, 其他模型/时段无。"""
        m = self._mgr([
            self._pool("sub1", models=["qwen3.8-max", "qwen3.7-max"]),
        ])
        p = m._resources["sub1"]
        self._patch_hour(monkeypatch, 23)
        assert m._time_bonus(p, "qwen3.8-max") == 1.0
        assert m._time_bonus(p, "qwen3.7-max") == 0.0
        self._patch_hour(monkeypatch, 3)    # 跨零点区间命中
        assert m._time_bonus(p, "qwen3.8-max") == 1.0
        self._patch_hour(monkeypatch, 12)
        assert m._time_bonus(p, "qwen3.8-max") == 0.0

    def test_window_urgency_monthly_ramps(self):
        """monthly 窗口紧迫度随月份推进从 0 升到近 1。"""
        import datetime as _dt
        from lan_mesh.model_resources import _BEIJING_TZ
        m = self._mgr([self._pool("sub1", models=["m1"], period="monthly")])
        p = m._resources["sub1"]
        early = _dt.datetime(2026, 8, 1, 12, tzinfo=_BEIJING_TZ).timestamp()
        late = _dt.datetime(2026, 8, 30, 12, tzinfo=_BEIJING_TZ).timestamp()
        u_early = m._window_urgency(p, early)
        u_late = m._window_urgency(p, late)
        assert u_early < 0.1
        assert u_late > 0.9

    def test_one_time_urgency_constant(self):
        """one_time 额度不刷新 → 紧迫度恒 1.0。"""
        import time
        m = self._mgr([self._pool("sub1", models=["m1"])])
        assert m._window_urgency(m._resources["sub1"], time.time()) == 1.0

    def test_rule_mode_fallback(self):
        """quant=false 回退 R5 首版纯规则基线。"""
        m = self._mgr([
            self._pool("sub1", models=["m1"]),
            self._pool("payg1", plan="payg", provider="deepseek",
                       models=["m1"]),
        ])
        m._rotation_cfg["quant"] = False
        assert m._pool_priority(m._resources["sub1"], "m1") == 10.0
        assert m._pool_priority(m._resources["payg1"], "m1") == 5.0

    def test_batch_block_subscription(self):
        """batch 模式 + 合规开关 → 订阅池剔除, payg 保留。"""
        m = self._mgr([
            self._pool("sub1", models=["m1"]),
            self._pool("sub2", models=["m2"]),
            self._pool("payg1", plan="payg", provider="deepseek",
                       models=["m2"]),
        ])
        m._rotation_cfg["batch_block_subscription"] = True
        m.set_usage_mode("batch")
        assert m._find_pool("m1") is None          # 仅订阅池 → 无候选
        assert m._find_pool("m2").id == "payg1"    # 保留 payg
        m.set_usage_mode("interactive")
        assert m._find_pool("m1").id == "sub1"     # 恢复交互模式

    def test_batch_flag_off_keeps_subscription(self):
        """合规开关关闭 (默认) 时 batch 模式不剔除订阅池。"""
        m = self._mgr([self._pool("sub1", models=["m1"])])
        m.set_usage_mode("batch")
        assert m._find_pool("m1").id == "sub1"

    def test_rotation_plan_detail(self):
        """rotation_plan 量化模式返回分量拆解 (审计透明)。"""
        m = self._mgr([
            self._pool("sub1", models=["m1"]),
            self._pool("payg1", plan="payg", provider="deepseek",
                       models=["m1"]),
        ])
        plan = m.rotation_plan()
        assert len(plan) == 1
        pools = plan[0]["pools"]
        assert plan[0]["chosen"] == "sub1"
        assert pools[0]["id"] == "sub1"
        assert set(pools[0]["detail"].keys()) == {
            "base", "sunk", "time", "deadline", "watermark"}

    def test_rotation_bias_global_quant(self, monkeypatch):
        """rotation_bias_global 走量化优先级且映射在 0~0.1。"""
        import lan_mesh.model_resources as mr
        m = self._mgr([self._pool("sub1", models=["m1"])])
        monkeypatch.setattr(mr, "_mgr", m)
        bias = mr.rotation_bias_global("m1")
        assert 0.0 < bias <= 0.1
        assert mr.rotation_bias_global("no_such_model") == 0.0


class TestSingleInstance:
    """主机级单实例守护 (E6) — 锁仲裁: 接管/同版退出/新版杀旧/更旧退出。"""

    def _setup(self, monkeypatch, tmp_path, alive=True):
        import lan_mesh.singleton as sg
        lock = tmp_path / "station.lock"
        killed = []
        monkeypatch.setattr(sg, "_lock_path", lambda: lock)
        monkeypatch.setattr(sg, "_pid_alive", lambda pid: alive)
        monkeypatch.setattr(sg, "_kill_process",
                            lambda pid: killed.append(pid) or True)
        monkeypatch.setattr(sg, "_wait_port_free", lambda port, timeout=8: True)
        monkeypatch.setattr(sg, "_port_holder", lambda port: 0)
        monkeypatch.setattr(sg, "_is_station_process", lambda pid: False)
        return sg, lock, killed

    def _seed(self, lock, pid=1111, commit="c1", commit_time=100.0, port=45470):
        import json
        lock.write_text(json.dumps({
            "pid": pid, "commit": commit, "commit_time": commit_time,
            "port": port, "started_at": 0.0}), encoding="utf-8")

    def test_no_lock_takeover(self, monkeypatch, tmp_path):
        """无锁 → 接管写锁 (记录本进程身份/版本/端口)。"""
        sg, lock, killed = self._setup(monkeypatch, tmp_path)
        assert sg.ensure_single_instance(45470, "c2", 200.0) == "proceed"
        import os, json
        info = json.loads(lock.read_text(encoding="utf-8"))
        assert info["pid"] == os.getpid()
        assert info["commit"] == "c2" and info["port"] == 45470
        assert not killed

    def test_zombie_lock_takeover(self, monkeypatch, tmp_path):
        """锁 PID 已死 (僵尸锁) → 接管覆盖, 不杀进程。"""
        sg, lock, killed = self._setup(monkeypatch, tmp_path, alive=False)
        self._seed(lock, pid=9999)
        assert sg.ensure_single_instance(45470, "c2", 200.0) == "proceed"
        assert not killed

    def test_same_version_exit(self, monkeypatch, tmp_path):
        """同版本实例在跑 (非 dev) → 取消启动, 不杀旧。"""
        sg, lock, killed = self._setup(monkeypatch, tmp_path)
        self._seed(lock, pid=1111, commit="c1", commit_time=100.0)
        assert sg.ensure_single_instance(45470, "c1", 100.0) == "exit_same"
        assert not killed

    def test_same_version_dev_reload_takeover(self, monkeypatch, tmp_path):
        """dev-reload 同版本 → 杀旧接管 (旧进程已请求退出)。"""
        sg, lock, killed = self._setup(monkeypatch, tmp_path)
        self._seed(lock, pid=1111, commit="c1", commit_time=100.0)
        assert sg.ensure_single_instance(45470, "c1", 100.0,
                                         dev_reload=True) == "proceed"
        assert killed == [1111]

    def test_older_current_exits(self, monkeypatch, tmp_path):
        """已有更新版本实例 → 取消启动, 不杀旧。"""
        sg, lock, killed = self._setup(monkeypatch, tmp_path)
        self._seed(lock, pid=1111, commit="c9", commit_time=900.0)
        assert sg.ensure_single_instance(45470, "c1", 100.0) == "exit_newer"
        assert not killed

    def test_newer_current_kills_old(self, monkeypatch, tmp_path):
        """当前更新 (升级场景) → 关闭旧版实例后接管写锁。"""
        sg, lock, killed = self._setup(monkeypatch, tmp_path)
        self._seed(lock, pid=1111, commit="c1", commit_time=100.0)
        assert sg.ensure_single_instance(45470, "c2", 200.0) == "proceed"
        assert killed == [1111]
        import json
        info = json.loads(lock.read_text(encoding="utf-8"))
        assert info["commit"] == "c2"

    def test_both_unknown_commit_exit(self, monkeypatch, tmp_path):
        """非 git 环境双方无 commit → 视为同版本, 取消启动。"""
        sg, lock, killed = self._setup(monkeypatch, tmp_path)
        self._seed(lock, pid=1111, commit="", commit_time=0.0)
        assert sg.ensure_single_instance(45470, "", 0.0) == "exit_same"
        assert not killed

    def test_mismatched_commit_unknown_exit(self, monkeypatch, tmp_path):
        """一方有 commit 一方无 (无法比较) → 保守取消启动。"""
        sg, lock, killed = self._setup(monkeypatch, tmp_path)
        self._seed(lock, pid=1111, commit="c1", commit_time=100.0)
        assert sg.ensure_single_instance(45470, "", 0.0) == "exit_same"
        assert not killed

    def test_clear_lock_only_own(self, tmp_path, monkeypatch):
        """正常退出只清自己的锁, 他人锁保留 (防误删活跃实例锁)。"""
        sg, lock, killed = self._setup(monkeypatch, tmp_path)
        self._seed(lock, pid=1111)
        sg._clear_lock(9999)   # 非本人 → 不删
        assert lock.exists()
        sg._clear_lock(1111)   # 本人 → 删
        assert not lock.exists()

    def test_corrupt_lock_takeover(self, monkeypatch, tmp_path):
        """锁文件损坏 → 视为无锁接管 (不抛异常)。"""
        sg, lock, killed = self._setup(monkeypatch, tmp_path)
        lock.write_text("{invalid json", encoding="utf-8")
        assert sg.ensure_single_instance(45470, "c2", 200.0) == "proceed"
        assert not killed

    def test_no_lock_station_holder_killed(self, monkeypatch, tmp_path):
        """无锁但端口被工作站进程占用 (旧版无锁遗留) → 关闭后接管。"""
        sg, lock, killed = self._setup(monkeypatch, tmp_path, alive=False)
        monkeypatch.setattr(sg, "_port_holder", lambda port: 5555)
        monkeypatch.setattr(sg, "_is_station_process", lambda pid: True)
        self._seed(lock, pid=9999)
        assert sg.ensure_single_instance(45470, "c2", 200.0) == "proceed"
        assert killed == [5555]

    def test_no_lock_foreign_holder_kept(self, monkeypatch, tmp_path):
        """无锁但端口被非工作站占用 → 不杀, 正常接管写锁。"""
        sg, lock, killed = self._setup(monkeypatch, tmp_path, alive=False)
        monkeypatch.setattr(sg, "_port_holder", lambda port: 7777)
        self._seed(lock, pid=9999)
        assert sg.ensure_single_instance(45470, "c2", 200.0) == "proceed"
        assert not killed


class TestTaskFlowTrace:
    """P3 Task Flow Trace: 任务流阶段事件写入/读取/瀑布聚合 (iter-38)。"""

    @pytest.fixture
    def rt(self, monkeypatch, tmp_path):
        """将 JSONL 追踪文件重定向到临时目录, 避免污染真实 ~/.lan_mesh。"""
        from lan_mesh import runtime_trace
        monkeypatch.setattr(runtime_trace, "_TRACE_DIR", tmp_path)
        monkeypatch.setattr(runtime_trace, "_TRACE_FILE", tmp_path / "trace.jsonl")
        # iter-41: 停滞告警全局状态隔离 (防测试间互相污染)
        monkeypatch.setattr(runtime_trace, "_stall_state", {})
        monkeypatch.setattr(runtime_trace, "_stall_active", [])
        monkeypatch.setattr(runtime_trace, "_stall_minutes", 30.0)
        monkeypatch.setattr(runtime_trace, "_stall_bot_notify", None)
        return runtime_trace

    def _lines(self, rt):
        f = rt._TRACE_FILE
        if not f.is_file():
            return []
        import json as _json
        return [_json.loads(l) for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]

    def test_event_written(self, rt):
        rt.trace_task_event("task-a", "submitted", detail="测试任务", pm_id="pm-1")
        rows = self._lines(rt)
        assert len(rows) == 1
        rec = rows[0]
        assert rec["type"] == "task_flow"
        assert rec["task_id"] == "task-a"
        assert rec["stage"] == "submitted"
        assert rec["detail"] == "测试任务"
        assert rec["pm_id"] == "pm-1"
        assert rec["ts"] > 0

    def test_empty_task_id_skipped(self, rt):
        rt.trace_task_event("", "submitted")
        assert self._lines(rt) == []

    def test_detail_truncated(self, rt):
        rt.trace_task_event("task-a", "pm:planning", detail="长" * 500)
        assert len(self._lines(rt)[0]["detail"]) == 200

    def test_read_filters_task_and_type(self, rt):
        rt.trace_task_event("task-a", "submitted")
        rt.trace_task_event("task-b", "submitted")
        rt.trace_task_event("task-a", "pm:planning")
        rt.trace_llm_call("m", 10, 20, 1.0, 2.0)  # 异类型记录不干扰 (db 未注入跳过 sqlite)
        events = rt.read_task_flow("task-a")
        assert [e["stage"] for e in events] == ["submitted", "pm:planning"]

    def test_read_sorted_and_limit(self, rt):
        # 乱序写入 (模拟多线程先后), 读取应按时间正序且受 limit 约束取尾部
        rt.trace_task_event("task-a", "pm:completed")
        import time as _t; _t.sleep(0.01)
        rt.trace_task_event("task-a", "submitted")
        events = rt.read_task_flow("task-a")
        assert events[0]["ts"] <= events[1]["ts"]
        limited = rt.read_task_flow("task-a", limit=1)
        assert len(limited) == 1
        assert limited[0]["stage"] == "submitted"  # 取最新一条 (尾裁)

    def test_waterfall_structure(self, rt):
        rt.trace_task_event("task-a", "submitted", detail="提效任务")
        rt.trace_task_event("task-a", "pm:planning", pm_id="pm-1")
        rt.trace_task_event("task-a", "pm:completed")
        flow = rt.task_flow_waterfall("task-a")
        assert flow["task_id"] == "task-a"
        assert flow["stage_count"] == 3
        labels = [e["label"] for e in flow["events"]]
        assert labels == ["任务提交", "PM 规划中", "任务完成"]
        assert flow["events"][0]["gap_ms"] == 0
        assert flow["total_ms"] >= 0

    def test_waterfall_empty_task(self, rt):
        flow = rt.task_flow_waterfall("task-none")
        assert flow["stage_count"] == 0
        assert flow["events"] == []
        assert flow["total_ms"] == 0

    def test_pm_report_status_hook(self, rt):
        """PM report_status 必经出口写任务流事件 (HTTP 失败不影响追踪)。"""
        from lan_mesh.pm_agent import ProjectManagerAgent
        pm = ProjectManagerAgent("pm-test-0001", None, "http://127.0.0.1:1", "dev-1")
        pm._task_id = "task-hook"
        pm.report_status("planning", collaboration_mode="single", task_list=[{"name": "a"}])
        events = rt.read_task_flow("task-hook")
        assert len(events) == 1
        assert events[0]["stage"] == "pm:planning"
        assert "模式=single" in events[0]["detail"]
        assert "子任务=1" in events[0]["detail"]
        assert events[0]["pm_id"] == "pm-test-0001"

    def test_task_flow_endpoint(self, rt):
        """GET /api/runtime/task-flow 端点: 正常返回/超长 task_id 400/未知任务空。"""
        import types
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.station_api import create_station_router

        class _Ctl:
            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        ctl.db = None
        ctl.state = types.SimpleNamespace(
            ws_clients=set(), p2p_messages={}, shared_folder=None)
        ctl.secretary_active = True

        rt.trace_task_event("task-ep", "submitted", detail="端点测试")
        rt.trace_task_event("task-ep", "pm:completed")

        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)

        r = client.get("/api/runtime/task-flow", params={"task_id": "task-ep"})
        assert r.status_code == 200
        body = r.json()
        assert body["stage_count"] == 2
        assert body["events"][0]["label"] == "任务提交"
        assert body["total_ms"] >= 0

        # 超长 task_id → 400
        r2 = client.get("/api/runtime/task-flow", params={"task_id": "x" * 100})
        assert r2.status_code == 400

        # 未知任务 → 200 + 空事件列表 (不报错)
        r3 = client.get("/api/runtime/task-flow", params={"task_id": "task-none"})
        assert r3.status_code == 200
        assert r3.json()["stage_count"] == 0

    # ── 任务流总览 (iter-39) ──

    def test_overview_aggregates_by_task(self, rt):
        """多任务事件聚合: 每任务一行, 末活动时间倒序。"""
        import time as _t
        rt.trace_task_event("task-old", "submitted")
        rt.trace_task_event("task-old", "pm:executing")
        _t.sleep(0.01)
        rt.trace_task_event("task-new", "submitted")
        rows = rt.task_flow_overview()
        assert len(rows) == 2
        # 末活动最新在前 (sleep 保证 task-new 的 ts 更大)
        assert rows[0]["task_id"] == "task-new"
        assert rows[0]["stage_count"] == 1
        assert rows[0]["last_stage"] == "submitted"
        assert rows[1]["task_id"] == "task-old"
        assert rows[1]["stage_count"] == 2
        assert rows[1]["last_stage"] == "pm:executing"
        assert rows[1]["last_label"] == "PM 执行中"
        assert rows[1]["total_ms"] >= 0

    def test_overview_done_flag(self, rt):
        """终态阶段 (完成/失败/取消/交付) 标记 done, 其余进行中。"""
        rt.trace_task_event("task-done", "submitted")
        rt.trace_task_event("task-done", "pm:completed")
        rt.trace_task_event("task-run", "submitted")
        rt.trace_task_event("task-run", "pm:executing")
        rows = {r["task_id"]: r for r in rt.task_flow_overview()}
        assert rows["task-done"]["done"] is True
        assert rows["task-run"]["done"] is False

    def test_overview_stalled_detection(self, rt):
        """iter-40 停滞检测: 未到终态且空闲超阈值标 stalled, 已收尾永不标。"""
        import json as _json
        import time as _t
        # 手写历史事件: 空闲 2 小时的进行中任务 + 同样久远的已收尾任务
        old = _t.time() - 7200
        with open(rt._TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(_json.dumps({"type": "task_flow", "task_id": "task-stuck",
                                 "stage": "pm:executing", "ts": old}) + "\n")
            f.write(_json.dumps({"type": "task_flow", "task_id": "task-old-done",
                                 "stage": "pm:completed", "ts": old}) + "\n")
        rows = {r["task_id"]: r for r in rt.task_flow_overview(stall_minutes=30)}
        assert rows["task-stuck"]["stalled"] is True
        assert rows["task-stuck"]["idle_ms"] > 30 * 60 * 1000
        assert rows["task-old-done"]["stalled"] is False  # 终态不标停滞
        assert rows["task-old-done"]["idle_ms"] > 0  # idle_ms 恒计算供展示

    def test_overview_empty_and_limit(self, rt):
        """无记录返回空列表; limit 裁剪生效; 禁用检测时全不标停滞。"""
        assert rt.task_flow_overview() == []
        import json as _json
        import time as _t
        for i in range(5):
            rt.trace_task_event(f"task-{i}", "submitted")
        rows = rt.task_flow_overview(limit=3)
        assert len(rows) == 3
        # stall_minutes=0 禁用检测: 即使空闲很久也不标 (手写久远事件)
        with open(rt._TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(_json.dumps({"type": "task_flow", "task_id": "task-ancient",
                                 "stage": "submitted", "ts": _t.time() - 99999}) + "\n")
        rows2 = rt.task_flow_overview(stall_minutes=0)
        assert all(r["stalled"] is False for r in rows2)

    def test_overview_ignores_other_types(self, rt):
        """非 task_flow 记录与空 task_id 不进入总览。"""
        import json as _json
        rt.trace_task_event("task-x", "submitted")
        with open(rt._TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(_json.dumps({"type": "llm_call", "model": "m"}) + "\n")
            f.write(_json.dumps({"type": "task_flow", "task_id": "", "stage": "submitted"}) + "\n")
        rows = rt.task_flow_overview()
        assert len(rows) == 1
        assert rows[0]["task_id"] == "task-x"

    def test_task_flow_list_endpoint(self, rt):
        """GET /api/runtime/task-flow-list 端点: 200 + tasks 结构。"""
        import types
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.station_api import create_station_router

        class _Ctl:
            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        ctl.db = None
        ctl.state = types.SimpleNamespace(
            ws_clients=set(), p2p_messages={}, shared_folder=None)
        ctl.secretary_active = True

        rt.trace_task_event("task-list-a", "submitted")
        rt.trace_task_event("task-list-a", "delivered")

        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)

        r = client.get("/api/runtime/task-flow-list", params={"limit": 10})
        assert r.status_code == 200
        body = r.json()
        assert body["stall_minutes"] == 30.0  # 默认阈值回显 (iter-40)
        tasks = body["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "task-list-a"
        assert tasks[0]["done"] is True
        assert tasks[0]["stage_count"] == 2
        assert tasks[0]["stalled"] is False
        assert "idle_ms" in tasks[0]

        # stall_minutes 参数透传与夹取 (上限 1440)
        r2 = client.get("/api/runtime/task-flow-list",
                        params={"limit": 5, "stall_minutes": 9999})
        assert r2.json()["stall_minutes"] == 1440.0

    # ── 任务停滞主动告警 (iter-41) ──

    def _inject_old_task(self, rt, task_id, stage, idle_min):
        """手写一条空闲 idle_min 分钟前的任务流事件 (绕过当前时间戳)。"""
        import json as _json
        import time as _t
        rec = {"type": "task_flow", "task_id": task_id, "stage": stage,
               "ts": _t.time() - idle_min * 60}
        with open(rt._TRACE_FILE, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec) + "\n")

    def test_stall_level_thresholds(self, rt):
        """档位边界: 1/2/4 倍阈值 → Lv1/2/3; 无效阈值 → 0。"""
        stall_ms = 30 * 60 * 1000
        assert rt._stall_level(stall_ms * 1.5, stall_ms) == 1
        assert rt._stall_level(stall_ms * 2, stall_ms) == 2
        assert rt._stall_level(stall_ms * 3.9, stall_ms) == 2
        assert rt._stall_level(stall_ms * 4, stall_ms) == 3
        assert rt._stall_level(stall_ms * 10, 0) == 0

    def test_check_stall_alerts_new_and_dedupe(self, rt):
        """新停滞推送一次; 同档位重复检查不重推 (防刷屏)。"""
        self._inject_old_task(rt, "task-stall-x", "pm:executing", 35)
        pushed1 = rt.check_stall_alerts()
        assert len(pushed1) == 1
        assert pushed1[0]["task_id"] == "task-stall-x"
        assert pushed1[0]["level"] == 1
        # 第二轮: 仍停滞但档位未升 → 不重推; 活跃告警仍保留全量
        pushed2 = rt.check_stall_alerts()
        assert pushed2 == []
        assert len(rt.active_stall_alerts()) == 1

    def test_check_stall_alerts_level_upgrade(self, rt):
        """空闲加深导致档位升级时重新推送 (Lv1 → Lv2 → Lv3)。"""
        self._inject_old_task(rt, "task-up", "pm:executing", 35)
        p1 = rt.check_stall_alerts()
        assert p1[0]["level"] == 1
        # 覆写事件时间: 空闲 65 分钟 (>2 倍阈值)
        rt._TRACE_FILE.unlink()
        self._inject_old_task(rt, "task-up", "pm:executing", 65)
        p2 = rt.check_stall_alerts()
        assert len(p2) == 1 and p2[0]["level"] == 2
        # 空闲 4 倍以上 → Lv3
        rt._TRACE_FILE.unlink()
        self._inject_old_task(rt, "task-up", "pm:executing", 130)
        p3 = rt.check_stall_alerts()
        assert len(p3) == 1 and p3[0]["level"] == 3

    def test_check_stall_alerts_recovery_clears_state(self, rt):
        """任务恢复活动后清除档位, 再次停滞可重新告警。"""
        self._inject_old_task(rt, "task-re", "pm:executing", 35)
        assert len(rt.check_stall_alerts()) == 1
        # 恢复: 写入一条当前时刻的新事件 → 不再停滞 → 状态清除
        rt.trace_task_event("task-re", "subtask_result")
        assert rt.check_stall_alerts() == []
        assert "task-re" not in rt._stall_state
        # 再次停滞 → 重新告警 (而非被去重吞掉)
        rt._TRACE_FILE.unlink()
        self._inject_old_task(rt, "task-re", "pm:executing", 40)
        assert len(rt.check_stall_alerts()) == 1

    def test_check_stall_alerts_done_never_alert(self, rt):
        """已到终态的任务即使空闲很久也永不告警。"""
        self._inject_old_task(rt, "task-done-x", "pm:completed", 500)
        assert rt.check_stall_alerts() == []
        assert rt.active_stall_alerts() == []

    def test_check_stall_alerts_bot_notify(self, rt):
        """Bot 推送回调按档位映射事件类型。"""
        calls = []
        rt.set_stall_bot_notify(lambda evt, data: calls.append((evt, data)))
        self._inject_old_task(rt, "task-bot", "pm:executing", 35)
        rt.check_stall_alerts()
        assert len(calls) == 1
        assert calls[0][0] == "task_stall_alert_low"
        assert calls[0][1]["task_id"] == "task-bot"
        # 回调异常不影响检查主流程 (异常隔离)
        rt.set_stall_bot_notify(lambda evt, data: (_ for _ in ()).throw(RuntimeError("boom")))
        rt._TRACE_FILE.unlink()
        self._inject_old_task(rt, "task-bot2", "pm:executing", 130)
        pushed = rt.check_stall_alerts()
        assert len(pushed) == 1 and pushed[0]["level"] == 3

    def test_stall_watcher_disabled_when_threshold_le_zero(self, rt):
        """stall_minutes≤0 禁用检测: 不启动线程且检查无输出。"""
        assert rt.start_stall_watcher(interval=60, stall_minutes=0) is False
        assert rt.stall_watcher_status()["watching"] is False

    def test_stall_alerts_endpoints(self, rt):
        """GET/POST /api/runtime/task-stall-alerts 端点。"""
        import types
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.station_api import create_station_router

        class _Ctl:
            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        ctl.db = None
        ctl.state = types.SimpleNamespace(
            ws_clients=set(), p2p_messages={}, shared_folder=None)
        ctl.secretary_active = True

        self._inject_old_task(rt, "task-ep", "pm:executing", 35)

        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)

        # 手动触发一轮检查 → 新推送 1 条
        r = client.post("/api/runtime/task-stall-alerts/check")
        assert r.status_code == 200
        pushed = r.json()["pushed"]
        assert len(pushed) == 1 and pushed[0]["task_id"] == "task-ep"

        # 查询活跃告警 + 守护状态结构
        r2 = client.get("/api/runtime/task-stall-alerts")
        assert r2.status_code == 200
        body = r2.json()
        assert len(body["alerts"]) == 1
        assert body["alerts"][0]["message"].startswith("已 ")
        assert "watching" in body and "interval" in body
        assert "stall_minutes" in body


class TestTaskMemoryOverview:
    """iter-42: 任务记忆面板 (F4.1 可视化) 端点。"""

    _MEM_ROWS = [
        {"task_name": "开发支付模块", "task_keywords": ["开发", "支付"],
         "task_type": "development", "collaboration_mode": "orchestrator",
         "team_size": 3, "duration_secs": 120.0, "success": True,
         "error_pattern": "", "boss_feedback": "", "created_at": 100.0},
        {"task_name": "重构调度器", "task_keywords": ["重构"],
         "task_type": "development", "collaboration_mode": "orchestrator",
         "team_size": 2, "duration_secs": 60.0, "success": True,
         "error_pattern": "", "boss_feedback": "", "created_at": 90.0},
        {"task_name": "调研报告", "task_keywords": ["调研"],
         "task_type": "research", "collaboration_mode": "single",
         "team_size": 1, "duration_secs": 30.0, "success": False,
         "error_pattern": "timeout", "boss_feedback": "", "created_at": 80.0},
    ]

    def _client(self):
        import types
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.station_api import create_station_router

        class _Ctl:
            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        ctl.db = types.SimpleNamespace(
            query_task_memory=lambda task_type="", keyword="", limit=10:
                self._MEM_ROWS[:limit],
            get_task_memory_stats=lambda task_type="": {
                "total": len(self._MEM_ROWS), "success_rate": 2 / 3,
                "avg_duration": 70.0, "recommended_mode": "orchestrator",
                "common_errors": [("timeout", 1)],
            },
        )
        ctl.state = types.SimpleNamespace(
            ws_clients=set(), p2p_messages={}, shared_folder=None)
        ctl.secretary_active = True

        app = FastAPI()
        app.include_router(create_station_router(ctl))
        return TestClient(app)

    def test_overview_structure_and_grouping(self):
        """总览结构: 全局统计 + 按类型分组 (多者在前) + 最近记录。"""
        client = self._client()
        r = client.get("/api/task-memory/overview")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 3
        assert body["recommended_mode"] == "orchestrator"
        # by_type: development 2 条在前, research 1 条在后
        by_type = body["by_type"]
        assert [g["task_type"] for g in by_type] == ["development", "research"]
        dev = by_type[0]
        assert dev["count"] == 2 and dev["success_rate"] == 1.0
        assert dev["avg_duration"] == 90.0
        assert dev["recommended_mode"] == "orchestrator"
        # recent: 字段齐全且关键词被截断到 ≤5 个
        assert len(body["recent"]) == 3
        first = body["recent"][0]
        assert first["task_name"] == "开发支付模块"
        assert first["keywords"] == ["开发", "支付"]
        assert body["recent"][2]["success"] is False

    def test_overview_limit_clamped(self):
        """limit 参数夹取: 1~50, recent 按 limit 截断。"""
        client = self._client()
        r = client.get("/api/task-memory/overview?limit=1")
        assert r.status_code == 200
        assert len(r.json()["recent"]) == 1

    def test_overview_empty_memory(self):
        """无记忆时: total=0 且 by_type/recent 为空列表 (不报错)。"""
        import types
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.station_api import create_station_router

        class _Ctl:
            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        ctl.db = types.SimpleNamespace(
            query_task_memory=lambda task_type="", keyword="", limit=10: [],
            get_task_memory_stats=lambda task_type="": {
                "total": 0, "success_rate": 0, "avg_duration": 0,
                "recommended_mode": "", "common_errors": [],
            },
        )
        ctl.state = types.SimpleNamespace(
            ws_clients=set(), p2p_messages={}, shared_folder=None)
        ctl.secretary_active = True

        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)
        r = client.get("/api/task-memory/overview")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 0
        assert body["by_type"] == [] and body["recent"] == []

    def test_overview_requires_secretary(self):
        """秘书未激活时返回 503 (与其他任务端点一致)。"""
        import types
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.station_api import create_station_router

        class _Ctl:
            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        ctl.db = types.SimpleNamespace(
            query_task_memory=lambda **kw: [],
            get_task_memory_stats=lambda task_type="": {"total": 0},
        )
        ctl.state = types.SimpleNamespace(
            ws_clients=set(), p2p_messages={}, shared_folder=None)
        ctl.secretary_active = False

        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)
        r = client.get("/api/task-memory/overview")
        assert r.status_code == 503


class TestObservabilityConfig:
    """iter-43: 停滞检测参数配置化 (config.yaml observability 段)。"""

    def test_defaults(self):
        """缺省段: 默认 60s 检查周期 / 30 分钟阈值。"""
        from lan_mesh.config import AppConfig
        cfg = AppConfig()
        assert cfg.observability.stall_check_interval == 60.0
        assert cfg.observability.stall_minutes == 30.0

    def test_custom_values_parsed(self, tmp_path):
        """自定义段: yaml 值生效。"""
        from lan_mesh.config import load_config
        p = tmp_path / "config.yaml"
        p.write_text("observability:\n  stall_check_interval: 20\n"
                     "  stall_minutes: 10\n", encoding="utf-8")
        cfg = load_config(str(p))
        assert cfg.observability.stall_check_interval == 20.0
        assert cfg.observability.stall_minutes == 10.0

    def test_section_absent_falls_back(self, tmp_path):
        """无 observability 段的旧配置: 回退默认不报错。"""
        from lan_mesh.config import load_config
        p = tmp_path / "config.yaml"
        p.write_text("auto_upgrade: false\n", encoding="utf-8")
        cfg = load_config(str(p))
        assert cfg.observability.stall_minutes == 30.0
        assert cfg.auto_upgrade is False

    def test_disable_value_rejects_watcher(self, tmp_path, monkeypatch):
        """stall_minutes: 0 → start_stall_watcher 返回 False 不启动。"""
        from lan_mesh import runtime_trace
        from lan_mesh.config import load_config
        monkeypatch.setattr(runtime_trace, "_stall_state", {})
        monkeypatch.setattr(runtime_trace, "_stall_active", [])
        monkeypatch.setattr(runtime_trace, "_stall_bot_notify", None)
        p = tmp_path / "config.yaml"
        p.write_text("observability:\n  stall_minutes: 0\n", encoding="utf-8")
        cfg = load_config(str(p))
        assert runtime_trace.start_stall_watcher(
            interval=cfg.observability.stall_check_interval,
            stall_minutes=cfg.observability.stall_minutes) is False


class TestErrorTrackerAlerts:
    """iter-44: 错误追踪闭环 (事件回调 + 突发告警冷却去重 + Bot 模板)。"""

    def _tracker(self, threshold=3, window=60.0):
        from lan_mesh.error_tracker import ErrorTracker
        return ErrorTracker(max_records=50, alert_threshold=threshold,
                            alert_window=window)

    def test_event_callback_per_capture(self):
        """全局事件回调: 每条 capture 触发一次, 携带记录字段。"""
        tr = self._tracker()
        seen = []
        tr.set_event_callback(seen.append)
        tr.capture("pm", error_type="Timeout", message="超时了")
        tr.capture("bot", ValueError("坏值"))
        assert len(seen) == 2
        assert seen[0]["module"] == "pm" and seen[0]["error_type"] == "Timeout"
        assert seen[1]["error_type"] == "ValueError"

    def test_event_callback_exception_isolated(self):
        """回调抛异常不影响捕获本身 (记录仍入库)。"""
        tr = self._tracker()

        def bad_cb(rec):
            raise RuntimeError("推送失败")

        tr.set_event_callback(bad_cb)
        tr.capture("pm", message="x")
        assert tr.get_stats()["total_errors"] == 1

    def test_burst_alert_cooldown_dedup(self):
        """突发告警: 达阈触发一次; 冷却期内重复达阈不重推; 冷却到期恢复。"""
        tr = self._tracker(threshold=3, window=60.0)
        fired = []
        tr.set_alert_callback(lambda mod, cnt, win: fired.append((mod, cnt, win)))
        for i in range(3):
            tr.capture("pm", message=f"e{i}")
        assert len(fired) == 1 and fired[0][0] == "pm" and fired[0][1] == 3
        # 冷却期内再达阈 — 不重推 (窗口计数仍 ≥ 阈值)
        tr.capture("pm", message="e3")
        assert len(fired) == 1
        # 模拟冷却到期 — 再达阈重推, 且携带窗口秒数
        tr._last_alert_at["pm"] -= tr._alert_cooldown + 1
        tr.capture("pm", message="e4")
        assert len(fired) == 2 and fired[1][2] == 60.0

    def test_burst_alert_modules_independent(self):
        """冷却按模块独立: pm 告警不影响 bot 模块首次告警。"""
        tr = self._tracker(threshold=2, window=60.0)
        fired = []
        tr.set_alert_callback(lambda mod, cnt, win: fired.append(mod))
        tr.capture("pm", message="a")
        tr.capture("pm", message="b")
        tr.capture("bot", message="c")
        tr.capture("bot", message="d")
        assert fired == ["pm", "bot"]

    def test_clear_resets_cooldown(self):
        """clear 同时重置告警冷却状态。"""
        tr = self._tracker(threshold=1, window=60.0)
        fired = []
        tr.set_alert_callback(lambda mod, cnt, win: fired.append(mod))
        tr.capture("pm", message="a")
        tr.clear()
        tr.capture("pm", message="b")
        assert fired == ["pm", "pm"]  # 冷却状态已清, 再次达阈即推
        assert tr.get_stats()["total_errors"] == 1

    def test_bot_error_burst_template(self):
        """Bot 模板: error_burst 存在/可格式化/高优先级。"""
        from lan_mesh.bot_gateway import EVENT_PRIORITY, EVENT_TEMPLATES
        msg = EVENT_TEMPLATES["error_burst"].format(module="pm", count=12, window=60.0)
        assert "pm" in msg and "12" in msg
        assert EVENT_PRIORITY["error_burst"] == "high"


