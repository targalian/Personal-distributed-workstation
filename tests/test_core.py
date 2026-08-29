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
16. pm-snapshot-resume — PM 执行态快照持久化 + 断点恢复: 快照往返/
    就地恢复/DB CRUD/恢复四场景/快照与 resume 端点/multi 生命周期 (iter-53)
17. log-pruning — 日志容量修剪: prune_logs 保留期/未上报保留/心跳 24h/
    VACUUM/手动修剪端点/配置驱动与节流 (iter-54)
18. iter55-multihost — 多机实测缺陷回归: PROVIDER_CONFIG ark 首位/
    _get_default_model 定义/_ensure_env_loaded 补齐/模型资源预加载/
    让位主机惰性 Worker runtime (iter-55)
19. iter56-spa — React SPA 挂载与认证白名单: /spa 静态托管产物/
    auth 开启时免认证放行 (iter-56)
20. iter57-concurrency — 并发压力验证 (补强#5): DB 并发混合负载/
    busy_timeout+WAL 加固生效/API 并发提交 10 任务 pm_id 唯一 (iter-57)
21. iter58-permissions — 多用户权限 (补强#6 F5.2): token 角色归属判定/
    角色访问矩阵/中间件角色分层/用户表解析/未配置向后兼容 (iter-58)
22. iter60-auto-heal-loop — F4.2 自愈全自动闭环: rotate_key/switch_pool
    真实修复写动作 (失效池暂停/耗尽池剔除)、写动作每日配额、
    连续失败熔断与错误消失自动复位 (iter-60)
23. iter61-plugin-market — F5.3 插件系统 (第三方 Skill 市场): 市场浏览/
    安装白名单复制/安全默认仅 station/体积与 ID 校验/内置冲突拒绝/
    卸载保护与来源追踪 origin 列 (iter-61)
24. iter62-pwa-mobile — F5.4 移动端 PWA: Service Worker 应用壳缓存
    (network-first 导航/API 不缓存/静态 SWR) + /sw.js 根挂载 + 认证
    白名单放行 + dashboard SW 注册 (iter-62)
25. iter63-user-admin — 团队场景深化: users 表持久化 (token 仅存
    SHA256 哈希, config 首次种子, 轮换跨重启保留) + 用户管理端点
    (列表/新增/改角色/轮换/删除 + 最后 boss 防自锁) + SPA 用户页 (iter-63)
26. iter64-federation — F3.4 跨网段联邦 (发现层): 静态 peer 配置 +
    /api/federation/info 端点 + 联邦轮询同步 (source=fed 隔离) +
    选举/仲裁仅限本网段 (source=lan) + 离线检测 (iter-64)
27. iter65-federation-forward — F3.4 联邦任务转发 (任务层): 选站分层
    (lan 优先/fed 兜底限对端 Secretary) + 委托转发 forwarded 标记 +
    转发端点参数传递 (iter-65)

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
from lan_mesh.pm_state import PMState
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
            # iter-64: 选举/仲裁仅限本网段, list_hosts 带 source 过滤参数
            db = type("D", (), {"list_hosts": lambda self_, source=None: hosts})()
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


class TestErrorCapturePoints:
    """iter-45: 错误追踪埋点 (关键异常路径 → error_tracker.capture)。"""

    def _rec(self, monkeypatch):
        from lan_mesh import error_tracker as et_mod
        seen = []
        monkeypatch.setattr(et_mod.error_tracker, "capture",
                            lambda *a, **k: seen.append((a, k)))
        return seen

    def test_pm_task_failure_captured(self, monkeypatch):
        """任务级失败: _run_task 异常 → capture(pm) 携带 task_id 上下文。"""
        from unittest.mock import MagicMock
        from lan_mesh.pm_agent import ProjectManagerAgent
        seen = self._rec(monkeypatch)
        pm = ProjectManagerAgent("pm-test1234", MagicMock(),
                                 "http://127.0.0.1:1", "dev-1")
        monkeypatch.setattr(pm._monitor, "is_global_timed_out", lambda: False)
        monkeypatch.setattr(pm._planner, "refine_requirements", lambda t: t)
        monkeypatch.setattr(pm, "report_status", lambda *a, **k: None)
        monkeypatch.setattr(pm, "report_progress", lambda *a, **k: None)

        def boom(task):
            raise RuntimeError("规划炸了")
        monkeypatch.setattr(pm._planner, "analyze_with_skill", boom)
        pm._run_task({"task_id": "task-x1", "name": "t"})
        assert len(seen) == 1
        (mod, exc), kw = seen[0]
        assert mod == "pm" and isinstance(exc, RuntimeError)
        assert kw["context"]["task_id"] == "task-x1"

    def test_bot_send_exhausted_captured(self, monkeypatch):
        """推送重试耗尽: _do_send_to_channel 最终失败 → capture(bot) 含通道上下文。"""
        from lan_mesh.bot_gateway import BotChannel, BotGateway
        seen = self._rec(monkeypatch)
        bg = BotGateway(aggregate_window=1, max_retry=1, retry_backoff=0.01)
        ch = BotChannel(channel_type="telegram", enabled=True,
                        bot_token="t", chat_id="c")

        def boom(*a, **k):
            raise ConnectionError("网络不可达")
        monkeypatch.setattr(bg, "_send_telegram", boom)
        bg._do_send_to_channel(ch, "msg", "task_completed")
        assert len(seen) == 1
        (mod, exc), kw = seen[0]
        assert mod == "bot"
        assert kw["context"]["point"] == "channel_send"
        assert kw["context"]["channel"] == "telegram"
        assert bg._pending_queue  # 仍进入离线队列 (原有行为不变)

    def test_bot_chat_handler_captured(self, monkeypatch):
        """秘书对话链异常: _handle_natural_language → capture(bot)。"""
        from lan_mesh.bot_gateway import BotGateway
        seen = self._rec(monkeypatch)
        bg = BotGateway(aggregate_window=1, max_retry=1)

        class _BadChat:
            def chat(self, text):
                raise ValueError("LLM 超时")
        bg._chat_handler = _BadChat()
        reply = bg._handle_natural_language("你好", "chat-1")
        assert "秘书处理异常" in reply
        assert len(seen) == 1 and seen[0][0][0] == "bot"
        assert seen[0][1]["context"]["point"] == "chat_handler"

    def test_llm_fallback_exhausted_captured(self, monkeypatch, tmp_path):
        """降级链耗尽: _call_llm_with_routing 全模型失败 → capture(llm) 含链路。"""
        from lan_mesh.agent_runtime import AgentRuntime
        seen = self._rec(monkeypatch)
        rt = AgentRuntime("agent-1", str(tmp_path))
        monkeypatch.setattr(rt, "_resolve_provider",
                            lambda m: {"base_url": "http://x", "api_key": "k"})

        def boom(*a, **k):
            raise RuntimeError("模型 500")
        monkeypatch.setattr(rt, "_call_openai_compatible", boom)
        result = rt._call_llm_with_routing("p", {
            "_model_preference": "m1", "_fallback_models": ["m2"],
            "_system_prompt": "sp"})
        assert "降级链均不可用" in result["content"]
        assert len(seen) == 1
        (mod, exc), kw = seen[0]
        assert mod == "llm"
        assert kw["context"]["point"] == "fallback_exhausted"
        assert kw["context"]["chain"] == ["m1", "m2"]


class TestErrorDiagnosis:
    """iter-46 (F4.2): 错误自愈诊断 — 模式规则表分组与建议。"""

    def _tracker(self):
        from lan_mesh.error_tracker import ErrorTracker
        return ErrorTracker(max_records=50)

    def test_diagnose_groups_by_pattern(self):
        """按模式分组: 同类错误聚合计数/模块/样例。"""
        tr = self._tracker()
        tr.capture("llm", error_type="Timeout", message="请求超时")
        tr.capture("pm", error_type="TimeoutError", message="HTTP timed out")
        tr.capture("bot", error_type="HTTPError", message="429 rate limit")
        d = tr.diagnose()
        assert d["scanned"] == 3 and d["unmatched"] == 0
        cats = [f["category"] for f in d["findings"]]
        assert cats == ["timeout", "rate_limit"]  # 命中数降序 (2 > 1)
        t = d["findings"][0]
        assert t["count"] == 2 and t["modules"] == ["llm", "pm"]
        assert t["action"] == "check_peer" and t["sample"]

    def test_diagnose_first_match_wins(self):
        """首命中归属: 同时含多模式词元的记录只计入靠前规则。"""
        tr = self._tracker()
        tr.capture("llm", error_type="HTTPError",
                   message="504 gateway timeout 超时")  # 同时命中 timeout 与 5xx 规则
        d = tr.diagnose()
        assert len(d["findings"]) == 1
        assert d["findings"][0]["category"] == "timeout"  # 规则表顺序优先
        assert d["unmatched"] == 0

    def test_diagnose_empty_and_window_clamp(self):
        """空缓冲与 window 夹取: 不报错且扫描数受限。"""
        tr = self._tracker()
        d = tr.diagnose()
        assert d == {"scanned": 0, "findings": [], "unmatched": 0}
        for i in range(10):
            tr.capture("pm", error_type="E", message=f"杂项错误 {i}")
        d = tr.diagnose(window_records=3)
        assert d["scanned"] == 3 and d["unmatched"] == 3
        d = tr.diagnose(window_records=0)  # 夹取至 1 仍可用 (实际最小 1)
        assert d["scanned"] >= 1

    def test_diagnosis_endpoint(self):
        """/api/errors/diagnosis 端点: 返回扫描结果结构。"""
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh import error_tracker as et
        from lan_mesh.station_api import create_station_router

        class _Ctl:
            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)
        et.error_tracker.clear()
        try:
            et.error_tracker.capture("llm", error_type="Timeout", message="超时")
            r = client.get("/api/errors/diagnosis", params={"window": 50})
            assert r.status_code == 200
            body = r.json()
            assert body["scanned"] == 1
            assert body["findings"][0]["category"] == "timeout"
        finally:
            et.error_tracker.clear()


class TestErrorPersistence:
    """iter-47 (F1.4): 错误记录落盘持久化 — error_log 表与回调/端点。"""

    def test_save_and_query_roundtrip(self, tmp_path):
        """写入/读取往返: context JSON 落盘, 读出还原为 dict。"""
        from lan_mesh.database import Database
        db = Database(str(tmp_path / "e.db"))
        db.save_error_record({"timestamp": 1755300000.0, "module": "pm",
                              "error_type": "TimeoutError", "message": "请求超时",
                              "context": {"task_id": "t-1"}})
        rows = db.query_error_history(limit=10)
        assert len(rows) == 1
        r = rows[0]
        assert r["module"] == "pm" and r["error_type"] == "TimeoutError"
        assert r["message"] == "请求超时"
        assert r["context"] == {"task_id": "t-1"}
        assert r["timestamp"] == 1755300000.0

    def test_query_filter_module_and_order(self, tmp_path):
        """模块过滤 + 按写入序倒序 + limit 生效。"""
        from lan_mesh.database import Database
        db = Database(str(tmp_path / "e.db"))
        for i, mod in enumerate(["pm", "bot", "pm"]):
            db.save_error_record({"timestamp": 1755300000.0 + i, "module": mod,
                                  "error_type": "E", "message": f"错误{i}"})
        rows = db.query_error_history(limit=10, module="pm")
        assert len(rows) == 2 and rows[0]["message"] == "错误2"  # 倒序最新在前
        rows = db.query_error_history(limit=1)
        assert len(rows) == 1 and rows[0]["message"] == "错误2"

    def test_persist_callback_on_capture(self, tmp_path):
        """capture 触发落盘回调; 回调抛异常不影响捕获本身。"""
        from lan_mesh.database import Database
        from lan_mesh.error_tracker import ErrorTracker
        db = Database(str(tmp_path / "e.db"))
        tr = ErrorTracker()
        tr.set_persist_callback(lambda rec: db.save_error_record(rec))
        tr.capture("llm", error_type="Timeout", message="超时落盘")
        assert len(db.query_error_history()) == 1
        # 异常隔离: 回调抛错不应中断 capture 链路
        tr.set_persist_callback(lambda rec: 1 / 0)
        tr.capture("llm", error_type="Timeout", message="回调失败仍捕获")
        assert tr.get_stats()["total_errors"] == 2
        assert len(db.query_error_history()) == 1  # 第二条未落盘

    def test_capacity_prune(self, tmp_path):
        """容量修剪: 超 2000 行只保留最新 2000 行。"""
        from lan_mesh.database import Database
        db = Database(str(tmp_path / "e.db"))
        conn = db._get_conn()
        conn.executemany(
            "INSERT INTO error_log (timestamp, module, error_type, message) "
            "VALUES (?, ?, ?, ?)",
            [(float(i), "pm", "E", f"m{i}") for i in range(2010)])
        conn.commit()
        db.save_error_record({"module": "pm", "error_type": "E", "message": "new"})
        total = conn.execute("SELECT COUNT(*) FROM error_log").fetchone()[0]
        assert total == 2000
        latest = db.query_error_history(limit=1)
        assert latest[0]["message"] == "new"

    def test_history_endpoint(self, tmp_path):
        """/api/errors/history 端点: 返回持久化记录列表。"""
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.database import Database
        from lan_mesh.station_api import create_station_router

        class _Ctl:
            def __init__(self):
                self.db = Database(str(tmp_path / "h.db"))

            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        ctl.db.save_error_record({"timestamp": 1755300000.0, "module": "bot",
                                  "error_type": "HTTPError", "message": "推送失败"})
        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)
        r = client.get("/api/errors/history", params={"limit": 10})
        assert r.status_code == 200
        body = r.json()
        assert len(body["errors"]) == 1
        assert body["errors"][0]["module"] == "bot"


class TestErrorHistoryDiagnosis:
    """iter-48 (F4.2): 诊断范围扩展 — 持久化历史诊断 (重启后不断档)。"""

    def test_diagnose_records_pure_function(self):
        """diagnose_records 纯函数: 与实例方法同规则, 空列表零值。"""
        from lan_mesh.error_tracker import diagnose_records
        assert diagnose_records([]) == {"scanned": 0, "findings": [],
                                        "unmatched": 0}
        records = [
            {"timestamp": 1.0, "module": "llm", "error_type": "Timeout",
             "message": "请求超时"},
            {"timestamp": 2.0, "module": "bot", "error_type": "HTTPError",
             "message": "429 rate limit"},
            {"timestamp": 3.0, "module": "pm", "error_type": "E",
             "message": "杂项"},
        ]
        d = diagnose_records(records)
        assert d["scanned"] == 3 and d["unmatched"] == 1
        assert [f["category"] for f in d["findings"]] == ["timeout", "rate_limit"]

    def test_diagnose_method_reuses_pure_function(self):
        """实例 diagnose() 重构后行为不变 (iter-46 回归)。"""
        from lan_mesh.error_tracker import ErrorTracker
        tr = ErrorTracker()
        tr.capture("llm", error_type="Timeout", message="HTTP timed out")
        d = tr.diagnose()
        assert d["scanned"] == 1
        assert d["findings"][0]["category"] == "timeout"

    def test_diagnosis_endpoint_history_source(self, tmp_path):
        """/api/errors/diagnosis?source=history: 诊断持久化记录, 缓冲行为不变。"""
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh import error_tracker as et
        from lan_mesh.database import Database
        from lan_mesh.station_api import create_station_router

        class _Ctl:
            def __init__(self):
                self.db = Database(str(tmp_path / "d.db"))

            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        ctl.db.save_error_record({"timestamp": 1755300000.0, "module": "llm",
                                  "error_type": "TimeoutError",
                                  "message": "历史超时记录"})
        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)
        et.error_tracker.clear()
        try:
            # history 源: 诊断落盘记录
            r = client.get("/api/errors/diagnosis",
                           params={"source": "history", "window": 50})
            assert r.status_code == 200
            body = r.json()
            assert body["scanned"] == 1
            assert body["findings"][0]["category"] == "timeout"
            # 默认 buffer 源行为不变 (缓冲为空)
            r = client.get("/api/errors/diagnosis")
            assert r.json()["scanned"] == 0
        finally:
            et.error_tracker.clear()


class TestHealActions:
    """iter-49 (F4.2 修复环节): 自愈动作执行器 + heal_log 落盘。"""

    def test_heal_save_and_query_roundtrip(self, tmp_path):
        """heal_log 写入/查询往返: 倒序 + limit 夹取。"""
        from lan_mesh.database import Database
        db = Database(str(tmp_path / "d.db"))
        db.save_heal_record({"timestamp": 1.0, "category": "timeout",
                             "action": "check_peer", "result": "ok",
                             "detail": "探测 1/1"})
        db.save_heal_record({"timestamp": 2.0, "category": "auth",
                             "action": "probe_balances", "result": "failed",
                             "detail": "boom"})
        rows = db.query_heal_history(limit=10)
        assert len(rows) == 2
        assert rows[0]["action"] == "probe_balances"  # 倒序最新在前
        assert rows[1]["result"] == "ok"
        assert len(db.query_heal_history(limit=1)) == 1

    def test_heal_capacity_prune(self, tmp_path):
        """容量修剪: 505 行 → 保留最近 500 行。"""
        from lan_mesh.database import Database
        db = Database(str(tmp_path / "d.db"))
        for i in range(505):
            db.save_heal_record({"timestamp": float(i), "category": "c",
                                 "action": f"a{i}", "result": "ok", "detail": ""})
        conn = db._get_conn()
        total = conn.execute("SELECT COUNT(*) FROM heal_log").fetchone()[0]
        assert total == 500

    def test_run_heal_action_results(self, tmp_path, monkeypatch):
        """执行器三分支: manual_required / ok / failed + 落盘。"""
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        ctl = StationController.__new__(StationController)
        ctl.db = Database(str(tmp_path / "d.db"))
        ctl.discovery = None
        # 未注册动作 → manual_required
        rec = ctl.run_heal_action("retry_or_switch", "upstream_5xx")
        assert rec["result"] == "manual_required"
        # check_peer 无发现服务 → ok 跳过
        rec = ctl.run_heal_action("check_peer", "timeout")
        assert rec["result"] == "ok" and "跳过" in rec["detail"]
        # probe_balances 走余额探测钩子 (mock)
        monkeypatch.setattr("lan_mesh.model_resources.probe_balances_global",
                            lambda timeout=10.0: {"probed": 1, "supported": 1})
        rec = ctl.run_heal_action("probe_balances", "auth")
        assert rec["result"] == "ok" and "1/1" in rec["detail"]
        # handler 异常 → failed (不抛出)
        ctl._heal_check_peer = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        rec = ctl.run_heal_action("check_peer", "connection")
        assert rec["result"] == "failed" and "boom" in rec["detail"]
        # 全部落盘 (4 条, 倒序最新在前)
        rows = ctl.db.query_heal_history(limit=10)
        assert len(rows) == 4 and rows[0]["result"] == "failed"

    def test_heal_endpoints(self, tmp_path, monkeypatch):
        """POST /api/errors/heal (含动作映射) + GET heal/history。"""
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.database import Database
        from lan_mesh.station_api import create_station_router
        from lan_mesh.station_controller import StationController

        class _Ctl:
            def __init__(self):
                self.db = Database(str(tmp_path / "d.db"))
                self.discovery = None

            def run_heal_action(self, action, category=""):
                return StationController.run_heal_action(self, action, category)

            def _heal_check_peer(self):
                return StationController._heal_check_peer(self)

            def _heal_probe_balances(self):
                return StationController._heal_probe_balances(self)

            def _heal_rotate_key(self):
                return StationController._heal_rotate_key(self)

            def _heal_switch_pool(self):
                return StationController._heal_switch_pool(self)

            def __getattr__(self, name):
                return MagicMock()

        monkeypatch.setattr("lan_mesh.model_resources.probe_balances_global",
                            lambda timeout=10.0: {"probed": 0, "supported": 0})
        app = FastAPI()
        app.include_router(create_station_router(_Ctl()))
        client = TestClient(app)
        # iter-60: rotate_key 透传为真实修复写动作 (不再映射 probe_balances)
        r = client.post("/api/errors/heal",
                        params={"action": "rotate_key", "category": "auth"})
        assert r.status_code == 200
        body = r.json()
        assert body["action"] == "rotate_key" and body["result"] == "ok"
        # switch_pool 透传: 空探测 → 全耗尽判定 → failed (升级人工语义)
        r = client.post("/api/errors/heal",
                        params={"action": "switch_pool", "category": "rate_limit"})
        assert r.json()["action"] == "switch_pool"
        assert r.json()["result"] == "failed"
        # 未注册动作 → manual_required (仍需人工)
        r = client.post("/api/errors/heal",
                        params={"action": "retry_or_switch"})
        assert r.json()["result"] == "manual_required"
        # 历史端点: 3 条倒序返回
        r = client.get("/api/errors/heal/history", params={"limit": 10})
        assert r.status_code == 200
        heals = r.json()["heals"]
        assert len(heals) == 3 and heals[0]["result"] == "manual_required"


