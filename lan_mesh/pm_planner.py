"""
PM 规划器 — 任务分析与分解

职责:
1. 加载 multi-agent-architect skill
2. 模板匹配 (F2.4) 或 LLM 规划
3. 多轮任务细化 (F2.3)
4. 简单任务直接执行
5. 任务类型推断 (供外部使用)
"""
import json
import uuid
from pathlib import Path
from typing import Optional

from .logger import get_logger
from .pm_state import PMState

logger = get_logger("pm.planner")


class PMPlanner:
    """规划器: 接收任务 → 调用 LLM 分解子任务 → 输出 plan。"""

    def __init__(self, pm_id: str, runtime, state: PMState, agent):
        """
        Args:
            pm_id: PM Agent ID
            runtime: Worker 的 AgentRuntime 实例
            state: 共享状态
            agent: ProjectManagerAgent 协调器引用 (用于上报/沟通)
        """
        self._pm_id = pm_id
        self._runtime = runtime
        self._state = state
        self._agent = agent
        self._skill_content = ""

    # ── 技能加载 ──────────────────────────────────────────────────

    def load_skill(self) -> str:
        """从本地技能缓存加载 multi-agent-architect skill。"""
        skill_dir = Path.home() / ".lan_mesh" / "skills_cache" / "multi-agent-architect"
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            logger.warning("[%s] multi-agent-architect skill 未找到, 使用内置摘要", self._pm_id[:8])
            self._skill_content = self._builtin_skill_summary()
            return self._skill_content

        try:
            content = skill_md.read_text(encoding="utf-8")
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    content = parts[2].strip()
            self._skill_content = content
            return content
        except Exception:
            self._skill_content = self._builtin_skill_summary()
            return self._skill_content

    @staticmethod
    def _builtin_skill_summary() -> str:
        """内置技能摘要 (技能缓存不可用时的回退)。"""
        return """多智能体协作架构决策框架:
1. 默认单智能体，仅当上下文触顶/工具过载/天然并行性/视角异构时才拆分
2. 拆分判定: 讲背景→合并, 看状态→拆分
3. 成本比值 <1.2 不拆, >1.5 可拆
4. 五种模式: 生成-验证器/编排者-子Agent/Agent Teams/Message Bus/Shared State
5. 简单任务用 single, 中等用 orchestrator, 复杂用 teams"""

    # ── 任务规划 ──────────────────────────────────────────────────

    def analyze_with_skill(self, task: dict) -> dict:
        """用 LLM + multi-agent-architect skill 分析任务。

        F2.4: 优先匹配任务模板, 命中则跳过 LLM 规划 (节省 token + 加速)。

        返回 JSON:
        {
            "complexity": "simple|moderate|complex",
            "pattern": "single|orchestrator|teams|bus|shared_state",
            "team_size": 1,
            "decomposition": [{"name": "...", "skill": "...", "depends_on": []}],
            "reasoning": "..."
        }
        """
        task_desc = task.get("description", task.get("name", ""))

        # F2.4: 模板匹配 (关键词命中 >= 2 时直接使用模板)
        try:
            from .task_templates import match_template, apply_template
            matched = match_template(task_desc)
            if matched and matched.get("match_score", 0) >= 2:
                variables = {
                    "project_path": task.get("input_data", {}).get("project_path", "."),
                    "language": task.get("input_data", {}).get("language", "python"),
                    "data_source": task.get("input_data", {}).get("data_source", ""),
                    "output_format": task.get("input_data", {}).get("output_format", "json"),
                    "doc_type": task.get("input_data", {}).get("doc_type", "API"),
                }
                plan = apply_template(matched, variables)
                logger.info("[%s] 模板命中: %s (score=%d)",
                           self._pm_id[:8], matched.get("name", ""), matched.get("match_score", 0))
                return plan
        except Exception as e:
            logger.debug("[%s] 模板匹配异常, 回退 LLM: %s", self._pm_id[:8], e)

        # LLM 规划
        prompt = f"""你是项目经理 Agent。请分析以下任务并给出架构决策。

## 任务信息
- 名称: {task.get('name', '')}
- 描述: {task_desc}
- 输入数据: {json.dumps(task.get('input_data', {}), ensure_ascii=False)[:500]}

## 决策框架 (multi-agent-architect skill)
{self._skill_content[:8000]}

## 输出要求
请严格输出 JSON 格式 (不要 markdown 代码块):
{{
  "complexity": "simple|moderate|complex",
  "pattern": "single|orchestrator|teams|bus|shared_state",
  "team_size": 1,
  "decomposition": [
    {{"name": "子任务名", "skill": "react_agent|code_generation|code_review|document_summary|shell_exec|file_ops|monitoring", "depends_on": [], "description": "子任务描述"}}
  ],
  "reasoning": "决策理由"
}}

决策规则:
- 默认 single (单 Agent)，除非任务复杂度 >= moderate
- simple → single, team_size=1
- moderate → orchestrator, team_size=2-3
- complex → orchestrator 或 teams, team_size=3-5
- decomposition 中的 depends_on 是子任务名称列表 (前序依赖)
- skill 选择: 需要多步工具操作(读文件+执行命令+写文件) → react_agent; 纯代码生成 → code_generation; 纯审查 → code_review

自主执行原则 (极其重要):
- 你是自主执行者, 不是交互式助手。收到任务后必须立即规划并执行, 禁止反问用户。
- 需求中未明确的细节, 按行业最佳实践自行假设并在 reasoning 中注明。
- 例如: "写一个计算器" → 直接假设 CLI 交互、支持四则运算、含错误处理, 立即执行。
- 只有当任务目标完全无法推断时 (如仅一个字 "做"), 才可标记为需要澄清。
- decomposition 中每个子任务的 description 必须足够具体, 让执行者无需再问。
"""

        resp = self._runtime._call_llm_with_routing(
            prompt,
            {"_model_preference": "", "_fallback_models": [],
             "description": task_desc}
        )
        content = resp.get("content", "")

        # 尝试解析 JSON
        try:
            if "```" in content:
                lines = content.split("\n")
                json_lines = [l for l in lines if not l.strip().startswith("```")]
                content = "\n".join(json_lines)
            plan = json.loads(content.strip())
        except json.JSONDecodeError:
            logger.warning("[%s] LLM 输出 JSON 解析失败, 回退 single 模式", self._pm_id[:8])
            plan = {
                "complexity": "simple",
                "pattern": "single",
                "team_size": 1,
                "decomposition": [{"name": task.get("name", "执行"), "skill": "code_generation", "depends_on": [], "description": task_desc}],
                "reasoning": "JSON 解析失败，回退单体模式",
            }

        return plan

    # ── 简单任务直接执行 ──────────────────────────────────────────

    def execute_directly(self, task: dict) -> dict:
        """PM 自己执行简单任务。"""
        task_desc = task.get("description", task.get("name", ""))
        input_data = dict(task.get("input_data", {}))
        if not input_data.get("requirement") and not input_data.get("description"):
            input_data["requirement"] = task_desc
        subtask = {
            "subtask_id": str(uuid.uuid4()),
            "parent_task_id": task.get("task_id", ""),
            "name": task.get("name", ""),
            "description": task_desc,
            "required_skill": "code_generation",
            "input_data": input_data,
            "model_preference": "",
            "fallback_models": [],
        }
        result = self._runtime.execute(subtask)
        output = result.get("output", {})
        code_content = output.get("code", output.get("summary", "完成"))
        # 检测 LLM 返回的错误标记
        status = result.get("status", "completed")
        if isinstance(code_content, str) and code_content.startswith(("[未配置", "[LLM 调用失败", "[模型调用失败")):
            status = "failed"
        return {
            "summary": code_content if isinstance(code_content, str) else str(code_content),
            "status": status,
        }

    # ── F2.3: 多轮任务细化 ─────────────────────────────────────

    def refine_requirements(self, task: dict) -> dict:
        """F2.3: 多轮对话式任务细化。

        仅当任务描述过短/模糊时触发 (描述 < 20 字且无 input_data)。
        最多 2 轮追问, 将 Boss 回复累积到 task description 中。
        """
        desc = task.get("description", task.get("name", ""))
        input_data = task.get("input_data", {})

        if len(desc) >= 20 or input_data:
            return task

        logger.info("[%s] 任务描述模糊 (%d字), 尝试细化", self._pm_id[:8], len(desc))

        max_rounds = 2
        accumulated_context = []

        for round_num in range(max_rounds):
            question = self._generate_refinement_question(desc, accumulated_context, round_num)
            if not question:
                break

            response = self._agent.request_clarification(question=question, timeout=120.0)

            if not response or response.get("timed_out"):
                logger.info("[%s] 细化轮次%d 未收到回复, 继续执行", self._pm_id[:8], round_num + 1)
                break

            answer = response.get("response", response.get("choice", ""))
            if answer:
                accumulated_context.append(f"Q{round_num+1}: {question}\nA{round_num+1}: {answer}")

        if accumulated_context:
            enriched_desc = desc + "\n\n## 补充信息 (Boss 细化)\n" + "\n".join(accumulated_context)
            task = dict(task)
            task["description"] = enriched_desc
            logger.info("[%s] 任务描述已细化: %d → %d 字",
                       self._pm_id[:8], len(desc), len(enriched_desc))

        return task

    @staticmethod
    def _generate_refinement_question(desc: str, context: list, round_num: int) -> str:
        """F2.3: 生成细化追问。"""
        if round_num == 0:
            return (
                f"任务描述 '{desc}' 比较简短。请补充以下信息:\n"
                "1. 具体要实现什么功能/目标?\n"
                "2. 涉及哪些文件或目录?\n"
                "3. 有无技术约束 (语言/框架/风格)?"
            )
        elif round_num == 1 and context:
            return "还有没有其他重要约束或期望? (如无需补充请回复 '无')"
        return ""

    # ── 任务类型推断 ──────────────────────────────────────────────

    @staticmethod
    def infer_task_type(task_name: str, task_desc: str) -> str:
        """从任务名称和描述推断任务类型。"""
        text = f"{task_name} {task_desc}".lower()
        type_keywords = {
            "code_review": ["代码审查", "code review", "审查", "review"],
            "development": ["开发", "实现", "编写", "develop", "implement", "coding"],
            "research": ["调研", "研究", "分析", "research", "analysis", "investigate"],
            "documentation": ["文档", "说明", "documentation", "readme", "doc"],
            "testing": ["测试", "test", "验证", "verify"],
            "refactoring": ["重构", "refactor", "优化", "optimize"],
            "deployment": ["部署", "deploy", "发布", "release"],
            "data_processing": ["数据", "data", "处理", "process", "etl"],
        }
        for task_type, kws in type_keywords.items():
            if any(kw in text for kw in kws):
                return task_type
        return "general"
