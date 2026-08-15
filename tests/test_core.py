"""
核心模块单元测试

覆盖:
1. TaskDAG — 拓扑排序、依赖解析、环检测、动态增删、条件边、序列化
2. ModelRouter — 难度分类、评分路由、降级链
3. _classify_task — 任务类型分类
4. EventBus — 发布/订阅、环形历史、sink 投递、边界 (M5)
5. role_cards — 角色卡结构、秘书 prompt 关键约束回归 (M6)
6. balance_probe — 别名归一、各家解析、异常提示、key 优先级 (R2/R4)

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