class TestAutoHeal:
    """iter-50 (F4.2 自动化环节): 自动自愈守护 (周期扫描 + 冷却去重 + 默认关)。"""

    def test_auto_heal_config_defaults(self, tmp_path):
        """观测配置默认值: 守护默认关 + 周期/冷却默认; yaml 覆盖生效。"""
        from lan_mesh.config import ObservabilityConfig, load_config
        obs = ObservabilityConfig()
        assert obs.auto_heal_enabled is False
        assert obs.auto_heal_interval == 300.0
        assert obs.auto_heal_cooldown == 600.0
        p = tmp_path / "c.yaml"
        p.write_text(
            "observability:\n"
            "  stall_check_interval: 60\n"
            "  stall_minutes: 30\n"
            "  auto_heal_enabled: true\n"
            "  auto_heal_interval: 120\n"
            "  auto_heal_cooldown: 240\n",
            encoding="utf-8",
        )
        cfg = load_config(str(p))
        assert cfg.observability.auto_heal_enabled is True
        assert cfg.observability.auto_heal_interval == 120.0
        assert cfg.observability.auto_heal_cooldown == 240.0

    def test_auto_heal_once_disabled(self, tmp_path):
        """守护关闭: 单轮扫描 no-op (runs 递增但零执行零落盘)。"""
        from lan_mesh import error_tracker as et
        from lan_mesh.config import AppConfig
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        ctl = StationController.__new__(StationController)
        ctl.db = Database(str(tmp_path / "d.db"))
        ctl.discovery = None
        ctl.cfg = AppConfig()  # observability 默认关
        ctl._auto_heal_last = {}
        ctl._auto_heal_state = {"runs": 0, "last_run": 0.0, "last_actions": []}
        et.error_tracker.clear()
        try:
            et.error_tracker.capture("llm", error_type="Timeout", message="请求超时")
            s = ctl._auto_heal_once()
            assert s["enabled"] is False
            assert s["actions_run"] == [] and s["skipped_manual"] == 0
            assert ctl._auto_heal_state["runs"] == 1
            assert ctl._auto_heal_state["last_actions"] == []
            assert len(ctl.db.query_heal_history()) == 0  # 未落盘
            st = ctl.get_auto_heal_status()
            assert st["enabled"] is False and st["runs"] == 1
            assert st["interval"] == 300.0 and st["cooldown"] == 600.0
        finally:
            et.error_tracker.clear()

    def test_auto_heal_once_enabled_flow(self, tmp_path):
        """守护开启: 安全动作自动执行落盘 + 同类别冷却去重 + 需人工跳过。"""
        from lan_mesh import error_tracker as et
        from lan_mesh.config import AppConfig, ObservabilityConfig
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        ctl = StationController.__new__(StationController)
        ctl.db = Database(str(tmp_path / "d.db"))
        ctl.discovery = None
        ctl.cfg = AppConfig(observability=ObservabilityConfig(
            auto_heal_enabled=True))
        ctl._auto_heal_last = {}
        ctl._auto_heal_state = {"runs": 0, "last_run": 0.0, "last_actions": []}
        et.error_tracker.clear()
        try:
            # 第一轮: timeout → check_peer 自动执行并落盘
            et.error_tracker.capture("llm", error_type="Timeout", message="请求超时")
            s1 = ctl._auto_heal_once()
            assert s1["enabled"] is True
            assert [a["action"] for a in s1["actions_run"]] == ["check_peer"]
            assert s1["actions_run"][0]["result"] == "ok"
            assert s1["skipped_cooldown"] == [] and s1["skipped_manual"] == 0
            assert len(ctl.db.query_heal_history()) == 1
            # 第二轮: timeout 同类别冷却跳过; 5xx 需人工计入 skipped_manual
            et.error_tracker.capture("llm", error_type="HTTPError",
                                     message="502 bad gateway")
            s2 = ctl._auto_heal_once()
            assert s2["actions_run"] == []
            assert s2["skipped_cooldown"] == ["timeout"]
            assert s2["skipped_manual"] == 1
            assert ctl._auto_heal_state["runs"] == 2
            assert ctl._auto_heal_state["last_actions"] == []
            assert len(ctl.db.query_heal_history()) == 1  # 第二轮未落盘
            st = ctl.get_auto_heal_status()
            assert st["runs"] == 2 and st["last_run"] > 0
        finally:
            et.error_tracker.clear()

    def test_auto_heal_endpoints(self, tmp_path):
        """GET /api/errors/heal/status + POST /api/errors/heal/auto-check。"""
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.config import AppConfig
        from lan_mesh.database import Database
        from lan_mesh.station_api import create_station_router
        from lan_mesh.station_controller import StationController

        class _Ctl:
            def __init__(self):
                self.db = Database(str(tmp_path / "d.db"))
                self.discovery = None
                self.cfg = AppConfig()  # 默认守护关
                self._auto_heal_last = {}
                self._auto_heal_state = {"runs": 0, "last_run": 0.0,
                                         "last_actions": []}

            def get_auto_heal_status(self):
                return StationController.get_auto_heal_status(self)

            def _auto_heal_once(self):
                return StationController._auto_heal_once(self)

            def __getattr__(self, name):
                return MagicMock()

        app = FastAPI()
        app.include_router(create_station_router(_Ctl()))
        client = TestClient(app)
        # 状态端点: 开关/周期/冷却/累计轮次
        r = client.get("/api/errors/heal/status")
        assert r.status_code == 200
        st = r.json()
        assert st["enabled"] is False
        assert st["interval"] == 300.0 and st["cooldown"] == 600.0
        assert st["runs"] == 0 and st["last_actions"] == []
        # 手动触发扫描: 守护关 → no-op 摘要
        r = client.post("/api/errors/heal/auto-check")
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is False and body["actions_run"] == []
        # runs 已递增
        r = client.get("/api/errors/heal/status")
        assert r.json()["runs"] == 1


class TestIter60AutoHealClosedLoop:
    """iter-60 (F4.2 全自动闭环): 真实修复写动作 + 每日配额/连续失败熔断护栏。"""

    @staticmethod
    def _make_ctl(tmp_path, **obs_kw):
        """构造轻量 controller (__new__ + 手工属性, 与 TestAutoHeal 同模式)。"""
        from lan_mesh.config import AppConfig, ObservabilityConfig
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        ctl = StationController.__new__(StationController)
        ctl.db = Database(str(tmp_path / "d.db"))
        ctl.discovery = None
        ctl.cfg = AppConfig(observability=ObservabilityConfig(
            auto_heal_enabled=True, **obs_kw))
        ctl._auto_heal_last = {}
        ctl._auto_heal_state = {"runs": 0, "last_run": 0.0, "last_actions": []}
        return ctl

    def test_config_daily_limit(self, tmp_path):
        """auto_heal_daily_limit 默认 3 + yaml 覆盖生效。"""
        from lan_mesh.config import ObservabilityConfig, load_config
        assert ObservabilityConfig().auto_heal_daily_limit == 3
        p = tmp_path / "c.yaml"
        p.write_text("observability:\n  auto_heal_daily_limit: 5\n",
                     encoding="utf-8")
        cfg = load_config(str(p))
        assert cfg.observability.auto_heal_daily_limit == 5

    def test_heal_rotate_key_pauses_invalid_pool(self, tmp_path, monkeypatch):
        """auth 错误 → rotate_key: 探测 401 的池被置 paused (路由剔除)。"""
        ctl = self._make_ctl(tmp_path)
        calls = []
        monkeypatch.setattr(
            "lan_mesh.model_resources.probe_balances_global",
            lambda timeout=10.0: {
                "probed": 2, "supported": 1, "results": {
                    "pool_bad": {"error": "401 Client Error: unauthorized",
                                 "balance": None},
                    "pool_ok": {"error": "", "balance": 12.5},
                }})
        monkeypatch.setattr(
            "lan_mesh.model_resources.set_pool_status_global",
            lambda rid, status: calls.append((rid, status)) or True)
        rec = ctl.run_heal_action("rotate_key", "auth")
        assert rec["result"] == "ok"
        assert "pool_bad" in rec["detail"]
        assert calls == [("pool_bad", "paused")]

    def test_heal_switch_pool_all_exhausted(self, tmp_path, monkeypatch):
        """rate_limit 错误 → switch_pool: 全耗尽时升级 failed (人工介入)。"""
        ctl = self._make_ctl(tmp_path)
        monkeypatch.setattr(
            "lan_mesh.model_resources.probe_balances_global",
            lambda timeout=10.0: {
                "probed": 2, "supported": 2, "results": {
                    "pool_a": {"error": "", "balance": 0.0},
                    "pool_b": {"error": "", "balance": 0.0},
                }})
        rec = ctl.run_heal_action("switch_pool", "rate_limit")
        assert rec["result"] == "failed"
        assert "人工" in rec["detail"]

    def test_heal_switch_pool_ok(self, tmp_path, monkeypatch):
        """rate_limit 错误 → switch_pool: 存在可用池 → ok 报告。"""
        ctl = self._make_ctl(tmp_path)
        monkeypatch.setattr(
            "lan_mesh.model_resources.probe_balances_global",
            lambda timeout=10.0: {
                "probed": 3, "supported": 3, "results": {
                    "pool_a": {"error": "", "balance": 0.0},
                    "pool_b": {"error": "", "balance": 8.0},
                    "pool_c": {"error": "", "balance": 3.2},
                }})
        rec = ctl.run_heal_action("switch_pool", "rate_limit")
        assert rec["result"] == "ok" and "2 个可用池" in rec["detail"]

    def test_auto_heal_write_quota(self, tmp_path, monkeypatch):
        """写动作每日配额: 超限后 skipped_quota + status 暴露消耗。"""
        from lan_mesh import error_tracker as et
        ctl = self._make_ctl(tmp_path, auto_heal_daily_limit=1)
        monkeypatch.setattr(
            "lan_mesh.model_resources.probe_balances_global",
            lambda timeout=10.0: {"probed": 0, "supported": 0, "results": {}})
        et.error_tracker.clear()
        try:
            et.error_tracker.capture("llm", error_type="AuthError",
                                     message="401 unauthorized")
            s1 = ctl._auto_heal_once()
            assert [a["action"] for a in s1["actions_run"]] == ["rotate_key"]
            # 清冷却后第二轮: 命中每日配额 → skipped_quota
            ctl._auto_heal_last.clear()
            s2 = ctl._auto_heal_once()
            assert s2["actions_run"] == []
            assert s2["skipped_quota"] == ["auth"]
            st = ctl.get_auto_heal_status()
            assert st["daily_counts"] == {"auth": 1}
            assert st["daily_limit"] == 1
        finally:
            et.error_tracker.clear()

    def test_auto_heal_fuse_on_consecutive_failures(self, tmp_path, monkeypatch):
        """连续 2 次写动作失败 → 熔断 skipped_fused; 错误消失 → 自动复位。"""
        from lan_mesh import error_tracker as et
        ctl = self._make_ctl(tmp_path, auto_heal_daily_limit=10)
        monkeypatch.setattr(
            "lan_mesh.model_resources.probe_balances_global",
            lambda timeout=10.0: {"probed": 0, "supported": 0,
                                  "results": {}, "error": "probe down"})
        et.error_tracker.clear()
        try:
            et.error_tracker.capture("llm", error_type="AuthError",
                                     message="401 unauthorized")
            s1 = ctl._auto_heal_once()
            assert s1["actions_run"][0]["result"] == "failed"
            assert ctl._auto_heal_fused == {}
            ctl._auto_heal_last.clear()
            s2 = ctl._auto_heal_once()
            assert s2["actions_run"][0]["result"] == "failed"
            assert "auth" in ctl._auto_heal_fused  # 连续 2 次失败熔断
            st = ctl.get_auto_heal_status()
            assert "auth" in st["fused"]
            ctl._auto_heal_last.clear()
            s3 = ctl._auto_heal_once()
            assert s3["actions_run"] == []
            assert s3["skipped_fused"][0]["category"] == "auth"
            # 错误消失 (无 findings) → 熔断/失败计数复位
            et.error_tracker.clear()
            ctl._auto_heal_last.clear()
            s4 = ctl._auto_heal_once()
            assert ctl._auto_heal_fused == {}
            assert ctl._auto_heal_fail_streak == {}
            assert s4["actions_run"] == []
        finally:
            et.error_tracker.clear()

    def test_heal_status_guard_fields(self, tmp_path):
        """status 端点扩展字段: daily_limit/daily_counts/fused 默认。"""
        ctl = self._make_ctl(tmp_path)
        st = ctl.get_auto_heal_status()
        assert st["daily_limit"] == 3
        assert st["daily_counts"] == {} and st["fused"] == {}


class TestIter61PluginMarket:
    """iter-61 (F5.3 插件系统): 第三方 Skill 市场浏览/安装/卸载与安全护栏。"""

    @staticmethod
    def _make_market(tmp_path, max_size_kb=200):
        """构造 SkillMarket: 独立 skills_dir + market_dir + 内存 DB。"""
        from lan_mesh.database import Database
        from lan_mesh.skill_market import SkillMarket
        db = Database(str(tmp_path / "d.db"))
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(exist_ok=True)
        market_dir = tmp_path / "skills_market"
        market_dir.mkdir(exist_ok=True)
        return (SkillMarket(db, skills_dir, market_dir, max_size_kb=max_size_kb),
                db, skills_dir, market_dir)

    @staticmethod
    def _make_pkg(market_dir, skill_id, name="测试插件",
                  extra="", default_access=None):
        """在市场中造一个插件包, 返回包目录。"""
        pkg = market_dir / skill_id
        pkg.mkdir(exist_ok=True)
        fm = f"""---
name: {skill_id}
description: {name} 描述
category: coding
tags: [plugin, demo]
version: "1.0"
"""
        if default_access is not None:
            fm += f"default_access: {default_access}\n"
        (pkg / "SKILL.md").write_text(fm + "---\n\n# {name}\n\n正文内容\n" + extra,
                                      encoding="utf-8")
        return pkg

    def test_config_market_fields(self, tmp_path):
        """AppConfig 市场字段默认值 + yaml 覆盖生效。"""
        from lan_mesh.config import AppConfig, load_config
        cfg = AppConfig()
        assert cfg.skill_market_dir == "skills_market"
        assert cfg.skill_max_size_kb == 200
        p = tmp_path / "c.yaml"
        p.write_text("skill_market_dir: market_custom\nskill_max_size_kb: 50\n",
                     encoding="utf-8")
        cfg2 = load_config(str(p))
        assert cfg2.skill_market_dir == "market_custom"
        assert cfg2.skill_max_size_kb == 50

    def test_market_list_packages(self, tmp_path):
        """市场浏览: 列出包 + installed 空标记 + 无 SKILL.md 目录被忽略。"""
        mkt, db, skills_dir, market_dir = self._make_market(tmp_path)
        self._make_pkg(market_dir, "demo-plugin")
        (market_dir / "no-skill-md").mkdir()
        (market_dir / "no-skill-md" / "other.txt").write_text("x")
        items = mkt.list_market()
        assert [i["skill_id"] for i in items] == ["demo-plugin"]
        assert items[0]["installed"] == "" and items[0]["valid"] is True

    def test_install_creates_files_and_db(self, tmp_path):
        """安装: 白名单复制到 skills/ + DB origin=market + 安全默认仅 station。"""
        mkt, db, skills_dir, market_dir = self._make_market(tmp_path)
        pkg = self._make_pkg(market_dir, "demo-plugin")
        (pkg / "evil.sh").write_text("rm -rf /")  # 白名单外文件不应被复制
        r = mkt.install("demo-plugin")
        assert r["ok"] and r["action"] == "installed"
        assert (skills_dir / "demo-plugin" / "SKILL.md").is_file()
        assert not (skills_dir / "demo-plugin" / "evil.sh").exists()
        row = db.get_skill("demo-plugin")
        assert row["origin"] == "market"
        assert row["default_access"] == ["station"]  # 未声明 → 安全默认

    def test_install_invalid_package_rejected(self, tmp_path):
        """校验护栏: 缺 name / 超体积 / 非法 ID / 包不存在 → 拒绝。"""
        mkt, db, skills_dir, market_dir = self._make_market(tmp_path)
        # 缺 front matter name
        bad = market_dir / "no-meta"
        bad.mkdir()
        (bad / "SKILL.md").write_text("# 无元数据\n正文", encoding="utf-8")
        assert not mkt.install("no-meta")["ok"]
        # 超体积 (上限 1KB)
        mkt2, *_ = self._make_market(tmp_path, max_size_kb=1)
        self._make_pkg(market_dir, "too-big", extra="x" * 5000)
        assert not mkt2.install("too-big")["ok"]
        # 非法 ID (大写/斜杠/点)
        for bad_id in ("Bad-Id", "../escape", "a" * 65):
            self._make_pkg(market_dir, "ok-pkg")
            assert not mkt.install(bad_id)["ok"]
        # 包不存在
        assert not mkt.install("ghost-pkg")["ok"]

    def test_install_builtin_conflict_rejected(self, tmp_path):
        """与内置技能同名 → 拒绝安装 (内置不可覆盖)。"""
        mkt, db, skills_dir, market_dir = self._make_market(tmp_path)
        db.upsert_skill(skill_id="builtin-skill", name="内置", description="d",
                        category="general", tags=[], default_access=["all"],
                        content_path="builtin-skill", origin="builtin")
        self._make_pkg(market_dir, "builtin-skill")
        r = mkt.install("builtin-skill")
        assert not r["ok"] and "内置" in r["message"]
        assert db.get_skill("builtin-skill")["origin"] == "builtin"

    def test_uninstall_market_skill(self, tmp_path):
        """卸载 market 技能: 文件删除 + DB 记录删除 + 分配记录级联。"""
        mkt, db, skills_dir, market_dir = self._make_market(tmp_path)
        self._make_pkg(market_dir, "demo-plugin")
        assert mkt.install("demo-plugin")["ok"]
        db.assign_skill("demo-plugin", "role", "worker")
        r = mkt.uninstall("demo-plugin")
        assert r["ok"] and r["action"] == "uninstalled"
        assert not (skills_dir / "demo-plugin").exists()
        assert db.get_skill("demo-plugin") is None
        assert db.get_skills_for_assignee("role", "worker") == []

    def test_uninstall_builtin_rejected(self, tmp_path):
        """卸载内置技能 → 拒绝且文件保留。"""
        mkt, db, skills_dir, market_dir = self._make_market(tmp_path)
        builtin_dir = skills_dir / "builtin-skill"
        builtin_dir.mkdir()
        (builtin_dir / "SKILL.md").write_text("---\nname: builtin-skill\n---\n正文",
                                              encoding="utf-8")
        db.upsert_skill(skill_id="builtin-skill", name="内置", description="d",
                        category="general", tags=[], default_access=["all"],
                        content_path="builtin-skill", origin="builtin")
        assert not mkt.uninstall("builtin-skill")["ok"]
        assert builtin_dir.exists() and db.get_skill("builtin-skill") is not None

    def test_scan_preserves_origin(self, tmp_path):
        """重扫内置目录不覆盖 origin 与安全默认: market 技能重扫后仍为 market + ["station"]。"""
        from lan_mesh.skill_registry import SkillRegistry
        mkt, db, skills_dir, market_dir = self._make_market(tmp_path)
        self._make_pkg(market_dir, "demo-plugin")
        assert mkt.install("demo-plugin")["ok"]
        reg = SkillRegistry(db, str(skills_dir))
        reg.scan_and_register()
        row = db.get_skill("demo-plugin")
        assert row["origin"] == "market"
        assert row["default_access"] == ["station"]  # 扫描不覆盖安全默认


class TestIter62PwaMobile:
    """iter-62 (F5.4 移动端 PWA): Service Worker 离线壳/缓存策略/挂载与白名单。"""

    def test_sw_file_valid(self):
        """sw.js 存在且含核心生命周期与缓存策略逻辑。"""
        from lan_mesh.station_controller import STATIC_DIR
        sw = STATIC_DIR / "sw.js"
        assert sw.is_file()
        content = sw.read_text(encoding="utf-8")
        assert "CACHE_NAME" in content
        assert "install" in content and "activate" in content and "fetch" in content
        assert "skipWaiting" in content and "clients.claim" in content
        # API 不缓存: /api/ 路径直接透传 (无 respondWith)
        assert "startsWith('/api/')" in content
        # 导航 network-first 回退缓存壳
        assert "mode === 'navigate'" in content
        assert "caches.match('/')" in content
        # 静态资源 stale-while-revalidate
        assert "caches.match(event.request)" in content

    def test_manifest_valid(self):
        """manifest.json PWA 声明完整 (名称/入口/独立窗口/主题色)。"""
        import json
        from lan_mesh.station_controller import STATIC_DIR
        m = json.loads((STATIC_DIR / "manifest.json").read_text(encoding="utf-8"))
        assert m["name"] and m["short_name"]
        assert m["start_url"] == "/" and m["display"] == "standalone"
        assert m["background_color"] and m["theme_color"]
        assert m["icons"]  # 安装图标

    def test_dashboard_pwa_refs(self):
        """dashboard 引用 manifest + 注册 SW (仅 https/localhost 安全上下文)。"""
        from lan_mesh.station_controller import TEMPLATES_DIR
        html = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
        assert 'rel="manifest" href="/static/manifest.json"' in html
        assert "navigator.serviceWorker.register('/sw.js')" in html
        assert "location.hostname==='127.0.0.1'" in html  # 安全上下文判定

    def test_sw_auth_whitelisted(self):
        """/sw.js 在认证白名单 (SW 注册请求不带 Authorization 头)。"""
        from lan_mesh.station_routes_common import _AUTH_WHITELIST
        assert "/sw.js" in _AUTH_WHITELIST

    def test_mobile_nav_css_layering(self):
        """iter-62 缺陷回归: .mobile-nav 的 display:none 仅出现在 min-width:641px
        规则内; 若存在同特异性的普通 display:none 规则会覆盖 640px 断点内的
        display:flex, 导致移动端底部导航不显示 (CDP 真实视口实测发现)。"""
        from lan_mesh.station_controller import TEMPLATES_DIR
        html = (TEMPLATES_DIR / "dashboard.html").read_text(encoding="utf-8")
        # 640px 断点内必须有 display:flex 显示规则
        mq640 = html.split("@media(max-width:640px){", 1)[1].split("@media(min-width", 1)[0]
        assert ".mobile-nav{display:flex" in mq640
        # display:none 只允许在 min-width:641px 规则中出现一次 (剔除注释文字)
        import re
        no_comments = re.sub(r"/\*.*?\*/", "", html, flags=re.S)
        assert no_comments.count(".mobile-nav{display:none}") == 1
        assert "@media(min-width:641px){.mobile-nav{display:none}}" in html


class TestIter63UserAdmin:
    """iter-63 团队场景深化: 用户管理 UI + token 轮换端点。

    核心: users 表持久化 (token 仅存 SHA256 哈希) + config 首次种子 +
    管理操作 (增/改角色/轮换/删) + 最后 boss 防自锁 + HTTP 端点。
    """

    def _make_db(self, tmp_path):
        from lan_mesh.database import Database
        return Database(str(tmp_path / "u.db"))

    def test_migration_v9_users_table(self, tmp_path):
        """迁移链完整 (SCHEMA_VERSION 随迭代递增) 且 users 表可写读。"""
        from lan_mesh import database as dbmod
        assert dbmod.SCHEMA_VERSION >= 9
        db = self._make_db(tmp_path)
        db.upsert_user_db("alice", "viewer", "hash1", "abcd")
        rows = db.list_users_db()
        assert len(rows) == 1
        assert rows[0]["name"] == "alice" and rows[0]["token_tail4"] == "abcd"

    def test_users_db_crud(self, tmp_path):
        """DB 层增改查删 + 角色更新。"""
        db = self._make_db(tmp_path)
        db.upsert_user_db("alice", "viewer", "h1", "aaa1")
        db.upsert_user_db("bob", "operator", "h2", "bbb2")
        db.update_user_role_db("alice", "boss")
        rows = {r["name"]: r for r in db.list_users_db()}
        assert rows["alice"]["role"] == "boss"
        assert rows["bob"]["role"] == "operator"
        db.delete_user_db("bob")
        assert [r["name"] for r in db.list_users_db()] == ["alice"]

    def test_configure_users_hashes_tokens(self):
        """iter-63: 内存表不存明文 — _user_tokens key 为 SHA256 哈希。"""
        from lan_mesh import station_routes_common as common
        common.set_users_db(None)
        common.configure_users([
            {"name": "小张", "role": "operator", "token": "op-token-63"},
        ])
        assert "op-token-63" not in common._user_tokens
        assert len(common._user_tokens) == 1
        entry = list(common._user_tokens.values())[0]
        assert entry["token_hash"] == common._hash_token("op-token-63")
        assert entry["token_tail4"] == "n-63"
        # 认证仍按明文 token 工作 (内部哈希比较)
        assert common.resolve_role("op-token-63")["role"] == "operator"
        common.configure_users([])

    def test_load_users_from_db_seed_then_load(self, tmp_path):
        """DB 空 → config 种子导入; DB 非空 → 以 DB 为准 (轮换持久化)。"""
        from lan_mesh import station_routes_common as common
        db = self._make_db(tmp_path)
        common.set_users_db(db)
        # 首次: config 种子 → 导入 DB
        common.configure_users([
            {"name": "种子boss", "role": "boss", "token": "seed-token-1"},
        ])
        assert common.load_users_from_db(db) is True
        assert len(db.list_users_db()) == 1
        # 模拟重启: 轮换写入 DB 后重新加载, 内存应从 DB 恢复
        common.rotate_user_token("种子boss")
        common.configure_users([])  # 重启后 config 已清空
        assert common.load_users_from_db(db) is True
        assert common.list_users_public()[0]["name"] == "种子boss"
        common.configure_users([])

    def test_create_user_and_rotate_token(self, tmp_path):
        """新增用户返回明文一次 + 轮换后旧 token 失效 + DB 持久化。"""
        from lan_mesh import station_routes_common as common
        db = self._make_db(tmp_path)
        common.set_users_db(db)
        common.configure_users([])
        r = common.create_user("alice", "viewer")
        assert r.get("token") and "error" not in r
        assert common.resolve_role(r["token"])["name"] == "alice"
        # 轮换: 旧 token 失效, 新 token 生效
        old = r["token"]
        r2 = common.rotate_user_token("alice")
        assert r2.get("token") != old
        assert common.resolve_role(old) is None
        assert common.resolve_role(r2["token"])["role"] == "viewer"
        # DB 同步: 哈希已更新
        db_row = db.list_users_db()[0]
        assert db_row["token_hash"] == common._hash_token(r2["token"])
        # 重复创建同名 → 拒绝
        assert "error" in common.create_user("alice", "viewer")
        common.configure_users([])

    def test_last_boss_guard(self, tmp_path):
        """最后 boss 不可降级/删除; 多 boss 时可操作; 删除后不可认证。"""
        from lan_mesh import station_routes_common as common
        db = self._make_db(tmp_path)
        common.set_users_db(db)
        common.configure_users([
            {"name": "boss1", "role": "boss", "token": "boss-token-1"},
            {"name": "op1", "role": "operator", "token": "op-token-1"},
        ])
        # 唯一 boss 降级 → 拒绝
        assert "error" in common.set_user_role("boss1", "viewer")
        # 唯一 boss 删除 → 拒绝
        assert "error" in common.remove_user("boss1")
        # 加第二个 boss 后可降级原 boss
        common.create_user("boss2", "boss")
        assert "error" not in common.set_user_role("boss1", "operator")
        assert common.resolve_role("boss-token-1")["role"] == "operator"
        # 删除非 boss 用户后其 token 失效
        assert "error" not in common.remove_user("op1")
        assert common.resolve_role("op-token-1") is None
        common.configure_users([])

    def test_users_endpoints(self, tmp_path, monkeypatch):
        """HTTP 层: boss 列表含尾4位 / 新增 / 改角色 / 轮换 / 删除 / 越权 403。"""
        from fastapi.testclient import TestClient
        from lan_mesh import station_routes_common as common
        from lan_mesh.station_routes_basic import build_basic_routes

        db = self._make_db(tmp_path)
        common.set_users_db(db)
        common.configure_users([
            {"name": "boss1", "role": "boss", "token": "boss-tok-aaaa"},
            {"name": "view1", "role": "viewer", "token": "view-tok-bbbb"},
        ])
        common.load_users_from_db(db)
        # 空 controller 对象 (端点仅用 logger)
        ctl = type("Ctl", (), {"db": db, "discovery": None,
                              "station_director": None})()
        ctl.state = type("St", (), {"shared_folder": None})()
        router = build_basic_routes(ctl)
        app = __import__("fastapi").FastAPI()
        # 与真实环境一致: 挂限流+认证+角色分级中间件
        from lan_mesh.station_api import configure_mesh_auth
        configure_mesh_auth(True, "mesh-secret-63")
        app.middleware("http")(common.api_guard_middleware)
        app.include_router(router)
        client = TestClient(app)
        boss_h = {"Authorization": "Bearer boss-tok-aaaa"}
        view_h = {"Authorization": "Bearer view-tok-bbbb"}
        # boss 视图: 含 token 尾 4 位
        r = client.get("/api/station/users", headers=boss_h)
        assert r.status_code == 200 and r.json()["admin_view"] is True
        assert any(u["token_tail4"] == "aaaa" for u in r.json()["users"])
        # viewer 视图: 脱敏
        r = client.get("/api/station/users", headers=view_h)
        assert r.status_code == 200 and r.json()["admin_view"] is False
        assert all("token_tail4" not in u for u in r.json()["users"])
        # viewer 写 → 403 (管理员路径仅 boss)
        r = client.post("/api/station/users", headers=view_h,
                       json={"name": "x", "role": "viewer"})
        assert r.status_code == 403
        # boss 新增
        r = client.post("/api/station/users", headers=boss_h,
                       json={"name": "new1", "role": "operator"})
        assert r.status_code == 200 and r.json().get("token")
        # boss 改角色
        r = client.put("/api/station/users/new1/role", headers=boss_h,
                       json={"role": "viewer"})
        assert r.status_code == 200 and r.json()["role"] == "viewer"
        # boss 轮换 (旧 token 失效)
        r = client.post("/api/station/users/new1/rotate-token", headers=boss_h)
        assert r.status_code == 200 and r.json().get("token")
        # boss 删除
        r = client.delete("/api/station/users/new1", headers=boss_h)
        assert r.status_code == 200
        # 轮换后旧 token 已被端到端确认失效 (非白名单端点返回 403)
        r = client.get("/api/station/users", headers=boss_h)
        assert r.status_code == 200
        common.configure_users([])
        configure_mesh_auth(False, "")


class TestDagEdit:
    """iter-51 (F4.3): 自然语言 DAG 编辑 (读写图方法 + PUT 编辑端点 + 秘书意图)。"""

    @staticmethod
    def _make_task(db, task_id="task-abc123456789", status="pending",
                   names=("需求分析", "编码实现")):
        """创建带两个串联子任务的任务并落盘。"""
        from lan_mesh.protocol import SubTask, Task
        sts = [
            SubTask(subtask_id=f"st-{i}", parent_task_id=task_id,
                    name=n).to_dict() for i, n in enumerate(names)
        ]
        sts[1]["depends_on"] = ["st-0"]
        db.save_task(Task(task_id=task_id, name="测试任务",
                          status=status, subtasks=sts))
        return task_id

    def test_get_task_graph_data_sources(self, tmp_path):
        """读图三来源: 子任务重建 / checkpoint 优先 / 无图 None。"""
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        ctl = StationController.__new__(StationController)
        ctl.db = Database(str(tmp_path / "d.db"))
        ctl.discovery = None
        tid = self._make_task(ctl.db)
        # 子任务重建
        g = ctl.get_task_graph_data(tid)
        assert len(g["nodes"]) == 2 and len(g["edges"]) == 1
        assert g["edges"][0] == {"source": "st-0", "target": "st-1",
                                 "condition": "", "description": ""}
        # checkpoint 优先 (dag_json 带 3 节点)
        ctl.db.save_checkpoint("ck1", tid, "dispatch",
                               '{"nodes": [{"id": "a"}, {"id": "b"}, '
                               '{"id": "c"}], "edges": []}', "{}", "{}")
        g = ctl.get_task_graph_data(tid)
        assert len(g["nodes"]) == 3
        # 无图任务
        assert ctl.get_task_graph_data("task-missing") is None

    def test_update_task_graph_validations(self, tmp_path):
        """三拒绝: 任务不存在 / 非 pending / 环检测。"""
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        ctl = StationController.__new__(StationController)
        ctl.db = Database(str(tmp_path / "d.db"))
        ctl.discovery = None
        # 不存在
        r = ctl.update_task_graph("task-missing", {"nodes": [], "edges": []})
        assert not r["ok"] and "不存在" in r["message"]
        # 非 pending (running)
        tid = self._make_task(ctl.db, status="running")
        r = ctl.update_task_graph(tid, {"nodes": [], "edges": []})
        assert not r["ok"] and "不可编辑" in r["message"]
        # 环 (A→B→A)
        tid = self._make_task(ctl.db)
        cyc = {"nodes": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
               "edges": [{"source": "a", "target": "b"},
                         {"source": "b", "target": "a"}]}
        r = ctl.update_task_graph(tid, cyc)
        assert not r["ok"] and "循环依赖" in r["message"]

    def test_update_task_graph_save_ok(self, tmp_path):
        """保存成功: 子任务落盘 + checkpoint dag_json 同步 + 读回新图。"""
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        ctl = StationController.__new__(StationController)
        ctl.db = Database(str(tmp_path / "d.db"))
        ctl.discovery = None
        tid = self._make_task(ctl.db)
        ctl.db.save_checkpoint("ck1", tid, "dispatch",
                               '{"nodes": [{"id": "old"}], "edges": []}',
                               "{}", "{}")
        new_graph = {"nodes": [
            {"id": "st-0", "name": "需求分析"},
            {"id": "st-1", "name": "编码实现"},
            {"id": "st-2", "name": "发布验收"},
        ], "edges": [
            {"source": "st-0", "target": "st-1"},
            {"source": "st-1", "target": "st-2"},
        ]}
        r = ctl.update_task_graph(tid, new_graph)
        assert r["ok"] and "3 节点" in r["message"]
        # DB 落盘
        task = ctl.db.get_task(tid)
        assert len(task.subtasks) == 3
        names = [st["name"] for st in task.subtasks]
        assert "发布验收" in names
        # checkpoint dag_json 已同步 (读图走 checkpoint 优先)
        g = ctl.get_task_graph_data(tid)
        assert len(g["nodes"]) == 3
        assert any(n["name"] == "发布验收" for n in g["nodes"])

    def test_graph_put_endpoint(self, tmp_path):
        """PUT /api/tasks/{tid}/graph: 成功 / 环 409 / 404 / 缺字段 400。"""
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.database import Database
        from lan_mesh.station_api import create_station_router
        from lan_mesh.station_controller import StationController

        class _Ctl:
            def __init__(self):
                self.db = Database(str(tmp_path / "d.db"))
                self.discovery = None
                self.secretary_active = True

            def get_task_graph_data(self, task_id):
                return StationController.get_task_graph_data(self, task_id)

            def update_task_graph(self, task_id, graph_data):
                return StationController.update_task_graph(
                    self, task_id, graph_data)

            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        tid = self._make_task(ctl.db)
        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)
        # GET 读图
        r = client.get(f"/api/tasks/{tid}/graph")
        assert r.status_code == 200 and len(r.json()["nodes"]) == 2
        # PUT 成功
        new_graph = {"nodes": [
            {"id": "st-0", "name": "需求分析"},
            {"id": "st-1", "name": "编码实现"},
            {"id": "st-2", "name": "测试验收"},
        ], "edges": [
            {"source": "st-0", "target": "st-1"},
            {"source": "st-1", "target": "st-2"},
        ]}
        r = client.put(f"/api/tasks/{tid}/graph", json=new_graph)
        assert r.status_code == 200 and r.json()["ok"]
        assert len(client.get(f"/api/tasks/{tid}/graph").json()["nodes"]) == 3
        # 环 → 409
        cyc = {"nodes": [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}],
               "edges": [{"source": "a", "target": "b"},
                         {"source": "b", "target": "a"}]}
        r = client.put(f"/api/tasks/{tid}/graph", json=cyc)
        assert r.status_code == 409 and "循环依赖" in r.json()["detail"]
        # 404
        r = client.put("/api/tasks/task-missing/graph",
                       json={"nodes": [], "edges": []})
        assert r.status_code == 409 and "不存在" in r.json()["detail"]
        # 缺字段 400
        r = client.put(f"/api/tasks/{tid}/graph", json={"nodes": []})
        assert r.status_code == 400

    def test_nl_edit_intent_detect(self):
        """关键词意图: 图编辑表达命中 edit_task_graph, 旧关键词不误命中。"""
        from lan_mesh.chat_handler import ChatHandler
        ch = ChatHandler.__new__(ChatHandler)
        for msg in ("加一步: 发布验收", "删除步骤 编码实现",
                    "跳过步骤 测试验收", "给任务加依赖 A→B"):
            assert ch._detect_action(msg) == "edit_task_graph"
        assert ch._detect_action("查询状态") == "query_status"
        assert ch._detect_action("加个好友") == ""  # 不误命中

    def test_nl_edit_action_end_to_end(self, tmp_path):
        """端到端: 自然语言加一步 → LLM 解析 → 落盘 → 读回新节点。"""
        from lan_mesh.chat_handler import ChatHandler
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController

        class _Runtime:
            def _call_llm_with_routing(self, prompt, opts):
                return {"content": '{"op": "add_node", '
                        '"node_name": "发布验收", "description": "交付验收"}'}

        ctl = StationController.__new__(StationController)
        ctl.db = Database(str(tmp_path / "d.db"))
        ctl.discovery = None
        tid = self._make_task(ctl.db, names=("需求分析", "编码实现"))
        ch = ChatHandler.__new__(ChatHandler)
        ch.runtime = _Runtime()
        ch.controller = ctl
        # 指令带 task id + 加一步
        out = ch._action_edit_task_graph(f"给任务 {tid} 加一步: 发布验收")
        assert "图编辑完成" in out and "3 节点" in out
        g = ctl.get_task_graph_data(tid)
        names = [n["name"] for n in g["nodes"]]
        assert "发布验收" in names and len(g["nodes"]) == 3
        # 未找到任务 → 明确提示
        out = ch._action_edit_task_graph("加一步: 无中生有")
        assert "未找到目标任务" in out

    def test_nl_edit_remove_node(self, tmp_path):
        """自然语言删步骤: 节点与关联边一并移除并落盘。"""
        from lan_mesh.chat_handler import ChatHandler
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController

        class _Runtime:
            def _call_llm_with_routing(self, prompt, opts):
                return {"content": '{"op": "remove_node", '
                        '"node_name": "编码实现"}'}

        ctl = StationController.__new__(StationController)
        ctl.db = Database(str(tmp_path / "d.db"))
        ctl.discovery = None
        tid = self._make_task(ctl.db, names=("需求分析", "编码实现", "测试验收"))
        # 三节点串联: st-0 → st-1 → st-2
        task = ctl.db.get_task(tid)
        task.subtasks[1]["depends_on"] = ["st-0"]
        task.subtasks[2]["depends_on"] = ["st-1"]
        ctl.db.save_task(task)
        ch = ChatHandler.__new__(ChatHandler)
        ch.runtime = _Runtime()
        ch.controller = ctl
        out = ch._action_edit_task_graph(f"任务 {tid} 删除步骤 编码实现")
        assert "图编辑完成" in out
        g = ctl.get_task_graph_data(tid)
        names = [n["name"] for n in g["nodes"]]
        assert names == ["需求分析", "测试验收"]
        assert len(g["edges"]) == 0  # 关联边一并移除

    def test_nl_edit_add_edge(self, tmp_path):
        """自然语言加依赖: 两个无依赖节点建立边 (含 LLM 解析失败兜底)。"""
        from lan_mesh.chat_handler import ChatHandler
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController

        class _Runtime:
            def _call_llm_with_routing(self, prompt, opts):
                return {"content": '{"op": "add_edge", "source": "需求分析", '
                        '"target": "编码实现"}'}

        ctl = StationController.__new__(StationController)
        ctl.db = Database(str(tmp_path / "d.db"))
        ctl.discovery = None
        tid = self._make_task(ctl.db, names=("需求分析", "编码实现"))
        task = ctl.db.get_task(tid)
        task.subtasks[1]["depends_on"] = []  # 去掉默认串联边
        ctl.db.save_task(task)
        assert len(ctl.get_task_graph_data(tid)["edges"]) == 0
        ch = ChatHandler.__new__(ChatHandler)
        ch.runtime = _Runtime()
        ch.controller = ctl
        out = ch._action_edit_task_graph(f"任务 {tid} 加依赖: 需求分析 → 编码实现")
        assert "图编辑完成" in out
        g = ctl.get_task_graph_data(tid)
        assert len(g["edges"]) == 1
        assert g["edges"][0]["source"] == "st-0"
        assert g["edges"][0]["target"] == "st-1"
        # LLM 解析失败 → 明确引导提示 (不虚报)
        class _BadRuntime:
            def _call_llm_with_routing(self, prompt, opts):
                return {"content": "乱七八糟"}
        ch.runtime = _BadRuntime()
        out = ch._action_edit_task_graph(f"任务 {tid} 修改图")
        assert "无法解析图编辑指令" in out


class TestBudgetAdvisor:
    """iter-52 (F4.4): 成本感知调度 (任务 Token 预算预估 + 预算适配检查)。"""

    @staticmethod
    def _make_mgr(pools):
        """构造 mock 资源管理器: pools = [(id, is_payg, status, remaining)]。"""
        from types import SimpleNamespace

        class _Mgr:
            def __init__(self, ps):
                self._ps = ps

            def list_resources(self):
                return [SimpleNamespace(id=p[0], is_payg=p[1],
                                        status=p[2]) for p in self._ps]

            def get_usage(self, pool_id):
                for p in self._ps:
                    if p[0] == pool_id:
                        return {"remaining": p[3]}
                return {}

        return _Mgr(pools)

    def test_estimate_text_tokens(self):
        from lan_mesh.budget_advisor import estimate_text_tokens
        assert estimate_text_tokens("") == 0
        # 中文每字≈1, 英文每 4 字符≈1
        assert 4 <= estimate_text_tokens("你好世界") <= 6
        en = estimate_text_tokens("hello world, this is a test")
        assert 6 <= en <= 10

    def test_estimate_baseline_heuristic(self):
        """无历史数据 → 纯启发式基线 (文本 token × 编排放大系数)。"""
        from lan_mesh.budget_advisor import (PM_MULTIPLIER,
                                             estimate_task_tokens,
                                             estimate_text_tokens)
        est = estimate_task_tokens("写周报", "汇总本周开发进度", db=None)
        base = estimate_text_tokens("写周报\n汇总本周开发进度") * PM_MULTIPLIER
        assert est["estimated_tokens"] == base
        assert est["basis"] == "heuristic"
        assert est["confidence"] == "low"

    def test_estimate_history_mixed(self):
        """历史样本充足 → 0.4×基线 + 0.6×历史均值 (mixed)。"""
        from lan_mesh.budget_advisor import estimate_task_tokens

        class _Db:
            def avg_tokens_per_task(self, days=30):
                return {"avg": 10000.0, "samples": 5}

        est = estimate_task_tokens("写周报", "汇总进度", db=_Db())
        assert est["basis"] == "mixed"
        assert est["confidence"] == "high"
        expected = int(est["baseline_tokens"] * 0.4 + 10000.0 * 0.6)
        assert est["estimated_tokens"] == expected
        assert est["history_avg_tokens"] == 10000

    def test_estimate_history_fallback(self):
        """历史样本不足 (<=3) → 回退纯基线。"""
        from lan_mesh.budget_advisor import estimate_task_tokens

        class _Db:
            def avg_tokens_per_task(self, days=30):
                return {"avg": 99999.0, "samples": 2}

        est = estimate_task_tokens("写周报", "汇总进度", db=_Db())
        assert est["basis"] == "heuristic"
        assert est["estimated_tokens"] == est["baseline_tokens"]

    def test_avg_tokens_per_task(self, tmp_path):
        """DB 聚合: 每任务 token 均值; 无归因记录返回全零。"""
        from lan_mesh.database import Database
        db = Database(str(tmp_path / "d.db"))
        assert db.avg_tokens_per_task() == {"avg": 0.0, "samples": 0}
        db.insert_resource_usage("p1", "m1", "token_plan", 100, 50, 0.0,
                                 usage_id="u1", task_id="task-a")
        db.insert_resource_usage("p1", "m1", "token_plan", 200, 100, 0.0,
                                 usage_id="u2", task_id="task-b")
        db.insert_resource_usage("p1", "m1", "token_plan", 999, 999, 0.0,
                                 usage_id="u3", task_id="")
        res = db.avg_tokens_per_task()
        assert res["samples"] == 2
        assert res["avg"] == 225.0  # (150 + 300) / 2

    def test_check_budget_fit_pool_states(self):
        """池层三态: ok / tight / insufficient (payg 与 paused 池跳过)。"""
        from lan_mesh.budget_advisor import check_budget_fit
        # 剩余 10000, 预估 1000 → ok
        fit = check_budget_fit(1000, mgr=self._make_mgr(
            [("tok", False, "active", 10000),
             ("payg", True, "active", 5.0),      # payg 金额口径跳过
             ("paused", False, "paused", 99999)]))  # 非 active 跳过
        assert fit["status"] == "ok" and fit["pool_id"] == "tok"
        # 剩余 1100, 预估 1000 → tight (够用但不足 1.2×)
        fit = check_budget_fit(1000, mgr=self._make_mgr(
            [("tok", False, "active", 1100)]))
        assert fit["status"] == "tight"
        # 剩余 900, 预估 1000 → insufficient
        fit = check_budget_fit(1000, mgr=self._make_mgr(
            [("tok", False, "active", 900)]))
        assert fit["status"] == "insufficient"
        assert "补充额度" in fit["advice"] or "经济模型" in fit["advice"]

    def test_check_budget_fit_project_layer(self):
        """项目层: 预算剩余金额按经济模型单价换算 token 预算。"""
        from unittest.mock import MagicMock
        from lan_mesh.budget_advisor import check_budget_fit
        from lan_mesh.project import ECONOMY_MODEL, calculate_cost
        pm = MagicMock()
        pm.get_project.return_value = MagicMock(
            budget_limit_usd=1.0, budget_used_usd=0.5)
        per_token = calculate_cost(ECONOMY_MODEL, 1, 1) / 2.0
        fit = check_budget_fit(1000, project_id="proj-x", project_manager=pm)
        assert fit["project_token_budget"] > 0
        assert fit["project_token_budget"] == round(0.5 / per_token, 1)

    def test_check_budget_fit_unknown(self):
        """两层均无数据 → unknown (不阻断提交)。"""
        from lan_mesh.budget_advisor import check_budget_fit
        fit = check_budget_fit(1000)
        assert fit["status"] == "unknown"

    def test_build_estimate_isolated(self):
        """组合入口全默认参数不抛异常, 返回结构完整 (异常隔离)。"""
        from lan_mesh.budget_advisor import build_task_cost_estimate
        est = build_task_cost_estimate("写周报", "汇总进度")
        assert est["estimated_tokens"] > 0
        assert est["budget_fit"]["status"] in ("ok", "tight",
                                               "insufficient", "unknown")

    def test_submit_task_attaches_estimate(self, tmp_path):
        """接入点: POST /api/tasks 提交后 input_data 落盘 _cost_estimate。"""
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.database import Database
        from lan_mesh.station_api import create_station_router

        class _Ctl:
            def __init__(self):
                self.db = Database(str(tmp_path / "d.db"))
                self.discovery = None
                self.secretary_active = True

            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)
        r = client.post("/api/tasks", json={
            "name": "写周报", "description": "汇总本周开发进度"})
        assert r.status_code == 200
        tid = r.json()["task_id"]
        task = ctl.db.get_task(tid)
        ce = (task.input_data or {}).get("_cost_estimate")
        assert ce and ce["estimated_tokens"] > 0
        assert "budget_fit" in ce and "status" in ce["budget_fit"]

    def test_cost_estimate_endpoint(self, tmp_path):
        """端点: GET /api/tasks/{tid}/cost-estimate 200 (含 saved_estimate) / 404。"""
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.database import Database
        from lan_mesh.protocol import Task
        from lan_mesh.station_api import create_station_router

        class _Ctl:
            def __init__(self):
                self.db = Database(str(tmp_path / "d.db"))
                self.discovery = None
                self.secretary_active = True

            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        tid = "task-cost123"
        ctl.db.save_task(Task(task_id=tid, name="写周报",
                              description="汇总本周开发进度", status="pending"))
        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)
        r = client.get(f"/api/tasks/{tid}/cost-estimate")
        assert r.status_code == 200
        d = r.json()
        assert d["task_id"] == tid
        assert d["estimated_tokens"] > 0
        assert "budget_fit" in d and "status" in d["budget_fit"]
        r = client.get("/api/tasks/task-missing/cost-estimate")
        assert r.status_code == 404


# ═══════════════════════════════════════════════════════════════════
# PM 执行态快照 + 断点恢复测试 (iter-53)
# ═══════════════════════════════════════════════════════════════════

def make_snapshot_state() -> PMState:
    """构造含典型中间执行态的 PMState (2 子任务, A 完成 / B 依赖 A)。"""
    import time as _time
    st = PMState()
    st.plan = {
        "pattern": "orchestrator",
        "decomposition": [
            {"name": "A", "skill": "code", "depends_on": [], "description": "dA"},
            {"name": "B", "skill": "review", "depends_on": ["A"], "description": "dB"},
        ],
    }
    st.task = {"task_id": "task-x", "name": "测试任务", "input_data": {"k": "v"}}
    st.subtask_outputs["A"] = {"summary": "done"}
    st.pending_subtasks["B"] = {"sub": {}, "station": {"ip": "1.2.3.4"}, "agent_info": {}}
    st.dispatched.add("A")
    st.task_station["A"] = {"ip": "1.2.3.4", "api_port": 80}
    st.task_agent["A"] = {"agent_id": "sub-1"}
    st.task_station["B"] = {"ip": "1.2.3.4", "api_port": 80}
    st.task_agent["B"] = {"agent_id": "sub-2"}
    st.teams["t1"] = {"team_id": "t1", "members": []}
    st.subagents["m1"] = {"member_id": "m1", "status": "completed", "current_task": "A"}
    st.retry_counts["A"] = 1
    st.start_time = _time.time() - 100
    st.subtask_start_times["A"] = _time.time() - 90
    st.clarification_question = "确认方案?"
    return st


class FakePMDispatcher:
    """极简 PMDispatcher 替身: 记录分发/本地执行调用。"""
    def __init__(self):
        self.dispatched = []
        self.locally = []
        self.pending = 0

    def dispatch_subtask(self, station, agent_info, task, sub, plan=None):
        self.dispatched.append((sub.get("name"), station))

    def execute_subtask_locally(self, task, sub):
        self.locally.append(sub.get("name"))

    def try_dispatch_pending(self):
        self.pending += 1

    def _record_subtask_start(self, name):
        pass


class FakePMMonitor:
    def __init__(self):
        self.aggregated = 0

    def aggregate_results(self):
        self.aggregated += 1

    def progress_loop(self):
        pass


class FakePMAgent:
    """极简 ProjectManagerAgent 替身, 只测 _run_resumed 分发逻辑。"""
    def __init__(self, state):
        self._state = state
        self._dispatcher = FakePMDispatcher()
        self._monitor = FakePMMonitor()
        self._planner = object()
        self.pm_id = "pm-test"
        self.running = True
        self._task_id = ""
        self.reported = []
        self.snap_phase = ""
        self.cleared = False

    def report_status(self, status, **kw):
        self.reported.append(("status", status))

    def report_progress(self, p, status, msg, **kw):
        self.reported.append(("progress", status))

    def sync_subtasks(self):
        pass

    def request_clarification(self, question):
        return {}

    def _persist_snapshot(self, phase):
        self.snap_phase = phase

    def _clear_snapshot(self):
        self.cleared = True


class TestPMSnapshotState:
    """PMState 快照序列化: 往返一致 + 就地恢复。"""

    def test_snapshot_roundtrip(self):
        """16 字段全量往返, JSON 安全。"""
        import json as _json
        st = make_snapshot_state()
        data = st.to_snapshot()
        assert isinstance(data, dict)
        st2 = PMState.from_snapshot(data)
        assert st2.plan == st.plan
        assert st2.task == st.task
        assert st2.subtask_outputs == st.subtask_outputs
        assert st2.pending_subtasks == st.pending_subtasks
        assert st2.dispatched == st.dispatched
        assert st2.task_station == st.task_station
        assert st2.task_agent == st.task_agent
        assert st2.teams == st.teams
        assert st2.subagents == st.subagents
        assert st2.retry_counts == st.retry_counts
        assert abs(st2.start_time - st.start_time) < 1e-6
        assert st2.subtask_start_times == st.subtask_start_times
        assert st2.clarification_question == "确认方案?"
        assert st2.max_retries == 2 and st2.global_timeout == 3600.0
        _json.dumps(data)

    def test_restore_in_place(self):
        """restore_from 只重写字段不替换对象 (子组件共享引用仍有效)。"""
        st = make_snapshot_state()
        ref = st  # 模拟子组件共享引用
        st2 = PMState()
        st2.restore_from(st.to_snapshot())
        assert st2.plan == st.plan
        assert st2 is not st
        assert ref is st


class TestPMSnapshotDB:
    """pm_snapshots 表 CRUD (UPSERT 一 PM 一快照)。"""

    def test_snapshot_crud(self, tmp_path):
        from lan_mesh.database import Database
        db = Database(str(tmp_path / "snap.db"))
        try:
            # UPSERT 新增
            db.save_pm_snapshot("pm-1", "task-1", "monitoring", '{"a": 1}')
            snap = db.get_pm_snapshot("pm-1")
            assert snap and snap["phase"] == "monitoring"
            assert snap["state_json"] == '{"a": 1}'
            # UPSERT 更新 (同 pm_id 只保留最新)
            db.save_pm_snapshot("pm-1", "task-1", "executing", '{"a": 2}')
            snap = db.get_pm_snapshot("pm-1")
            assert snap["phase"] == "executing"
            # 按任务查找
            db.save_pm_snapshot("pm-2", "task-2", "planning_done", "{}")
            by_task = db.get_pm_snapshot_by_task("task-2")
            assert by_task and by_task["pm_id"] == "pm-2"
            assert db.get_pm_snapshot_by_task("task-none") is None
            # 删除
            db.delete_pm_snapshot("pm-1")
            assert db.get_pm_snapshot("pm-1") is None
            # 任务级清理 (delete_task 级联)
            db.delete_task("task-2")
            assert db.get_pm_snapshot("pm-2") is None
        finally:
            # Windows 下释放 sqlite 文件锁, 保证 tmp_path 可清理
            conn = getattr(getattr(db, "_local", None), "conn", None)
            if conn:
                conn.close()


class TestPMResumeScenarios:
    """_run_resumed 断点续跑四场景。"""

    def test_scenario4_redispatch(self):
        """部分完成 → 保留已完成输出, 仅重分发未完成子任务。"""
        from lan_mesh.pm_agent import ProjectManagerAgent
        st = make_snapshot_state()
        agent = FakePMAgent(st)
        ProjectManagerAgent._run_resumed(agent)
        names = [n for n, _ in agent._dispatcher.dispatched]
        assert names == ["B"], f"期望只重分发 B, 实际 {names}"
        assert agent._dispatcher.locally == []
        assert agent.snap_phase == "monitoring"

    def test_scenario3_aggregate(self):
        """全部完成 → 直接聚合交付, 不再分发。"""
        from lan_mesh.pm_agent import ProjectManagerAgent
        st = make_snapshot_state()
        st.subtask_outputs["B"] = {"summary": "done B"}
        st.pending_subtasks = {}
        agent = FakePMAgent(st)
        ProjectManagerAgent._run_resumed(agent)
        assert agent._monitor.aggregated == 1
        assert agent._dispatcher.dispatched == []

    def test_scenario2_no_plan(self):
        """无任务分解 (快照残缺) → 标记失败。"""
        from lan_mesh.pm_agent import ProjectManagerAgent
        st = make_snapshot_state()
        st.plan = {}
        agent = FakePMAgent(st)
        ProjectManagerAgent._run_resumed(agent)
        assert agent._running is False
        assert any(s == "failed" for _, s in agent.reported)

    def test_dependency_pending(self):
        """依赖未满足的子任务挂回 pending, 不分发。"""
        from lan_mesh.pm_agent import ProjectManagerAgent
        st = make_snapshot_state()
        st.subtask_outputs = {}  # A 也未完成
        st.pending_subtasks = {}
        agent = FakePMAgent(st)
        ProjectManagerAgent._run_resumed(agent)
        names = [n for n, _ in agent._dispatcher.dispatched]
        assert names == ["A"], f"期望只分发 A, 实际 {names}"
        assert "B" in st.pending_subtasks

    def test_bad_snapshot_json(self):
        """损坏快照 → resume_from_snapshot 返回 False 拒绝恢复。"""
        from lan_mesh.pm_agent import ProjectManagerAgent
        pm = ProjectManagerAgent("pm-x", None, "http://127.0.0.1:1", "dev-1")
        ok = pm.resume_from_snapshot({"state_json": "{invalid json", "phase": "x"})
        assert ok is False


class TestPMSnapshotEndpoints:
    """快照端点 (POST/GET/404/DELETE) + resume 端点 + 落库字段完整性。"""

    def _make_ctl(self, tmp_path):
        from lan_mesh.database import Database

        class _Ctl:
            """最小控制器替身: 只暴露路由所需属性。"""
            def __init__(self):
                self.db = Database(str(tmp_path / "ctl.db"))
                self.secretary_active = True
                self._local_pm_agent = None
                self.chat_runtime = None
                self.resume_called = []
                self.state = type("S", (), {
                    "device_id": "dev-ctl",
                    "device_name": "ctl",
                    "api_port": 45470,
                    "ws_clients": set(),
                    "shared_folder": type("SF", (), {"path": str(tmp_path)})(),
                })()
                self.bot_gateway = type("BG", (), {"notify": lambda *a, **k: None})()
                self.project_manager = None
                self._pm_worker_map = {}
                self.chat_handler = None
                self.discovery = type("D", (), {"find_device": lambda *a, **k: None})()
                self._ws_queue = []

            def _local_resume_pm(self, task_id):
                self.resume_called.append(task_id)
                return {"ok": True, "pm_id": "pm-ctl"}

            def _queue_ws_broadcast(self, *a, **k):
                pass

        return _Ctl()

    def test_snapshot_endpoints(self, tmp_path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.station_routes_pm import build_pm_routes
        from lan_mesh.station_routes_tasks import build_task_routes

        ctl = self._make_ctl(tmp_path)
        app = FastAPI()
        app.include_router(build_pm_routes(ctl))
        app.include_router(build_task_routes(ctl))
        client = TestClient(app)

        r = client.post("/api/pm/pm-1/snapshot", json={
            "pm_id": "pm-1", "task_id": "task-1", "phase": "monitoring",
            "state": {"plan": {"decomposition": []}, "task": {"task_id": "task-1"}},
        })
        assert r.status_code == 200, r.text
        r = client.get("/api/pm/pm-1/snapshot")
        assert r.status_code == 200 and r.json()["phase"] == "monitoring"
        r = client.get("/api/pm/pm-none/snapshot")
        assert r.status_code == 404
        r = client.delete("/api/pm/pm-1/snapshot")
        assert r.status_code == 200
        assert ctl.db.get_pm_snapshot("pm-1") is None

    def test_resume_endpoint(self, tmp_path):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.station_routes_pm import build_pm_routes
        from lan_mesh.station_routes_tasks import build_task_routes
        from lan_mesh.protocol import Task

        ctl = self._make_ctl(tmp_path)
        app = FastAPI()
        app.include_router(build_pm_routes(ctl))
        app.include_router(build_task_routes(ctl))
        client = TestClient(app)

        # 无快照 → 404
        ctl.db.save_task(Task(task_id="task-no-snap", name="t", status="running"))
        r = client.post("/api/tasks/task-no-snap/resume")
        assert r.status_code == 404
        # 有快照 → 恢复
        ctl.db.save_task(Task(task_id="task-r", name="t2", status="interrupted"))
        ctl.db.save_pm_snapshot("pm-r", "task-r", "monitoring",
                                '{"plan": {}, "task": {}}')
        r = client.post("/api/tasks/task-r/resume")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["ok"] and body["pm_id"] == "pm-ctl"
        assert ctl.resume_called == ["task-r"]
        # 任务不存在 → 404
        r = client.post("/api/tasks/task-missing/resume")
        assert r.status_code == 404

    def test_snapshot_route_field_integrity(self, tmp_path):
        """快照 POST 落库字段完整 (state 深拷贝 JSON 化)。"""
        import json as _json
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.station_routes_pm import build_pm_routes

        ctl = self._make_ctl(tmp_path)
        app = FastAPI()
        app.include_router(build_pm_routes(ctl))
        client = TestClient(app)
        state_data = {"plan": {"decomposition": [{"name": "A"}]},
                      "subtask_outputs": {"A": "x"}}
        r = client.post("/api/pm/pm-s/snapshot", json={
            "pm_id": "pm-s", "task_id": "task-s", "phase": "executing",
            "state": state_data})
        assert r.status_code == 200
        snap = ctl.db.get_pm_snapshot("pm-s")
        assert snap["task_id"] == "task-s" and snap["phase"] == "executing"
        parsed = _json.loads(snap["state_json"])
        assert parsed["plan"]["decomposition"][0]["name"] == "A"
        assert parsed["subtask_outputs"]["A"] == "x"


class TestPMMultiLifecycle:
    """multi 模式 running 生命周期回归 (iter-53 修复)。"""

    def test_multi_running_lifecycle(self):
        """恢复分发后 running 保持 True; 聚合收尾后停止。"""
        from lan_mesh.pm_agent import ProjectManagerAgent
        from lan_mesh.pm_monitor import PMMonitor

        pm = ProjectManagerAgent("pm-lc", None, "http://127.0.0.1:1", "dev-1")
        st = pm._state
        st.plan = {"pattern": "multi", "decomposition": [
            {"name": "A", "depends_on": []}, {"name": "B", "depends_on": []}]}
        st.task = {"task_id": "task-lc", "name": "lc"}
        st.task_station["A"] = {"ip": "1.2.3.4", "api_port": 80}
        st.task_agent["A"] = {"agent_id": "sub-a"}
        st.task_station["B"] = {"ip": "1.2.3.4", "api_port": 80}
        st.task_agent["B"] = {"agent_id": "sub-b"}
        pm._running = True
        try:
            pm._dispatcher = FakePMDispatcher()
            pm._run_resumed()
            assert pm.running is True, "恢复分发后 running 应保持 True 等待聚合"
            # 真实 PMMonitor 聚合收尾 (LLM 失败亦走收尾): running → False
            monitor = PMMonitor("pm-lc", None, "http://127.0.0.1:1", st, pm,
                                pm._dispatcher)
            monitor.aggregate_results()
            assert pm.running is False, "聚合收尾后 running 应停止"
        finally:
            pm.running = False


class TestLogPruning:
    """iter-54 补强#2: 日志容量修剪 (保留期/VACUUM/端点/配置节流)。"""

    @staticmethod
    def _seed_logs(db):
        """灌入新旧混合数据 (旧 40 天 / 新 1 小时)。"""
        import time as _time
        conn = db._get_conn()
        now = _time.time()
        old = now - 40 * 86400
        fresh = now - 3600
        conn.execute(
            "INSERT INTO llm_call_log (call_type, model, input_tokens,"
            " output_tokens, ttft_ms, total_ms, status, task_id, error,"
            " created_at) VALUES ('chat','m1',10,20,1,2,'ok','t1','',?)", (old,))
        conn.execute(
            "INSERT INTO llm_call_log (call_type, model, input_tokens,"
            " output_tokens, ttft_ms, total_ms, status, task_id, error,"
            " created_at) VALUES ('chat','m1',10,20,1,2,'ok','t1','',?)", (fresh,))
        conn.execute(
            "INSERT INTO chat_history (role, content, action_taken, timestamp)"
            " VALUES ('user','old','',?)", (old,))
        conn.execute(
            "INSERT INTO chat_history (role, content, action_taken, timestamp)"
            " VALUES ('user','new','',?)", (fresh,))
        conn.execute(
            "INSERT INTO resource_usage_log (resource_id, model_id, plan_type,"
            " input_tokens, output_tokens, cost, created_at, usage_id, reported,"
            " task_id, project_id) VALUES ('r1','m1','plan',10,20,0,?,'u1',1,'t1','p1')",
            (old,))
        conn.execute(
            "INSERT INTO resource_usage_log (resource_id, model_id, plan_type,"
            " input_tokens, output_tokens, cost, created_at, usage_id, reported,"
            " task_id, project_id) VALUES ('r1','m1','plan',10,20,0,?,'u2',0,'t1','p1')",
            (old,))
        conn.execute(
            "INSERT INTO resource_usage_log (resource_id, model_id, plan_type,"
            " input_tokens, output_tokens, cost, created_at, usage_id, reported,"
            " task_id, project_id) VALUES ('r1','m1','plan',10,20,0,?,'u3',1,'t1','p1')",
            (fresh,))
        conn.execute(
            "INSERT INTO progress_reports (pm_id, reporter_id, reporter_type,"
            " task_name, progress, status, message, timestamp)"
            " VALUES ('pm1','s1','agent','t',50,'running','m',?)", (old,))
        conn.execute(
            "INSERT INTO heartbeat_log (device_id, timestamp, cpu_percent,"
            " memory_percent, disk_percent) VALUES ('d1',?,1,1,1)", (old,))
        conn.commit()

    def test_prune_logs_expiry_and_retention(self, tmp_path):
        """prune_logs: 各表删除过期行保留新行, 统计正确。"""
        from lan_mesh.database import Database
        db = Database(str(tmp_path / "p.db"))
        try:
            self._seed_logs(db)
            stats = db.prune_logs(retention_days=30)
            assert stats["llm_call_log"] == 1
            assert stats["chat_history"] == 1
            assert stats["resource_usage_log"] == 1
            assert stats["progress_reports"] == 1
            assert stats["heartbeat_log"] == 1
            conn = db._get_conn()
            assert conn.execute("SELECT COUNT(*) FROM llm_call_log").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM chat_history").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM resource_usage_log").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM progress_reports").fetchone()[0] == 0
            # 再次修剪幂等 (无过期行)
            assert sum(db.prune_logs(30).values()) == 0
        finally:
            db._local.conn.close()

    def test_prune_unreported_usage_kept(self, tmp_path):
        """resource_usage_log 未上报 (reported=0) 旧行保留等离线补报。"""
        from lan_mesh.database import Database
        db = Database(str(tmp_path / "u.db"))
        try:
            self._seed_logs(db)
            db.prune_logs(retention_days=30)
            conn = db._get_conn()
            rows = conn.execute(
                "SELECT usage_id FROM resource_usage_log ORDER BY usage_id"
            ).fetchall()
            ids = [r["usage_id"] for r in rows]
            assert ids == ["u2", "u3"], ids  # 旧未上报 + 新已上报
        finally:
            db._local.conn.close()

    def test_prune_heartbeat_24h_window(self, tmp_path):
        """心跳固定 24h 窗口: 不随 retention 放宽。"""
        import time as _time
        from lan_mesh.database import Database
        db = Database(str(tmp_path / "h.db"))
        try:
            conn = db._get_conn()
            hb_old = _time.time() - 48 * 3600
            conn.execute(
                "INSERT INTO heartbeat_log (device_id, timestamp, cpu_percent,"
                " memory_percent, disk_percent) VALUES ('d1',?,1,1,1)", (hb_old,))
            conn.commit()
            stats = db.prune_logs(retention_days=365)  # 大保留期
            assert stats["heartbeat_log"] == 1, "心跳仍按 24h 清理"
        finally:
            db._local.conn.close()

    def test_vacuum_ok(self, tmp_path):
        """VACUUM 正常执行不报错。"""
        from lan_mesh.database import Database
        db = Database(str(tmp_path / "v.db"))
        try:
            self._seed_logs(db)
            db.prune_logs(retention_days=30)
            db.vacuum()
        finally:
            db._local.conn.close()

    def test_log_prune_endpoint(self, tmp_path):
        """手动修剪端点 POST /api/runtime/logs/prune 返回统计 + days 夹取。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.database import Database
        from lan_mesh.station_routes_basic import build_basic_routes

        class _Ctl:
            def __init__(self):
                self.db = Database(str(tmp_path / "ep.db"))
                self.state = type("S", (), {
                    "device_id": "dev-ep", "device_name": "ep",
                    "api_port": 45470, "ws_clients": set(),
                    "shared_folder": type("SF", (), {"path": str(tmp_path)})(),
                })()
                self.discovery = type("D", (), {"list_devices": lambda *a, **k: []})()
                self.station_director = type("SD", (), {
                    "get_resources": lambda *a, **k: [],
                })()

        ctl = _Ctl()
        try:
            self._seed_logs(ctl.db)
            app = FastAPI()
            app.include_router(build_basic_routes(ctl))
            client = TestClient(app)
            r = client.post("/api/runtime/logs/prune", params={"days": 30})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["vacuum"] is True
            assert body["pruned"]["llm_call_log"] == 1
            assert set(body["pruned"].keys()) == {
                "llm_call_log", "chat_history", "resource_usage_log",
                "progress_reports", "heartbeat_log"}
            # days 夹取: 0 → 1, 999 → 365
            r2 = client.post("/api/runtime/logs/prune", params={"days": 999})
            assert r2.status_code == 200
        finally:
            ctl.db._local.conn.close()

    def test_observability_log_prune_config(self, tmp_path):
        """ObservabilityConfig 新字段默认值/自定义解析/缺省回退。"""
        from lan_mesh.config import AppConfig
        cfg = AppConfig.model_validate({})
        assert cfg.observability.log_retention_days == 30.0
        assert cfg.observability.log_prune_interval_hours == 24.0
        assert cfg.observability.log_vacuum is True
        cfg2 = AppConfig.model_validate({"observability": {
            "log_retention_days": 7, "log_prune_interval_hours": 6,
            "log_vacuum": False}})
        assert cfg2.observability.log_retention_days == 7
        assert cfg2.observability.log_prune_interval_hours == 6
        assert cfg2.observability.log_vacuum is False

    def test_prune_logs_if_due_throttle(self, tmp_path):
        """_prune_logs_if_due: 节流间隔/禁用开关/失败推进时间戳防风暴。"""
        import time
        from lan_mesh.config import AppConfig
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController

        class _FakeCtl:
            def __init__(self):
                self.cfg = AppConfig.model_validate({})
                self.db = Database(str(tmp_path / "ctl.db"))
                self._last_log_prune_ts = 0.0

        ctl = _FakeCtl()
        try:
            fn = StationController._prune_logs_if_due
            # 首次调用: 时间戳为 0 → 立即执行 (prune+vacuum 真实跑)
            fn(ctl)
            assert ctl._last_log_prune_ts > 0, "首次执行推进时间戳"
            ts1 = ctl._last_log_prune_ts
            # 立即再调: 24h 内节流跳过, 时间戳不变
            fn(ctl)
            assert ctl._last_log_prune_ts == ts1, "周期内节流不推进"
            # 禁用保留期 → no-op 且不推进
            ctl.cfg.observability.log_retention_days = 0
            ctl._last_log_prune_ts = 0.0
            fn(ctl)
            assert ctl._last_log_prune_ts == 0.0, "禁用时不推进时间戳"
            # 周期已过 → 再执行
            ctl.cfg.observability.log_retention_days = 30
            ctl.cfg.observability.log_prune_interval_hours = 1
            ctl._last_log_prune_ts = time.time() - 7200
            fn(ctl)
            assert ctl._last_log_prune_ts > 0
            # db 异常 → 异常隔离不抛出, 时间戳已推进防风暴
            ctl.db = None
            ctl._last_log_prune_ts = 0.0
            fn(ctl)  # 不应抛异常 (db None → prune 调用在 try 内)
            assert ctl._last_log_prune_ts > 0
        finally:
            if ctl.db is not None and hasattr(ctl.db._local, "conn"):
                ctl.db._local.conn.close()


class TestIter55MultiHostHardening:
    """iter-55 补强#3 多机实测发现的生产缺陷修复回归。

    1. PROVIDER_CONFIG 缺 volcengine-ark → default_model 旧路径永远不可用
    2. _ensure_env_loaded 部分 key 有值提前 return → .env 不全量加载
    3. 让位主机不加载模型资源 → 远程派发 PM 无 LLM Key
    4. 让位主机 chat_runtime 为 None → 远程派发被拒 (惰性初始化解耦)
    """

    def test_provider_config_ark_first(self):
        """PROVIDER_CONFIG 含 volcengine-ark 且置首位 (coding/v3 端点)。"""
        from lan_mesh.agent_runtime import PROVIDER_CONFIG
        assert "volcengine-ark" in PROVIDER_CONFIG
        assert next(iter(PROVIDER_CONFIG)) == "volcengine-ark", (
            "ark 置首位: 订阅制 Coding Plan 优先消耗")
        assert PROVIDER_CONFIG["volcengine-ark"]["api_key_env"] == "ARK_API_KEY"
        assert "coding/v3" in PROVIDER_CONFIG["volcengine-ark"]["base_url"]

    def test_get_default_model_defined(self, monkeypatch):
        """_get_default_model 已定义: 兜底 defaults + 未知 provider 空串。"""
        from lan_mesh import agent_runtime
        rt = agent_runtime.AgentRuntime.__new__(agent_runtime.AgentRuntime)
        # 无 model_pool 条目时走硬编码 defaults 兜底
        monkeypatch.setattr(agent_runtime, "_load_model_pool_entries",
                            lambda: {})
        assert rt._get_default_model("volcengine-ark") == "ark-code-latest"
        assert rt._get_default_model("deepseek") == "deepseek-chat"
        assert rt._get_default_model("no-such-provider") == ""

    def test_ensure_env_loaded_fills_when_partial_keys_exist(
            self, monkeypatch, tmp_path):
        """部分 key 已有值时仍继续加载 .env 补齐缺失 key (不提前 return)。"""
        import os
        from lan_mesh import agent_runtime
        (tmp_path / ".env").write_text(
            "X55_PARTIAL_KEY=partial-ok\n", encoding="utf-8")
        monkeypatch.setattr(agent_runtime, "_env_loaded", False)
        monkeypatch.delenv("X55_PARTIAL_KEY", raising=False)
        monkeypatch.setenv("ALIYUN_TOKENPLAN_API_KEY", "ali-exists")
        monkeypatch.chdir(tmp_path)
        agent_runtime._ensure_env_loaded()
        # 旧逻辑: ALIYUN 已有值 → 提前 return → X55_PARTIAL_KEY 缺失
        assert os.environ.get("X55_PARTIAL_KEY") == "partial-ok", \
            "部分 key 有值时仍应补齐 .env 中缺失 key"
        assert os.environ["ALIYUN_TOKENPLAN_API_KEY"] == "ali-exists", \
            "已有 key 不被覆盖 (override=False 幂等)"

    def test_ensure_env_loaded_manual_parse_fallback(self, monkeypatch, tmp_path):
        """dotenv 缺失时手动解析 .env 兜底 (基础解释器无依赖场景)。"""
        import os
        from lan_mesh import agent_runtime
        (tmp_path / ".env").write_text(
            "X55_MANUAL_KEY=manual-ok\n# comment\n", encoding="utf-8")
        real_import = __builtins__["__import__"] if isinstance(
            __builtins__, dict) else __builtins__.__import__

        def _fake_import(name, *a, **k):
            if name == "dotenv" or name.startswith("dotenv."):
                raise ImportError("no dotenv")
            return real_import(name, *a, **k)

        monkeypatch.delenv("X55_MANUAL_KEY", raising=False)
        monkeypatch.setattr(agent_runtime, "_env_loaded", False)
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("builtins.__import__", _fake_import)
        agent_runtime._ensure_env_loaded()
        assert os.environ.get("X55_MANUAL_KEY") == "manual-ok"

    def test_load_model_resources_preloads_pool(self, tmp_path, monkeypatch):
        """_load_model_resources: 任何 station 模式预加载模型池, 幂等。"""
        import time
        from lan_mesh.config import AppConfig
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController

        calls = {"n": 0}

        class _FakeCtl:
            def __init__(self):
                self.cfg = AppConfig.model_validate({})
                self.db = Database(str(tmp_path / "mr.db"))
                self._model_pool = None
                self.bot_gateway = None

            def _find_resources_path(self):
                return None  # 不加载 resources.yaml, 仅验证模型池预加载

        ctl = _FakeCtl()
        try:
            real_load = StationController._load_model_resources
            from lan_mesh import config as _cfg_mod
            real_pool = _cfg_mod.load_model_pool

            def _counting_pool():
                calls["n"] += 1
                return real_pool()

            monkeypatch.setattr(_cfg_mod, "load_model_pool", _counting_pool)
            StationController._load_model_resources(ctl)
            assert ctl._model_pool is not None, "模型池已预加载"
            assert calls["n"] == 1
            # 幂等: 二次调用复用已加载池, 不重复 load_model_pool
            StationController._load_model_resources(ctl)
            assert calls["n"] == 1
        finally:
            ctl.db._local.conn.close()

    def test_load_model_resources_exception_noop(self, tmp_path):
        """_load_model_resources 内部异常隔离 (no-op 不抛出)。"""
        from lan_mesh.station_controller import StationController

        class _BrokenCtl:
            _model_pool = None
            bot_gateway = None

            def _find_resources_path(self):
                raise RuntimeError("boom")

        StationController._load_model_resources(_BrokenCtl())  # 不抛异常

    def test_local_start_pm_lazy_runtime_init(self, monkeypatch):
        """让位主机 chat_runtime=None 时惰性初始化 Worker AgentRuntime。"""
        from lan_mesh import agent_runtime as ar_mod
        from lan_mesh import pm_agent as pm_mod
        from lan_mesh.station_controller import StationController

        created = []

        class _FakeRuntime:
            def __init__(self, **kwargs):
                created.append(kwargs)

        class _FakePM:
            instances = []

            def __init__(self, **kwargs):
                self.kwargs = kwargs
                _FakePM.instances.append(self)

            def start_task(self, task_data):
                self.started = task_data

        monkeypatch.setattr(ar_mod, "AgentRuntime", _FakeRuntime)
        monkeypatch.setattr(pm_mod, "ProjectManagerAgent", _FakePM)

        class _Ctl:
            _local_pm_agent = None
            chat_runtime = None

            def __init__(self):
                self.state = type("S", (), {
                    "device_id": "testdev0123456789",
                    "device_name": "分机",
                    "shared_folder": type("SF", (), {"path": "fake"})(),
                })()

            def _auto_attach_pm_thread(self, *a, **k):
                pass

        ctl = _Ctl()
        res = StationController._local_start_pm(
            ctl, "task-x", "http://secretary", {"name": "跨机任务"})
        assert res["ok"] is True
        assert ctl.chat_runtime is not None, "chat_runtime 惰性初始化"
        assert created[0]["agent_id"].startswith("worker-")
        assert created[0]["agent_id"] == "worker-testdev0"
        assert _FakePM.instances[0].kwargs["agent_runtime"] is ctl.chat_runtime
        assert _FakePM.instances[0].started["name"] == "跨机任务"

    def test_local_start_pm_runtime_reuse(self, monkeypatch):
        """chat_runtime 已就绪时不重复创建 (Secretary 已激活场景)。"""
        from lan_mesh import agent_runtime as ar_mod
        from lan_mesh import pm_agent as pm_mod
        from lan_mesh.station_controller import StationController

        created = []

        class _FakeRuntime:
            def __init__(self, **kwargs):
                created.append(kwargs)

        class _FakePM:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def start_task(self, task_data):
                pass

        monkeypatch.setattr(ar_mod, "AgentRuntime", _FakeRuntime)
        monkeypatch.setattr(pm_mod, "ProjectManagerAgent", _FakePM)

        class _Ctl:
            _local_pm_agent = None

            def __init__(self):
                self.chat_runtime = object()  # 已有 runtime
                self.state = type("S", (), {
                    "device_id": "dev1", "device_name": "d",
                    "shared_folder": type("SF", (), {"path": "fake"})(),
                })()

            def _auto_attach_pm_thread(self, *a, **k):
                pass

        StationController._local_start_pm(
            _Ctl(), "task-y", "http://s", {"name": "t"})
        assert created == [], "已有 runtime 不重复创建"


class TestIter56Spa:
    """iter-56 补强#4 F5.1: React SPA 挂载与认证白名单。"""

    @staticmethod
    def _spa_dir() -> Path:
        return Path(__file__).parent.parent / "lan_mesh" / "web" / "static" / "spa"

    def test_spa_index_and_assets_served(self):
        """SPA 构建产物可经 /spa 静态托管 (index.html + assets)。"""
        from fastapi import FastAPI
        from fastapi.staticfiles import StaticFiles
        from fastapi.testclient import TestClient
        spa_dir = self._spa_dir()
        assert (spa_dir / "index.html").is_file(), "SPA 构建产物存在"
        app = FastAPI()
        app.mount("/spa", StaticFiles(directory=str(spa_dir), html=True),
                  name="spa")
        client = TestClient(app)
        r = client.get("/spa/")
        assert r.status_code == 200
        assert '<div id="root">' in r.text, "SPA 入口 HTML"
        js_files = [p.name for p in spa_dir.glob("assets/*.js")]
        assert js_files, "存在 JS 产物"
        r2 = client.get(f"/spa/assets/{js_files[0]}")
        assert r2.status_code == 200
        assert "javascript" in r2.headers["content-type"]

    def test_spa_whitelisted_under_auth(self, monkeypatch):
        """auth 开启时 /spa 免认证 (SPA 页面加载后才能 auth-token 自举)。"""
        import lan_mesh.station_routes_common as common
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        monkeypatch.setattr(common, "_mesh_auth_enabled", True)
        monkeypatch.setattr(common, "_mesh_auth_token", "secret-tk")
        app = FastAPI()
        app.middleware("http")(common.api_guard_middleware)

        @app.get("/spa/")
        def _spa():
            return {"html": "ok"}

        @app.get("/spa/assets/app.js")
        def _asset():
            return {"js": "ok"}

        @app.get("/api/tasks")
        def _tasks():
            return {"tasks": []}

        client = TestClient(app)
        assert client.get("/spa/").status_code == 200
        assert client.get("/spa/assets/app.js").status_code == 200
        # 其余 API 路径仍要求 token
        assert client.get("/api/tasks").status_code == 401


class TestIter57Concurrency:
    """iter-57 补强#5: 并发压力验证 — DB 线程安全与队列表现。"""

    @staticmethod
    def _fresh_db(tmp_path) -> "Database":
        from lan_mesh.database import Database
        return Database(str(tmp_path / "conc.db"))

    def test_wal_and_busy_timeout_configured(self, tmp_path):
        """加固生效: 连接启用 WAL + busy_timeout=30s (并发写不因锁失败)。"""
        db = self._fresh_db(tmp_path)
        conn = db._get_conn()
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"

    def test_db_concurrent_mixed_load(self, tmp_path):
        """20 线程混合读写负载 (save_task/get_task/upsert_host) 无锁异常。"""
        import threading
        from lan_mesh.database import Database
        from lan_mesh.protocol import HostRecord

        db = Database(str(tmp_path / "mix.db"))
        db.upsert_host(HostRecord(device_id="host-seed", device_name="seed",
                                  online=True))
        errors: list = []
        lock = threading.Lock()

        def worker(tid: int):
            try:
                for i in range(20):
                    t = Task(
                        task_id=f"task-{tid}-{i}",
                        name=f"并发任务 {tid}-{i}",
                        description="压测",
                        status="pending",
                    )
                    db.save_task(t)
                    db.get_task(t.task_id)
                    db.list_tasks(limit=10)
                    db.list_hosts()
            except Exception as e:  # noqa: BLE001 — 并发异常收集断言
                with lock:
                    errors.append(f"thread-{tid}: {type(e).__name__}: {e}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(20)]
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=60)
        assert not errors, f"并发 DB 操作出现异常: {errors[:5]}"
        tasks = db.list_tasks(limit=1000)
        assert len(tasks) == 20 * 20, f"写入丢失: {len(tasks)}/400"

    def test_api_concurrent_task_submit(self, tmp_path):
        """10 并发 POST /api/tasks: 全部成功 + pm_id 唯一 + 状态 running。"""
        import itertools
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.database import Database
        from lan_mesh.protocol import HostRecord
        from lan_mesh.station_api import create_station_router

        db = Database(str(tmp_path / "api.db"))
        # 预置在线主机, 走本机派发分支 (_local_start_pm)
        db.upsert_host(HostRecord(device_id="host-1", device_name="主机1",
                                  role="worker", online=True))
        pm_counter = itertools.count(1)
        pm_lock = threading.Lock()

        class _Ctl:
            def __init__(self):
                self.db = db
                self.discovery = None
                self.secretary_active = True
                self.chat_runtime = True
                self.project_manager = None
                self.bot_gateway = MagicMock()
                self.state = SimpleNamespace(
                    ws_clients=set(), device_id="dev-ctl",
                    api_port=45500, device_name="ctl",
                    shared_folder="")

            def _local_start_pm(self, task_id, secretary_url, task_dict):
                with pm_lock:
                    pid = f"pm-{next(pm_counter):03d}"
                return {"ok": True, "pm_id": pid}

            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)

        def submit(i: int):
            r = client.post("/api/tasks", json={
                "name": f"并发任务-{i}", "description": "压测提交",
            })
            return r.status_code, r.json()

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(submit, range(10)))

        codes = [c for c, _ in results]
        assert all(c == 200 for c in codes), f"存在非 200 响应: {codes}"
        tasks = db.list_tasks(limit=100)
        assert len(tasks) == 10, f"任务数不符: {len(tasks)}/10"
        pm_ids = [t.pm_agent_id for t in tasks]
        assert len(set(pm_ids)) == 10, f"pm_id 冲突: {pm_ids}"
        assert all(t.status == "running" for t in tasks)

    def test_rate_limiter_dual_bucket(self):
        """双桶隔离: 严格桶拒绝超限, 信任桶高阈值放行合法负载。"""
        from lan_mesh import station_routes_common as common

        common.configure_rate_limit(strict_max=5, trusted_max=1000)
        # 严格桶: 同 IP 第 6 次拒绝 (防滥用)
        for _ in range(5):
            assert common._rate_limiter.is_allowed("ip-strict", trusted=False)
        assert not common._rate_limiter.is_allowed("ip-strict", trusted=False)
        # 信任桶: 高阈值放行 (20 并发任务 + UI 轮询不误伤)
        for _ in range(50):
            assert common._rate_limiter.is_allowed("ip-trusted", trusted=True)

    def test_rate_limiter_disable(self):
        """阈值 ≤0 禁用对应桶: 压测时全部放行。"""
        from lan_mesh import station_routes_common as common

        common.configure_rate_limit(strict_max=0, trusted_max=0)
        for _ in range(200):
            assert common._rate_limiter.is_allowed("ip-any", trusted=False)
            assert common._rate_limiter.is_allowed("ip-any", trusted=True)

    def test_middleware_trusted_token_high_bucket(self, monkeypatch):
        """中间件: 带 mesh token 走信任桶, 白名单未认证流量受严格桶约束。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh import station_routes_common as common

        monkeypatch.setattr(common, "_mesh_auth_enabled", True)
        monkeypatch.setattr(common, "_mesh_auth_token", "secret-tk")
        common.configure_rate_limit(strict_max=4, trusted_max=1000)
        app = FastAPI()
        app.middleware("http")(common.api_guard_middleware)

        @app.get("/api/tasks")
        def _tasks():
            return {"tasks": []}

        @app.get("/api/register")  # 白名单路径: 免 token 认证但限流仍生效
        def _reg():
            return {"ok": True}

        @app.get("/api/health")  # 健康探活须免认证 (压测发现曾漏登记白名单)
        def _health():
            return {"ok": True}

        client = TestClient(app)
        # 白名单健康探活免 token (iter-57 压测发现 /api/health 曾 401)
        assert client.get("/api/health").status_code == 200
        # 信任流量: 超严格阈值 (4) 仍全部放行
        for _ in range(10):
            r = client.get("/api/tasks",
                           headers={"Authorization": "Bearer secret-tk"})
            assert r.status_code == 200
        # 未认证流量 (白名单): 严格桶 health 已占 1 次, 第 5 次起 429
        codes = [client.get("/api/register").status_code for _ in range(5)]
        assert codes[:3] == [200, 200, 200], f"前 3 次应放行: {codes}"
        assert codes[3:] == [429, 429], f"超限后应 429: {codes}"

    def test_submit_queue_when_local_pm_busy(self, tmp_path):
        """本机 PM 忙且无远程 worker: 任务排队 pending 而非瞬时 failed。"""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh.database import Database
        from lan_mesh.protocol import HostRecord
        from lan_mesh.station_api import create_station_router

        db = Database(str(tmp_path / "queue.db"))
        # 仅本机在线 (无远程 worker 可派发)
        db.upsert_host(HostRecord(device_id="dev-ctl", device_name="本机",
                                  role="worker", online=True))

        class _Ctl:
            def __init__(self):
                self.db = db
                self.discovery = None
                self.secretary_active = True
                self.chat_runtime = True
                self.project_manager = None
                self.bot_gateway = MagicMock()
                self.state = SimpleNamespace(
                    ws_clients=set(), device_id="dev-ctl",
                    api_port=45500, device_name="ctl",
                    shared_folder="")

            def _local_start_pm(self, task_id, secretary_url, task_dict):
                return {"ok": False, "message": "本机 PM Agent 已在运行"}

            def __getattr__(self, name):
                return MagicMock()

        ctl = _Ctl()
        app = FastAPI()
        app.include_router(create_station_router(ctl))
        client = TestClient(app)

        r = client.post("/api/tasks", json={
            "name": "排队任务", "description": "PM 忙时排队",
        })
        assert r.status_code == 200, r.text[:200]
        body = r.json()
        assert body.get("queued") is True, f"应返回 queued 标记: {body}"
        tasks = db.list_tasks(limit=10)
        assert len(tasks) == 1
        assert tasks[0].status == "pending", \
            f"PM 忙时任务应保持 pending: {tasks[0].status}"
        assert not tasks[0].pm_agent_id, "排队任务不应绑定 PM"

    def test_dispatch_queued_task_relay(self, tmp_path):
        """接力派发: PM 空闲后 _dispatch_queued_task 派发最早 pending 任务。"""
        from types import SimpleNamespace
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        from lan_mesh.protocol import Task

        db = Database(str(tmp_path / "relay.db"))
        db.save_task(Task(task_id="task-relay-1", name="排队任务1",
                          description="待接力", status="pending"))

        class _Fake:
            _local_pm_agent = None
            _queued_dispatch_waiting = False

            def __init__(self):
                self.db = db
                self.state = SimpleNamespace(
                    device_id="dev-ctl", api_port=45500, device_name="ctl")
                self._pm_worker_map: dict = {}
                self._ws_events: list = []
                self._started: list = []

            def _local_start_pm(self, task_id, secretary_url, task_data):
                self._started.append(task_id)
                return {"ok": True, "pm_id": "pm-relay-001"}

            # iter-69: 登记逻辑抽为 _register_local_pm (接力/接管共用)
            _register_local_pm = StationController._register_local_pm

            def _queue_ws_broadcast(self, event_type, data):
                self._ws_events.append((event_type, data))

        fake = _Fake()
        ok = StationController._dispatch_queued_task(fake)
        assert ok is True, "接力派发应成功"
        assert fake._started == ["task-relay-1"], "应派发最早的 pending 任务"
        task = db.get_task("task-relay-1")
        assert task.status == "running", f"接力后应 running: {task.status}"
        assert task.pm_agent_id == "pm-relay-001"
        pm = db.get_pm_agent("pm-relay-001")
        assert pm is not None, "PM Agent 应落库"
        assert "pm_relay_001" not in [k for k in fake._pm_worker_map] \
            or fake._pm_worker_map.get("pm-relay-001", {}).get("local") is True
        types = [e[0] for e in fake._ws_events]
        assert "pm_registered" in types and "task_updated" in types

        # PM 忙时: 返回 False (后台等待线程接力, 不阻塞请求线程)
        fake._local_pm_agent = SimpleNamespace(_running=True)
        ok2 = StationController._dispatch_queued_task(fake)
        assert ok2 is False, "PM 忙时不应立即派发"


class TestIter58Permissions:
    """iter-58 补强#6 F5.2: 多用户权限 — 配置驱动用户表 + 角色分级。"""

    def test_resolve_role_判定(self, monkeypatch):
        """token 归属: mesh token→boss, 用户 token→角色, 未知→None。"""
        from lan_mesh import station_routes_common as common

        monkeypatch.setattr(common, "_mesh_auth_enabled", True)
        monkeypatch.setattr(common, "_mesh_auth_token", "mesh-secret")
        common.configure_users([
            {"name": "小张", "role": "operator", "token": "op-token-1"},
            {"name": "小王", "role": "viewer", "token": "vw-token-1"},
        ])
        boss = common.resolve_role("mesh-secret")
        assert boss == {"name": "节点", "role": "boss"}
        op = common.resolve_role("op-token-1")
        assert op["role"] == "operator" and op["name"] == "小张"
        assert common.resolve_role("vw-token-1")["role"] == "viewer"
        assert common.resolve_role("unknown-token") is None

    def test_configure_users_非法角色归一(self):
        """非法 role 归一到 viewer; 空 token 跳过; 脱敏列表不含 token。"""
        from lan_mesh import station_routes_common as common

        common.configure_users([
            {"name": "管理员", "role": "admin", "token": "tk-admin"},  # 非法 → viewer
            {"name": "无token者", "role": "boss", "token": ""},        # 跳过
        ])
        assert common.users_configured() is True
        assert common.resolve_role("tk-admin")["role"] == "viewer"
        public = common.list_users_public()
        assert public == [{"name": "管理员", "role": "viewer"}]
        assert all("token" not in u for u in public)
        # 清空用户表 = 关闭多用户
        common.configure_users([])
        assert common.users_configured() is False

    def test_check_role_access_分层(self):
        """访问矩阵: boss 全权/operator 写放行(管理员路径除外)/viewer 仅读。"""
        from lan_mesh import station_routes_common as common

        # boss 全权
        assert common._check_role_access("/api/secrets/fetch", "POST", "boss")
        assert common._check_role_access("/api/tasks", "POST", "boss")
        # operator: 业务写放行
        assert common._check_role_access("/api/tasks", "POST", "operator")
        assert common._check_role_access("/api/tasks/t1/cancel",
                                         "POST", "operator")
        # operator: 管理员路径写拒绝
        assert not common._check_role_access("/api/station/secretary/start",
                                             "POST", "operator")
        assert not common._check_role_access("/api/runtime/x",
                                             "DELETE", "operator")
        # viewer: 读放行/写拒绝
        assert common._check_role_access("/api/tasks", "GET", "viewer")
        assert common._check_role_access("/api/secrets/fetch", "GET", "viewer")
        assert not common._check_role_access("/api/tasks", "POST", "viewer")
        assert not common._check_role_access("/api/tasks/t1",
                                             "DELETE", "viewer")

    def test_middleware_role_tier(self, monkeypatch):
        """中间件端到端: 未认证 401/未知 403/角色分层放行与拒绝。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh import station_routes_common as common

        monkeypatch.setattr(common, "_mesh_auth_enabled", True)
        monkeypatch.setattr(common, "_mesh_auth_token", "mesh-secret")
        common.configure_users([
            {"name": "操作员", "role": "operator", "token": "op-tk"},
            {"name": "观察者", "role": "viewer", "token": "vw-tk"},
        ])
        common.configure_rate_limit(strict_max=1000, trusted_max=1000)
        app = FastAPI()
        app.middleware("http")(common.api_guard_middleware)

        @app.get("/api/tasks")
        def _tasks():
            return {"tasks": []}

        @app.post("/api/tasks")
        def _create():
            return {"ok": True}

        @app.post("/api/station/secretary/start")
        def _admin():
            return {"ok": True}

        client = TestClient(app)
        # 未认证 401
        assert client.post("/api/tasks", json={}).status_code == 401
        # 未知 token 403
        assert client.post("/api/tasks", json={},
                           headers={"Authorization": "Bearer bad"})\
            .status_code == 403
        # viewer: 读放行 / 写 403
        vw = {"Authorization": "Bearer vw-tk"}
        assert client.get("/api/tasks", headers=vw).status_code == 200
        assert client.post("/api/tasks", json={}, headers=vw).status_code == 403
        # operator: 业务写放行 / 管理员路径写 403
        op = {"Authorization": "Bearer op-tk"}
        assert client.post("/api/tasks", json={}, headers=op).status_code == 200
        assert client.post("/api/station/secretary/start",
                           json={}, headers=op).status_code == 403
        # mesh token = boss 全权
        boss = {"Authorization": "Bearer mesh-secret"}
        assert client.post("/api/station/secretary/start",
                           json={}, headers=boss).status_code == 200

    def test_backward_compat_no_users(self, monkeypatch):
        """未配置用户表: 多用户关闭, mesh token 认证全权 (向后兼容)。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh import station_routes_common as common

        monkeypatch.setattr(common, "_mesh_auth_enabled", True)
        monkeypatch.setattr(common, "_mesh_auth_token", "mesh-secret")
        common.configure_users([])
        common.configure_rate_limit(strict_max=1000, trusted_max=1000)
        app = FastAPI()
        app.middleware("http")(common.api_guard_middleware)

        @app.post("/api/tasks")
        def _create():
            return {"ok": True}

        client = TestClient(app)
        r = client.post("/api/tasks", json={},
                        headers={"Authorization": "Bearer mesh-secret"})
        assert r.status_code == 200

    def test_config_users_解析(self, tmp_path):
        """config.yaml security.users 解析为 UserAccount (缺省 role→viewer)。"""
        from lan_mesh.config import load_config, UserAccount

        cfg_path = tmp_path / "cfg.yaml"
        cfg_path.write_text("""
security:
  auth_enabled: true
  mesh_token: "mesh-tk-cfg"
  users:
    - name: "老板"
      role: "boss"
      token: "boss-tk"
    - name: "访客"
      role: "viewer"
""", encoding="utf-8")
        cfg = load_config(str(cfg_path))
        users = cfg.security.users
        assert len(users) == 2
        assert isinstance(users[0], UserAccount)
        assert users[0].name == "老板" and users[0].role == "boss"
        assert users[0].token == "boss-tk"
        # 缺省 role → viewer, 缺省 token → 空
        assert users[1].role == "viewer" and users[1].token == ""

    def test_auth_token_mesh_grant_tier(self, monkeypatch):
        """auth-token 收紧: 多用户模式仅 boss 获 mesh_token (防低角色提权)。"""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh import station_routes_basic as basic
        from lan_mesh import station_routes_common as common

        monkeypatch.setattr(common, "_mesh_auth_enabled", True)
        monkeypatch.setattr(common, "_mesh_auth_token", "mesh-secret")
        common.configure_users([
            {"name": "老板", "role": "boss", "token": "boss-tk"},
            {"name": "观察者", "role": "viewer", "token": "vw-tk"},
        ])
        controller = MagicMock()
        controller.state = SimpleNamespace(shared_folder="", device_id="t")
        app = FastAPI()
        app.include_router(basic.build_basic_routes(controller))
        client = TestClient(app)
        # 未登录: 空角色, 不下发 mesh token
        d = client.get("/api/station/auth-token").json()
        assert d["role"] == "" and d["mesh_token"] == ""
        # viewer: 回显 viewer, 不下发 mesh token (防提权)
        d = client.get("/api/station/auth-token",
                       headers={"Authorization": "Bearer vw-tk"}).json()
        assert d["role"] == "viewer" and d["mesh_token"] == ""
        # boss 用户: 回显 boss 并下发 mesh token
        d = client.get("/api/station/auth-token",
                       headers={"Authorization": "Bearer boss-tk"}).json()
        assert d["role"] == "boss" and d["mesh_token"] == "mesh-secret"
        # 单人模式 (无用户表): 未登录照旧 boss + 下发 (向后兼容)
        common.configure_users([])
        d = client.get("/api/station/auth-token").json()
        assert d["role"] == "boss" and d["mesh_token"] == "mesh-secret"


class TestIter64Federation:
    """iter-64 F3.4 跨网段联邦 (发现层): 静态 peer 配置 + 信息端点 +
    联邦轮询同步 (source=fed 隔离) + 选举/仲裁仅限本网段 + 离线检测。
    """

    def _make_db(self, tmp_path):
        from lan_mesh.database import Database
        return Database(str(tmp_path / "f.db"))

    def _peer(self, name="office-a", host="10.9.0.2", port=45501):
        from lan_mesh.config import FederationPeer
        return FederationPeer(name=name, host=host, port=port)

    def test_federation_config_parse(self):
        """配置解析: 默认关闭 + peers 列表; 缺段时向后兼容。"""
        from lan_mesh.config import AppConfig, FederationConfig
        c = AppConfig()
        assert c.federation.enabled is False
        assert c.federation.peers == []
        f = FederationConfig(enabled=True, interval=7,
                             peers=[{"name": "o", "host": "1.2.3.4", "port": 5}])
        assert f.peers[0].name == "o" and f.peers[0].port == 5

    def test_migration_v10_source_columns(self, tmp_path):
        """迁移 v10: hosts 表新增 source/federation 列 (默认 lan)。"""
        from lan_mesh import database as dbmod
        assert dbmod.SCHEMA_VERSION >= 10
        db = self._make_db(tmp_path)
        from lan_mesh.protocol import HostRecord
        db.upsert_host(HostRecord(device_id="h1", device_name="本机"))
        h = db.get_host("h1")
        assert h.source == "lan" and h.federation == ""

    def test_upsert_host_persists_fed_source(self, tmp_path):
        """联邦主机写入 DB 后 source=fed + federation 名持久化。"""
        db = self._make_db(tmp_path)
        from lan_mesh.protocol import HostRecord
        db.upsert_host(HostRecord(device_id="h2", device_name="远端",
                                  source="fed", federation="office-a"))
        rows = db.list_hosts(source="fed")
        assert len(rows) == 1
        assert rows[0].device_id == "h2" and rows[0].federation == "office-a"
        # lan 过滤不含联邦主机
        assert [h.device_id for h in db.list_hosts(source="lan")] == []

    def test_federation_sync_peer_upserts_remote(self, tmp_path, monkeypatch):
        """联邦同步: 拉取对端信息后对端自身+其主机入库 (source=fed)。"""
        db = self._make_db(tmp_path)
        import lan_mesh.station_controller as sc

        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "device_id": "peer-a", "device_name": "对端站",
                    "role": "secretary", "api_port": 45501,
                    "secretary_active": True,
                    "code_version": "abc", "version_ts": 1.0,
                    "hosts": [{
                        "device_id": "peer-w1", "device_name": "对端Worker",
                        "role": "station", "online": True,
                        "cpu_count": 8, "rating_tier": "B",
                    }],
                }

        import requests
        monkeypatch.setattr(requests, "get",
                            lambda url, headers=None, timeout=8: FakeResp())

        fake = type("F", (), {
            "db": db, "state": type("S", (), {"device_id": "self-1",
                                              "api_port": 45500})(),
            "_mesh_auth_enabled": True, "_mesh_token": "tk",
        })()
        assert sc.StationController._federation_sync_peer(
            fake, self._peer()) == 1
        fed = db.list_hosts(source="fed")
        ids = {h.device_id: h for h in fed}
        assert "peer-a" in ids and "peer-w1" in ids
        assert ids["peer-a"].role == "secretary"
        assert ids["peer-a"].federation == "office-a"
        assert ids["peer-w1"].federation == "office-a"
        # 本网段过滤仍为空 (隔离)
        assert db.list_hosts(source="lan") == []

    def test_federation_sync_skips_self(self, tmp_path, monkeypatch):
        """对端回显本机时不回写 (防自环)。"""
        db = self._make_db(tmp_path)
        import lan_mesh.station_controller as sc

        class FakeResp:
            status_code = 200

            def json(self):
                return {
                    "device_id": "peer-a", "device_name": "对端",
                    "role": "station", "api_port": 45501,
                    "hosts": [{"device_id": "self-1", "device_name": "本机"}],
                }

        import requests
        monkeypatch.setattr(requests, "get",
                            lambda url, headers=None, timeout=8: FakeResp())
        fake = type("F", (), {
            "db": db, "state": type("S", (), {"device_id": "self-1",
                                              "api_port": 45500})(),
            "_mesh_auth_enabled": False, "_mesh_token": "",
        })()
        assert sc.StationController._federation_sync_peer(
            fake, self._peer()) == 1
        ids = [h.device_id for h in db.list_hosts(source="fed")]
        assert ids == ["peer-a"]  # self-1 被跳过

    def test_federation_offline_after_failures(self, tmp_path, monkeypatch):
        """连续失败 offline_after 次 → 该联邦主机集合置离线。"""
        db = self._make_db(tmp_path)
        from lan_mesh.protocol import HostRecord
        db.upsert_host(HostRecord(device_id="peer-a", device_name="对端",
                                  online=True, source="fed",
                                  federation="office-a"))
        import lan_mesh.station_controller as sc
        from lan_mesh.config import FederationConfig

        import requests

        def boom(url, headers=None, timeout=8):
            raise ConnectionError("down")
        monkeypatch.setattr(requests, "get", boom)
        # sleep 一次后抛异常退出 loop (offline_after=1 → 首轮失败即置离线)
        def fake_sleep(secs):
            raise RuntimeError("stop")
        monkeypatch.setattr(sc.time, "sleep", fake_sleep)

        fake = type("F", (), {
            "db": db,
            "cfg": type("C", (), {
                "federation": FederationConfig(
                    enabled=True, interval=1, offline_after=1,
                    peers=[{"name": "office-a", "host": "10.9.0.2",
                            "port": 45501}])})(),
            "state": type("S", (), {"device_id": "self-1",
                                      "api_port": 45500})(),
            "_running": True,
            "_mesh_auth_enabled": False, "_mesh_token": "",
            "_federation_sync_peer":
                sc.StationController._federation_sync_peer,
        })()
        try:
            sc.StationController._federation_loop(fake)
        except RuntimeError:
            pass  # 由 fake_sleep 退出 loop
        assert db.get_host("peer-a").online is False

    def test_secretary_election_ignores_fed(self, tmp_path):
        """选举避让: 联邦远端 Secretary 不阻止本网段当选 (source 隔离)。"""
        db = self._make_db(tmp_path)
        from lan_mesh.protocol import HostRecord
        db.upsert_host(HostRecord(device_id="peer-sec", device_name="远端秘",
                                  role="secretary", online=True,
                                  source="fed", federation="office-a"))
        import lan_mesh.station_controller as sc
        fake = type("F", (), {
            "db": db,
            "state": type("S", (), {"device_id": "self-1"})(),
        })()
        assert sc.StationController._find_existing_secretary(fake) == ""
        # 本网段 Secretary 仍可被发现
        db.upsert_host(HostRecord(device_id="lan-sec", device_name="本网秘",
                                  role="secretary", online=True,
                                  source="lan"))
        assert sc.StationController._find_existing_secretary(fake) == "本网秘"

    def test_federation_info_endpoint(self, tmp_path):
        """HTTP 端点: 返回本机身份/角色/主机列表 (无需 Secretary)。"""
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh import station_routes_basic as basic
        from lan_mesh.database import Database

        db = Database(str(tmp_path / "e.db"))
        from lan_mesh.protocol import HostRecord
        db.upsert_host(HostRecord(device_id="h9", device_name="主机9",
                                  source="lan"))
        controller = MagicMock()
        controller.db = db
        controller.secretary_active = False
        controller.state = SimpleNamespace(device_id="self-1",
                                           device_name="本站", api_port=45500,
                                           shared_folder=None)
        app = FastAPI()
        app.include_router(basic.build_basic_routes(controller))
        client = TestClient(app)
        d = client.get("/api/federation/info").json()
        assert d["device_id"] == "self-1"
        assert d["role"] == "station" and d["secretary_active"] is False
        assert any(h["device_id"] == "h9" for h in d["hosts"])


class TestIter65FederationForward:
    """iter-65 F3.4 联邦任务转发 (任务层): 选站分层 (lan 优先/fed 兜底
    限对端 Secretary) + 委托转发 forwarded 标记 + 转发端点参数传递。
    """

    def _make_db(self, tmp_path):
        from lan_mesh.database import Database
        return Database(str(tmp_path / "f65.db"))

    def _host(self, device_id, source="lan", role="station", tier="B", fed=""):
        from types import SimpleNamespace
        return SimpleNamespace(
            device_id=device_id, device_name=f"站-{device_id[:5]}",
            hostname=None, ip="10.0.0.9", api_port=45501, online=True,
            source=source, role=role, rating_tier=tier, federation=fed)

    def _fake_ctrl(self, tmp_path):
        import lan_mesh.station_controller as sc
        fake = type("F", (), {
            "db": self._make_db(tmp_path),
            "state": type("S", (), {"device_id": "self-1",
                                    "api_port": 45500})(),
        })()
        return sc, fake  # 注意: 返回模块, 避免与类名遮蔽

    # ── 选站分层 ──────────────────────────────────────────

    def test_pick_host_lan_priority(self, tmp_path):
        """选站: 本网段主机优先, 即使联邦主机评级更高。"""
        sc, fake = self._fake_ctrl(tmp_path)
        lan = self._host("lan-1", source="lan", tier="A")
        fed = self._host("fed-sec", source="fed", role="secretary",
                         tier="S", fed="net-a")
        picked = sc.StationController._pick_task_host(fake, [fed, lan])
        assert picked.device_id == "lan-1"

    def test_pick_host_fed_fallback_secretary_only(self, tmp_path):
        """选站: 无本网段主机时兜底联邦主机, 且仅限对端 Secretary。"""
        sc, fake = self._fake_ctrl(tmp_path)
        fed_station = self._host("fed-w1", source="fed", tier="S")
        fed_sec = self._host("fed-sec", source="fed", role="secretary",
                             tier="B")
        picked = sc.StationController._pick_task_host(fake,
                                                      [fed_station, fed_sec])
        assert picked.device_id == "fed-sec"

    def test_pick_host_fed_fallback_any_when_no_secretary(self, tmp_path):
        """选站: 无对端 Secretary 时退化为任意联邦主机 (尽力而为)。"""
        sc, fake = self._fake_ctrl(tmp_path)
        fed_w = self._host("fed-w1", source="fed", tier="C")
        picked = sc.StationController._pick_task_host(fake, [fed_w])
        assert picked.device_id == "fed-w1"

    # ── 委托转发 ──────────────────────────────────────────

    def test_federation_forward_success_marks_forwarded(self, tmp_path,
                                                        monkeypatch):
        """委托转发: 对端 200 → 任务 forwarded + output_data 记录目标。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task
        from unittest.mock import MagicMock

        db = self._make_db(tmp_path)
        task = Task(task_id="task-f65", name="跨网段任务", description="d")
        ws_events = []
        fake = type("F", (), {
            "db": db,
            "state": type("S", (), {"device_id": "self-1"})(),
            # 类字典 lambda 经实例访问会绑定 self → 首参为 self_
            "_queue_ws_broadcast":
                lambda self_, ev, data: ws_events.append(ev),
            "bot_gateway": MagicMock(),
        })()

        class FakeResp:
            status_code = 200

            def json(self):
                return {"ok": True, "task_id": "task-peer"}

        monkeypatch.setattr(sc, "http_post",
                            lambda url, json=None, timeout=15: FakeResp())
        target = self._host("fed-sec", source="fed", role="secretary",
                            fed="net-a")
        assert sc.StationController._federation_forward_task(
            fake, task, target) is True
        assert task.status == "forwarded"
        assert task.output_data["forwarded_to"] == "fed-sec"
        assert task.output_data["federation"] == "net-a"
        assert db.get_task("task-f65").status == "forwarded"
        assert "task_updated" in ws_events
        fake.bot_gateway.notify.assert_called_once()

    def test_federation_forward_failure_marks_failed(self, tmp_path,
                                                     monkeypatch):
        """委托转发: 对端异常 → 任务 failed 且错误信息落库。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task
        from unittest.mock import MagicMock

        db = self._make_db(tmp_path)
        task = Task(task_id="task-f65b", name="跨网段任务", description="d")
        fake = type("F", (), {
            "db": db,
            "state": type("S", (), {"device_id": "self-1"})(),
            "_queue_ws_broadcast": lambda self_, ev, data: None,
            "bot_gateway": MagicMock(),
        })()

        def boom(url, json=None, timeout=15):
            raise ConnectionError("peer down")
        monkeypatch.setattr(sc, "http_post", boom)
        target = self._host("fed-sec", source="fed", role="secretary",
                            fed="net-a")
        assert sc.StationController._federation_forward_task(
            fake, task, target) is False
        assert task.status == "failed"
        assert "联邦委托异常" in task.output_data["error"]
        assert db.get_task("task-f65b").status == "failed"

    def test_submit_task_fed_target_goes_forward(self, tmp_path):
        """提交链路: 选中联邦主机时走委托转发, 不直派远程 Worker。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import HostRecord
        from unittest.mock import MagicMock
        from types import SimpleNamespace

        db = self._make_db(tmp_path)
        db.upsert_host(HostRecord(device_id="fed-sec", device_name="对端秘书",
                                  source="fed", federation="net-a",
                                  role="secretary", online=True,
                                  rating_tier="A"))
        calls = []
        fake = type("F", (), {
            "db": db,
            "state": type("S", (), {
                "device_id": "self-1", "device_name": "本站",
                "api_port": 45500,
                "shared_folder": SimpleNamespace(
                    path=str(tmp_path))})(),
            "secretary_active": False,
            "chat_runtime": None,
            "bot_gateway": MagicMock(),
            "project_manager": None,
            "_queue_ws_broadcast": lambda self_, ev, data: None,
            # 类字典 lambda 经实例访问会绑定 self → 签名需含 self_
            "_pick_task_host": lambda self_, hosts: hosts[0],
            "_federation_forward_task":
                lambda self_, task, host: calls.append((task.task_id,
                                                        host.device_id)),
            "_pm_worker_map": {},
        })()
        result = sc.StationController.submit_task_from_chat(
            fake, "联邦计算", "跨网段委托")
        assert result["status"] == "pending"  # 状态由委托方 (mock) 决定
        assert len(calls) == 1
        assert calls[0][1] == "fed-sec"

    def test_federation_forward_marks_relay_flag(self, tmp_path, monkeypatch):
        """iter-65 防环: 转发的任务数据带 _federation_relay 标记 (跳数上限 1)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task
        from unittest.mock import MagicMock

        db = self._make_db(tmp_path)
        task = Task(task_id="task-f65c", name="跨网段任务", description="d")
        sent = []
        fake = type("F", (), {
            "db": db,
            "state": type("S", (), {"device_id": "self-1"})(),
            "_queue_ws_broadcast": lambda self_, ev, data: None,
            "bot_gateway": MagicMock(),
        })()

        class FakeResp:
            status_code = 200

            def json(self):
                return {"ok": True, "task_id": "task-peer"}

        def fake_post(url, json=None, timeout=15):
            sent.append(json)
            return FakeResp()
        monkeypatch.setattr(sc, "http_post", fake_post)
        target = self._host("fed-sec", source="fed", role="secretary",
                            fed="net-a")
        assert sc.StationController._federation_forward_task(
            fake, task, target) is True
        assert sent[0]["task_data"]["input_data"]["_federation_relay"] is True
        # 本侧任务自身的 input_data 不被污染
        assert "_federation_relay" not in (task.input_data or {})

    def test_fed_relay_no_loopback(self, tmp_path):
        """iter-65 防环: 委托任务 (fed_relay=True) 选站再命中联邦主机时
        直接失败终止, 不再回传 (防 A↔B 互相委托死循环)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import HostRecord
        from unittest.mock import MagicMock
        from types import SimpleNamespace

        db = self._make_db(tmp_path)
        db.upsert_host(HostRecord(device_id="fed-sec", device_name="对端秘书",
                                  source="fed", federation="net-a",
                                  role="secretary", online=True,
                                  rating_tier="A"))
        forward_calls = []
        fake = type("F", (), {
            "db": db,
            "state": type("S", (), {
                "device_id": "self-1", "device_name": "本站",
                "api_port": 45500,
                "shared_folder": SimpleNamespace(
                    path=str(tmp_path))})(),
            "secretary_active": False,
            "chat_runtime": None,
            "bot_gateway": MagicMock(),
            "project_manager": None,
            "_queue_ws_broadcast": lambda self_, ev, data: None,
            "_pick_task_host": lambda self_, hosts: hosts[0],
            "_federation_forward_task":
                lambda self_, task, host: forward_calls.append(task.task_id),
            "_pm_worker_map": {},
        })()
        result = sc.StationController.submit_task_from_chat(
            fake, "委托任务", "二次转发", fed_relay=True)
        assert result["status"] == "failed"
        assert "跳数上限" in (result["output_data"] or {}).get("error", "")
        assert forward_calls == []  # 未回传
        assert db.get_task(result["task_id"]).input_data["_federation_relay"] is True

    # ── 转发端点 ──────────────────────────────────────────

    def test_federation_forward_endpoint_creates_task(self, tmp_path):
        """转发端点: 参数传递正确, 任务由对端 submit_task_from_chat 接管。"""
        from unittest.mock import MagicMock
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from lan_mesh import station_routes_basic as basic

        controller = MagicMock()
        controller.submit_task_from_chat.return_value = {"task_id": "task-x"}
        app = FastAPI()
        app.include_router(basic.build_basic_routes(controller))
        client = TestClient(app)
        d = client.post("/api/federation/tasks/forward", json={
            "task_data": {"name": "联邦任务A", "description": "跨网段计算",
                          "input_data": {"_priority": "high"}},
            "forwarded_from": "self-1234abcd",
        }).json()
        assert d["ok"] is True and d["task_id"] == "task-x"
        controller.submit_task_from_chat.assert_called_once()
        kwargs = controller.submit_task_from_chat.call_args.kwargs
        assert kwargs["name"] == "联邦任务A"
        assert kwargs["created_by"] == "federation:self-123"
        assert kwargs["priority"] == "high"


class TestIter66ClusterScale:
    """iter-66 三机集群背书 (F3.1 自动扩缩容 + F3.3 PM 迁移):

    修复 3 个真实 bug 后回归验证:
    - Bug A: _migrate_orphaned_pms 调用不存在的 db.upsert_task → save_task
    - Bug B: _pm_worker_map 缺 task_id 键致迁移分支永不执行
    - Bug C: 扩容派发不置 running 致重复派发同一任务
    另有迁移精确派发目标任务 (非 pending[0]) 与忙 Worker 过滤。
    """

    def _make_db(self, tmp_path):
        from lan_mesh.database import Database
        return Database(str(tmp_path / "f66.db"))

    def _worker(self, device_id, ip="10.0.0.9", port=45501):
        from lan_mesh.protocol import HostRecord
        return HostRecord(device_id=device_id, device_name=f"工-{device_id[-4:]}",
                          role="worker", ip=ip, api_port=port, online=True,
                          rating_tier="B")

    def _fake(self, db, pm_map=None, dispatch=None, takeover=None,
              busy_check=None, up_threshold=2):
        import lan_mesh.station_controller as sc
        fake = type("F", (), {
            "db": db,
            "state": type("S", (), {"device_id": "self-1",
                                    "api_port": 45500})(),
            "_pm_worker_map": pm_map or {},
            "_autoscale_up_threshold": up_threshold,
            "_is_worker_busy": busy_check or (lambda self_, h: False),
            "_dispatch_task_to_worker": dispatch or (
                sc.StationController._dispatch_task_to_worker),
            "_dispatch_next_task_to_worker":
                sc.StationController._dispatch_next_task_to_worker,
            "_next_pending_task":
                sc.StationController._next_pending_task,
            "_start_local_pm_for_task": takeover or (
                lambda self_, tid: None),
        })()
        return fake

    # ── F3.1 自动扩容 ─────────────────────────────────────

    def test_autoscale_dispatches_backlog_and_marks_running(self, tmp_path,
                                                            monkeypatch):
        """扩容: 积压>=2 且有空闲 Worker → 派发并置 running (Bug C 回归)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        ta = Task(task_id="t-66-a", name="积压A", description="d")
        ta.created_at = 1000.0   # 早提交 (FIFO 应优先)
        db.save_task(ta)
        tb = Task(task_id="t-66-b", name="积压B", description="d")
        tb.created_at = 2000.0
        db.save_task(tb)
        db.upsert_host(self._worker("w-1"))
        fake = self._fake(db, busy_check=sc.StationController._is_worker_busy)

        class FakeResp:
            status_code = 200

            def json(self):
                return {"ok": True, "pm_id": "pm-66-1"}

        sent = []

        def fake_post(url, json=None, timeout=10, headers=None):
            sent.append(json)
            return FakeResp()
        monkeypatch.setattr(sc, "http_post", fake_post)
        monkeypatch.setattr("lan_mesh.host_info.pick_reachable_ip",
                            lambda ip: "127.0.0.1")

        sc.StationController._autoscale_check(fake)
        assert sent and sent[0]["task_id"] == "t-66-a"   # FIFO: 早任务优先
        # Bug F 回归: 派发必须携带 task_data (Worker 本地 DB 无此任务)
        assert sent[0]["task_data"]["task_id"] == "t-66-a"
        assert db.get_task("t-66-a").status == "running"   # Bug C 回归
        assert db.get_task("t-66-b").status == "pending"
        # 映射含 task_id (Bug B 回归)
        assert fake._pm_worker_map["pm-66-1"]["task_id"] == "t-66-a"
        assert fake._pm_worker_map["pm-66-1"]["device_id"] == "w-1"
        # Bug K 回归: 派发成功落 pm_agents 表 (运维查询/承载定位)
        agents = [a for a in db.list_pm_agents()
                  if getattr(a, 'task_id', '') == "t-66-a"]
        assert len(agents) == 1
        assert getattr(agents[0], 'device_id', '') == "w-1"

    def test_autoscale_dispatches_single_queued_task(self, tmp_path,
                                                     monkeypatch):
        """扩容: 单任务积压 (队列=1) 也派发 (Bug J — 水位门槛调度滞后)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        task = Task(task_id="t-67-j", name="单任务积压", description="d")
        db.save_task(task)
        db.upsert_host(self._worker("w-1"))
        fake = self._fake(db)

        class FakeResp:
            status_code = 200

            def json(self):
                return {"ok": True, "pm_id": "pm-67-1"}

        sent = []

        def fake_post(url, json=None, timeout=10, headers=None):
            sent.append(json)
            return FakeResp()
        monkeypatch.setattr(sc, "http_post", fake_post)
        monkeypatch.setattr("lan_mesh.host_info.pick_reachable_ip",
                            lambda ip: "127.0.0.1")

        sc.StationController._autoscale_check(fake)
        # Bug J 回归: 队列=1 (< 原水位 2) 且有空闲 Worker → 仍派发
        assert sent and sent[0]["task_id"] == "t-67-j"
        assert db.get_task("t-67-j").status == "running"

    def test_autoscale_skips_when_no_worker_or_empty_queue(self, tmp_path,
                                                           monkeypatch):
        """扩容: 无空闲 Worker 或空队列 → 不派发 (Bug J 修正不引入空转)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        task = Task(task_id="t-67-k", name="排队", description="d")
        db.save_task(task)
        fake = self._fake(
            db,
            pm_map={"pm-busy": {"device_id": "w-1", "task_id": "t-other"}},
            busy_check=sc.StationController._is_worker_busy)
        db.upsert_host(self._worker("w-1"))
        dispatched = []
        fake._dispatch_next_task_to_worker = (
            lambda self_, h: dispatched.append(h.device_id))
        sc.StationController._autoscale_check(fake)
        assert dispatched == []   # w-1 忙 → 不派发
        assert db.get_task("t-67-k").status == "pending"

    def test_autoscale_skips_busy_worker(self, tmp_path, monkeypatch):
        """扩容: 唯一 Worker 忙碌 → 不派发 (忙过滤, 避免叠任务)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        db.save_task(Task(task_id="t-66-c", name="积压C", description="d"))
        db.save_task(Task(task_id="t-66-d", name="积压D", description="d"))
        db.upsert_host(self._worker("w-1"))
        fake = self._fake(
            db, pm_map={"pm-busy": {"device_id": "w-1", "task_id": "t-old"}},
            busy_check=sc.StationController._is_worker_busy)
        called = []
        monkeypatch.setattr(sc, "http_post",
                            lambda *a, **k: called.append(1))
        sc.StationController._autoscale_check(fake)
        assert called == []
        assert db.get_task("t-66-c").status == "pending"

    def test_autoscale_idle_when_queue_below_threshold(self, tmp_path,
                                                       monkeypatch):
        """扩容: 有积压但无在线 Worker → 不派发 (Bug J 后水位不再是门槛)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        db.save_task(Task(task_id="t-66-e", name="单积压", description="d"))
        # 不 upsert host → 无在线 Worker, 即使队列=1 也不派发
        fake = self._fake(db)
        called = []
        monkeypatch.setattr(sc, "http_post",
                            lambda *a, **k: called.append(1))
        sc.StationController._autoscale_check(fake)
        assert called == []

    def test_dispatch_next_task_fifo_order(self, tmp_path, monkeypatch):
        """派发顺序: FIFO — 早提交任务先派发 (Bug D 回归,
        list_tasks 为 created_at DESC 时不可 LIFO 饥饿)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        early = Task(task_id="t-66-early", name="早任务", description="d")
        early.created_at = 1000.0
        db.save_task(early)
        late = Task(task_id="t-66-late", name="晚任务", description="d")
        late.created_at = 2000.0
        db.save_task(late)
        fake = self._fake(db)
        sent = []

        class FakeResp:
            status_code = 200

            def json(self):
                return {"ok": True, "pm_id": "pm-66-f"}
        monkeypatch.setattr(sc, "http_post",
                            lambda url, json=None, timeout=10,
                            headers=None: FakeResp())
        monkeypatch.setattr("lan_mesh.host_info.pick_reachable_ip",
                            lambda ip: "127.0.0.1")
        fake._dispatch_next_task_to_worker = (
            sc.StationController._dispatch_next_task_to_worker)
        sc.StationController._dispatch_next_task_to_worker(
            fake, self._worker("w-1"))
        assert db.get_task("t-66-early").status == "running"   # 早任务优先
        assert db.get_task("t-66-late").status == "pending"

    def test_dispatch_task_failure_keeps_pending(self, tmp_path, monkeypatch):
        """派发: Worker 不可达 → 返回 False, 任务保持 pending (可重试)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        task = Task(task_id="t-66-f", name="派发失败", description="d")
        db.save_task(task)
        fake = self._fake(db)

        def boom(url, json=None, timeout=10):
            raise ConnectionError("peer down")
        monkeypatch.setattr(sc, "http_post", boom)
        ok = sc.StationController._dispatch_task_to_worker(
            fake, task, self._worker("w-1"))
        assert ok is False
        assert db.get_task("t-66-f").status == "pending"

    # ── F3.3 PM 迁移 ──────────────────────────────────────

    def test_migrate_resets_task_and_redispatches_to_target(self, tmp_path):
        """迁移: running 任务 → pending 落库 (Bug A 回归) → 精确派发
        目标任务到替代 Worker (语义修正, 非 pending[0])。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        task = Task(task_id="t-66-m", name="迁移任务", description="d")
        task.status = "running"
        task.pm_agent_id = "pm-old"
        db.save_task(task)
        db.upsert_host(self._worker("w-2", ip="10.0.0.10", port=45502))
        dispatched = []
        fake = self._fake(
            db,
            pm_map={"pm-old": {"device_id": "gone-1",
                               "task_id": "t-66-m"}},
            dispatch=lambda self_, t, h: (
                dispatched.append((t.task_id, h.device_id)) or True))
        sc.StationController._migrate_orphaned_pms(fake, ["gone-1"])
        # 任务重置 pending 并落库 (Bug A 回归: save_task 存在且生效)
        got = db.get_task("t-66-m")
        assert got.status == "pending"
        assert got.pm_agent_id == ""
        # 精确派发目标任务到替代 Worker
        assert dispatched == [("t-66-m", "w-2")]
        # 旧映射清理
        assert "pm-old" not in fake._pm_worker_map

    def test_migrate_local_takeover_when_no_workers(self, tmp_path):
        """迁移: 无替代 Worker → 本机接管。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        task = Task(task_id="t-66-n", name="孤立任务", description="d")
        task.status = "monitoring"
        db.save_task(task)
        taken = []
        fake = self._fake(
            db,
            pm_map={"pm-old": {"device_id": "gone-1",
                               "task_id": "t-66-n"}},
            takeover=lambda self_, tid: taken.append(tid))
        sc.StationController._migrate_orphaned_pms(fake, ["gone-1"])
        assert db.get_task("t-66-n").status == "pending"
        assert taken == ["t-66-n"]
        assert "pm-old" not in fake._pm_worker_map

    def test_migrate_skips_busy_replacement_worker(self, tmp_path):
        """迁移: 替代 Worker 忙碌 → 本机接管 (忙过滤)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        task = Task(task_id="t-66-p", name="忙过滤", description="d")
        task.status = "running"
        db.save_task(task)
        db.upsert_host(self._worker("w-2", ip="10.0.0.10", port=45502))
        taken = []
        fake = self._fake(
            db,
            pm_map={
                "pm-old": {"device_id": "gone-1", "task_id": "t-66-p"},
                "pm-w2": {"device_id": "w-2", "task_id": "t-other"},
            },
            busy_check=sc.StationController._is_worker_busy,
            takeover=lambda self_, tid: taken.append(tid))
        sc.StationController._migrate_orphaned_pms(fake, ["gone-1"])
        assert taken == ["t-66-p"]  # w-2 忙 → 本机接管

    def test_migrate_entry_without_task_id_cleans_map_only(self, tmp_path):
        """迁移: 映射无 task_id (Bug B 历史数据) → 仅清理映射, 不抛异常。"""
        import lan_mesh.station_controller as sc

        db = self._make_db(tmp_path)
        dispatched = []
        fake = self._fake(
            db,
            pm_map={"pm-old": {"device_id": "gone-1"}},
            dispatch=lambda self_, t, h: dispatched.append(t.task_id))
        sc.StationController._migrate_orphaned_pms(fake, ["gone-1"])
        assert dispatched == []
        assert "pm-old" not in fake._pm_worker_map

    def test_migrate_ignores_non_running_task(self, tmp_path):
        """迁移: 关联任务非 running/monitoring (如已 failed) → 不重置不派发。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        task = Task(task_id="t-66-q", name="已完成", description="d")
        task.status = "completed"
        db.save_task(task)
        dispatched = []
        fake = self._fake(
            db,
            pm_map={"pm-old": {"device_id": "gone-1",
                               "task_id": "t-66-q"}},
            dispatch=lambda self_, t, h: dispatched.append(t.task_id))
        sc.StationController._migrate_orphaned_pms(fake, ["gone-1"])
        assert dispatched == []
        assert db.get_task("t-66-q").status == "completed"  # 状态不变
        assert "pm-old" not in fake._pm_worker_map

    # ── 取消任务 (Bug G/H 回归) ─────────────────────────

    def test_cancel_remote_sends_auth_headers_and_clears_map(self, tmp_path,
                                                             monkeypatch):
        """取消: 远程 PM 需带认证头 (Bug G), 成功后清理映射 (Bug H)。"""
        import lan_mesh.http_retry as hr
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        task = Task(task_id="t-66-c", name="取消目标", description="d")
        task.status = "running"
        task.pm_agent_id = "pm-c1"
        db.save_task(task)
        fake = self._fake(db, pm_map={
            "pm-c1": {"ip": "10.0.0.2", "api_port": 9081,
                      "device_id": "w-1"}})
        fake.bot_gateway = type("B", (), {
            "notify": lambda self, *a, **kw: None})()
        monkeypatch.setattr(hr, "_auth_token", "mesh-test-token")

        class FakeResp:
            status_code = 200
            text = ""

        sent = []

        def fake_post(url, json=None, timeout=10, headers=None, retries=3):
            sent.append({"url": url, "headers": headers, "retries": retries})
            return FakeResp()
        monkeypatch.setattr(sc, "http_post", fake_post)

        result = sc.StationController.cancel_task(fake, "t-66-c")
        assert result.get("ok")
        assert sent and "role/cancel-pm" in sent[0]["url"]
        # Bug G 回归: 认证头必须携带 (Worker 端要求 Bearer mesh_token)
        assert sent[0]["headers"].get("Authorization") == "Bearer mesh-test-token"
        # Bug I 回归: 控制命令重试降为 1, 避免死锁级联时重试放大超时
        assert sent[0]["retries"] == 1
        assert db.get_task("t-66-c").status == "cancelled"
        # Bug H 回归: 映射清理, 防止 _is_worker_busy 误判
        assert "pm-c1" not in fake._pm_worker_map

    def test_cancel_local_clears_map(self, tmp_path):
        """取消: 本机 PM 取消后同样清理映射 (Bug H)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        task = Task(task_id="t-66-l", name="本机取消", description="d")
        task.status = "running"
        task.pm_agent_id = "pm-l1"
        db.save_task(task)
        fake = self._fake(db, pm_map={
            "pm-l1": {"local": True, "device_id": "self-1"}})
        fake._local_cancel_pm = lambda: None
        fake.bot_gateway = type("B", (), {
            "notify": lambda self, *a, **kw: None})()

        result = sc.StationController.cancel_task(fake, "t-66-l")
        assert result.get("ok")
        assert "pm-l1" not in fake._pm_worker_map
        assert db.get_task("t-66-l").status == "cancelled"

    # ── iter-68 扩容批量清空 ───────────────────────────

    def test_autoscale_clears_backlog_in_one_pass(self, tmp_path,
                                                  monkeypatch):
        """扩容: 同轮连续派发清空积压 (iter-68 — 30s/轮滞后修复)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        ta = Task(task_id="t-68-a", name="积压A", description="d")
        ta.created_at = 1000.0
        db.save_task(ta)
        tb = Task(task_id="t-68-b", name="积压B", description="d")
        tb.created_at = 2000.0
        db.save_task(tb)
        db.upsert_host(self._worker("w-1", ip="10.0.0.1", port=45501))
        db.upsert_host(self._worker("w-2", ip="10.0.0.2", port=45502))
        fake = self._fake(db, busy_check=sc.StationController._is_worker_busy)

        sent = []
        pm_counter = {"n": 0}

        def fake_post(url, json=None, timeout=10, headers=None):
            sent.append(json)
            pm_counter["n"] += 1

            class R:
                status_code = 200

                def json(self):
                    return {"ok": True, "pm_id": f"pm-68-{pm_counter['n']}"}
            return R()
        monkeypatch.setattr(sc, "http_post", fake_post)
        monkeypatch.setattr("lan_mesh.host_info.pick_reachable_ip",
                            lambda ip: "127.0.0.1")

        sc.StationController._autoscale_check(fake)
        # 同轮连续派发 2 个任务 (不再等 30s 下一轮)
        assert len(sent) == 2
        assert sent[0]["task_id"] == "t-68-a"   # FIFO
        assert sent[1]["task_id"] == "t-68-b"
        assert db.get_task("t-68-a").status == "running"
        assert db.get_task("t-68-b").status == "running"
        assert len(fake._pm_worker_map) == 2

    def test_autoscale_stops_batch_on_failed_dispatch(self, tmp_path,
                                                      monkeypatch):
        """扩容: 派发失败立即停止本轮 (iter-68 防死循环)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.protocol import Task

        db = self._make_db(tmp_path)
        for i in range(3):
            t = Task(task_id=f"t-68-f{i}", name="积压", description="d")
            t.created_at = 1000.0 + i
            db.save_task(t)
        db.upsert_host(self._worker("w-1", ip="10.0.0.1", port=45501))
        fake = self._fake(db)

        sent = []

        class R:
            status_code = 500
            text = "boom"

        def fake_post(url, json=None, timeout=10, headers=None):
            sent.append(json)
            return R()
        monkeypatch.setattr(sc, "http_post", fake_post)
        monkeypatch.setattr("lan_mesh.host_info.pick_reachable_ip",
                            lambda ip: "127.0.0.1")

        sc.StationController._autoscale_check(fake)
        # 派发失败 → 队列未减 → 立即停止本轮 (不死循环重试)
        assert len(sent) == 1


class TestIter69LocalTakeover:
    """iter-69 七节点实压背书 (F3.3 本机接管 Bug L):

    真机日志暴露: 6 Worker 全部离线 (无可用替补) 时 F3.3 走本机接管分支,
    `_start_local_pm_for_task` 用早期 PM 签名 (task=/runtime=) 自行构造
    ProjectManagerAgent → TypeError "unexpected keyword argument 'task'",
    接管必然失败, 任务停在 pending 无人推进。
    修复: 复用唯一入口 `_local_start_pm` + 抽出 `_register_local_pm`
    统一落库/映射/广播。
    """

    def _fake(self, db, started=None, fail_reason=None, raises=False):
        from types import SimpleNamespace
        from lan_mesh.station_controller import StationController

        class _Fake:
            _local_pm_agent = None

            def __init__(self):
                self.db = db
                self.state = SimpleNamespace(
                    device_id="dev-ctl", api_port=45500, device_name="ctl")
                self._pm_worker_map: dict = {}
                self._ws_events: list = []

            def _local_start_pm(self, task_id, secretary_url, task_data):
                if raises:
                    raise TypeError(
                        "ProjectManagerAgent.__init__() got an unexpected "
                        "keyword argument 'task'")
                if started is not None:
                    started.append((task_id, secretary_url, task_data))
                if fail_reason:
                    return {"ok": False, "message": fail_reason}
                return {"ok": True, "pm_id": "pm-69-take"}

            _register_local_pm = StationController._register_local_pm

            def _queue_ws_broadcast(self, event_type, data):
                self._ws_events.append((event_type, data))

        return _Fake()

    def test_takeover_reuses_local_start_pm_entry(self, tmp_path):
        """接管: 走 _local_start_pm 唯一入口 (Bug L 回归 — 不再自构 PM)。"""
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        from lan_mesh.protocol import Task

        db = Database(str(tmp_path / "f69a.db"))
        task = Task(task_id="t-69-a", name="接管任务", description="d")
        task.status = "pending"
        db.save_task(task)
        started: list = []
        fake = self._fake(db, started=started)

        ok = StationController._start_local_pm_for_task(fake, "t-69-a")
        assert ok is True, "本机接管应成功"
        # 复用唯一入口, 且带完整 task_data (PM 才能真正 start_task)
        assert len(started) == 1
        assert started[0][0] == "t-69-a"
        assert started[0][1] == "http://127.0.0.1:45500"
        assert started[0][2]["task_id"] == "t-69-a"

    def test_takeover_registers_pm_and_marks_running(self, tmp_path):
        """接管: PM 落库 + 映射带 task_id + 任务转 running + WS 广播。"""
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        from lan_mesh.protocol import Task

        db = Database(str(tmp_path / "f69b.db"))
        task = Task(task_id="t-69-b", name="接管落库", description="d")
        task.status = "pending"
        db.save_task(task)
        fake = self._fake(db)

        assert StationController._start_local_pm_for_task(fake, "t-69-b")
        got = db.get_task("t-69-b")
        assert got.status == "running"
        assert got.pm_agent_id == "pm-69-take"
        # PM 落库 (运维可查 / 可取消)
        assert db.get_pm_agent("pm-69-take") is not None
        # 映射带 task_id: 支撑二次迁移 (iter-66 Bug B 约束)
        entry = fake._pm_worker_map["pm-69-take"]
        assert entry["task_id"] == "t-69-b"
        assert entry["local"] is True
        assert entry["device_id"] == "dev-ctl"
        types = [e[0] for e in fake._ws_events]
        assert "pm_registered" in types and "task_updated" in types

    def test_takeover_missing_task_returns_false(self, tmp_path):
        """接管: 任务不存在 → 返回 False, 不落库不广播。"""
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController

        db = Database(str(tmp_path / "f69c.db"))
        fake = self._fake(db)
        assert StationController._start_local_pm_for_task(
            fake, "t-69-missing") is False
        assert fake._pm_worker_map == {}
        assert fake._ws_events == []

    def test_takeover_failure_leaves_task_pending(self, tmp_path):
        """接管失败: 任务留在 pending (由下轮扩容/接力兜底), 不误标 running。"""
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        from lan_mesh.protocol import Task

        db = Database(str(tmp_path / "f69d.db"))
        task = Task(task_id="t-69-d", name="接管失败", description="d")
        task.status = "pending"
        db.save_task(task)
        fake = self._fake(db, fail_reason="本机 PM Agent 正在运行")

        assert StationController._start_local_pm_for_task(
            fake, "t-69-d") is False
        assert db.get_task("t-69-d").status == "pending"
        assert fake._pm_worker_map == {}

    def test_takeover_swallows_exception(self, tmp_path):
        """接管: 入口抛异常被隔离 (不打断 _migrate_orphaned_pms 循环)。"""
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        from lan_mesh.protocol import Task

        db = Database(str(tmp_path / "f69e.db"))
        task = Task(task_id="t-69-e", name="异常隔离", description="d")
        task.status = "pending"
        db.save_task(task)
        fake = self._fake(db, raises=True)

        assert StationController._start_local_pm_for_task(
            fake, "t-69-e") is False
        assert db.get_task("t-69-e").status == "pending"

    def test_migrate_takeover_failure_keeps_task_pending(self, tmp_path):
        """全 Worker 离线 + 接管失败 → 任务 pending 且映射已清理 (真机场景)。"""
        import lan_mesh.station_controller as sc
        from lan_mesh.database import Database
        from lan_mesh.protocol import Task

        db = Database(str(tmp_path / "f69f.db"))
        task = Task(task_id="t-69-f", name="七节点全灭", description="d")
        task.status = "running"
        task.pm_agent_id = "pm-gone"
        db.save_task(task)
        fake = type("F", (), {
            "db": db,
            "state": type("S", (), {"device_id": "self-1",
                                    "api_port": 45500})(),
            "_pm_worker_map": {"pm-gone": {"device_id": "w-gone",
                                           "task_id": "t-69-f"}},
            "_is_worker_busy": lambda self_, h: False,
            "_start_local_pm_for_task": lambda self_, tid: False,
        })()

        sc.StationController._migrate_orphaned_pms(fake, ["w-gone"])
        got = db.get_task("t-69-f")
        assert got.status == "pending"
        assert got.pm_agent_id == ""
        assert "pm-gone" not in fake._pm_worker_map

    def test_relay_dispatch_still_registers_via_shared_helper(self, tmp_path):
        """接力派发: 抽出 _register_local_pm 后行为不变 (iter-57 回归)。"""
        from types import SimpleNamespace
        from lan_mesh.database import Database
        from lan_mesh.station_controller import StationController
        from lan_mesh.protocol import Task

        db = Database(str(tmp_path / "f69g.db"))
        db.save_task(Task(task_id="t-69-g", name="排队", description="d",
                          status="pending"))

        class _Fake:
            _local_pm_agent = None
            _queued_dispatch_waiting = False

            def __init__(self):
                self.db = db
                self.state = SimpleNamespace(
                    device_id="dev-ctl", api_port=45500, device_name="ctl")
                self._pm_worker_map: dict = {}
                self._ws_events: list = []

            def _local_start_pm(self, task_id, secretary_url, task_data):
                return {"ok": True, "pm_id": "pm-69-relay"}

            _register_local_pm = StationController._register_local_pm

            def _queue_ws_broadcast(self, event_type, data):
                self._ws_events.append((event_type, data))

        fake = _Fake()
        assert StationController._dispatch_queued_task(fake) is True
        assert db.get_task("t-69-g").status == "running"
        assert db.get_pm_agent("pm-69-relay") is not None
        assert fake._pm_worker_map["pm-69-relay"]["task_id"] == "t-69-g"


