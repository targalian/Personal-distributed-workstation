"""
项目经理 Agent (PM Agent) — 任务驱动的管理型 Agent

职责:
1. 接收任务，使用 multi-agent-architect skill 分析任务复杂度
2. 决定团队架构（单体 / 编排者 / Teams / Bus / SharedState）
3. 在合适的 work_station 上创建子 Agent 或团队
4. 分解任务为任务列表，梳理依赖关系
5. 向子 Agent 布置任务，要求阶段 / 定期反馈进度
6. 收集进度，上报 Secretary
7. 简单任务可自行完成

运行模型:
  Worker 进程内嵌模块，由 Secretary 通过 HTTP 指令激活。
  复用 Worker 的 AgentRuntime 进行 LLM 调用，
  通过 Secretary API 获取 fleet 信息和上报状态。
"""
import json
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

import requests

from .agent_prompt import build_subagent_prompt, build_dispatch_context, build_aggregation_prompt
from .logger import get_logger

logger = get_logger("pm")


class ProjectManagerAgent:
    """项目经理 Agent — 运行在 Worker 进程内。

    由 Secretary 注册到合适的 work_station 上运行，
    使用 multi-agent-architect skill 自主决策团队架构、
    分解任务、创建子 Agent / 团队、管理进度反馈。
    """

    def __init__(self, pm_id: str, agent_runtime, secretary_url: str,
                 device_id: str, device_name: str = ""):
        self.pm_id = pm_id
        self.runtime = agent_runtime          # Worker 的 AgentRuntime 实例
        self.secretary_url = secretary_url.rstrip("/")  # Secretary API 地址
        self.device_id = device_id
        self.device_name = device_name

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._progress_thread: Optional[threading.Thread] = None
        self._skill_content = ""
        self._teams: dict = {}     # team_id → team_info
        self._subagents: dict = {} # agent_id → agent_info

        # ── 优化1: 依赖感知的结果传递 ──
        self._plan: dict = {}              # PM 的架构决策 plan
        self._task: dict = {}              # 原始任务
        self._subtask_outputs: dict = {}   # task_name → 完成后的 output
        self._pending_subtasks: dict = {}  # task_name → {sub, station, agent_info}
        self._dispatched: set = set()      # 已分发的 task_name 集合
        self._task_station: dict = {}      # task_name → station dict
        self._task_agent: dict = {}        # task_name → agent_info dict

        # ── 优化5: 失败接管策略 ──
        self._retry_counts: dict = {}      # task_name → 已重试次数
        self._max_retries: int = 2         # 最大重试次数

        # ── 优化7: 反向沟通通道 (PM → Secretary → Boss) ──
        self._clarification_event = threading.Event()   # 等待 Boss 回复
        self._clarification_response: dict = {}          # Boss 的回复数据
        self._clarification_timeout: float = 600.0       # 等待回复超时 (默认10分钟)

        # ── F1.3: PM Agent 超时保护 ──
        self._global_timeout: float = 3600.0  # 全局任务超时 (默认1小时)
        self._subtask_timeout: float = 1800.0  # 子任务超时 (默认30分钟)
        self._start_time: float = time.time()  # 任务开始时间
        self._subtask_start_times: dict = {}   # task_name → 开始时间

    # ── 生命周期 ──────────────────────────────────────────────────

    def start_task(self, task: dict):
        """接管任务，开始规划与执行 (异步)。"""
        self._running = True

        # 加载 multi-agent-architect skill
        self._skill_content = self._load_skill()

        # 启动工作线程
        self._thread = threading.Thread(
            target=self._run_task, args=(task,), daemon=True, name=f"pm-{self.pm_id[:8]}"
        )
        self._thread.start()

        # 启动进度收集线程
        self._progress_thread = threading.Thread(
            target=self._progress_loop, daemon=True, name=f"pm-progress-{self.pm_id[:8]}"
        )
        self._progress_thread.start()

    def stop(self):
        """停止 PM Agent。"""
        self._running = False

    def get_status(self) -> dict:
        """返回 PM Agent 当前状态。"""
        return {
            "pm_id": self.pm_id,
            "device_id": self.device_id,
            "running": self._running,
            "teams": len(self._teams),
            "subagents": len(self._subagents),
        }

    # ── F1.3: 超时保护 ─────────────────────────────────────────

    def _is_global_timed_out(self) -> bool:
        """F1.3: 检查全局任务是否超时。"""
        return (time.time() - self._start_time) > self._global_timeout

    def _check_subtask_timeouts(self):
        """F1.3: 检测子任务超时, 触发失败接管。"""
        now = time.time()
        timed_out = []
        for task_name, start_t in list(self._subtask_start_times.items()):
            if (now - start_t) > self._subtask_timeout:
                timed_out.append(task_name)

        for task_name in timed_out:
            del self._subtask_start_times[task_name]
            logger.warning("[%s] 子任务 '%s' 超时 (%.0fs), 触发接管",
                          self.pm_id[:8], task_name, self._subtask_timeout)
            # 标记对应 subagent 为 failed
            for agent_id, info in self._subagents.items():
                if info.get("current_task") == task_name and info.get("status") not in ("completed", "failed"):
                    info["status"] = "failed"
                    break
            # 触发失败接管策略
            self._handle_subagent_failure(task_name, f"子任务超时 ({self._subtask_timeout}s)")

    def _record_subtask_start(self, task_name: str):
        """F1.3: 记录子任务开始时间。"""
        self._subtask_start_times[task_name] = time.time()

    # ── 核心工作流 ────────────────────────────────────────────────

    def _run_task(self, task: dict):
        """任务执行主流程 (工作线程)。"""
        self._start_time = time.time()  # F1.3: 重置开始时间
        try:
            # F1.3: 全局超时守卫
            if self._is_global_timed_out():
                raise TimeoutError(f"全局任务超时 ({self._global_timeout}s)")

            # F2.3: 多轮任务细化 (可选, 仅当任务描述模糊时触发)
            task = self._refine_requirements(task)

            # 阶段 1: 规划
            self._report_status("planning")
            plan = self._analyze_with_skill(task)
            logger.info("[%s] 规划完成: 模式=%s, 子任务=%d",
                       self.pm_id[:8], plan.get('pattern', 'single'),
                       len(plan.get('decomposition', [])))

            # 记录 plan/task 供子任务状态同步使用
            self._plan = plan
            self._task = task

            # 阶段 2: 执行
            pattern = plan.get("pattern", "single")
            if pattern == "single":
                # 简单任务，PM 自己做
                self._report_status("executing", collaboration_mode="single")
                # 单体任务: 构造一个子任务节点供前端进度展示
                if not plan.get("decomposition"):
                    plan["decomposition"] = [{
                        "name": task.get("name", "执行"),
                        "skill": "code_generation",
                        "depends_on": [],
                        "description": task.get("description", ""),
                    }]
                self._sync_subtasks()  # 先推送 pending 状态
                result = self._execute_directly(task)
                # 标记子任务完成并同步
                for sub in plan.get("decomposition", []):
                    self._subtask_outputs[sub.get("name", "")] = result
                self._sync_subtasks()
                self._report_status("completed", task_list=[{"name": task.get("name", ""), "status": "completed"}])
                self._report_progress(1.0, "completed", f"任务完成: {result.get('summary', '')}")
            else:
                # 复杂任务，创建团队并分发
                self._report_status("executing", collaboration_mode=pattern,
                                    task_list=plan.get("decomposition", []))
                self._sync_subtasks()  # 推送初始子任务列表 (全部 pending)
                self._create_team_and_dispatch(task, plan)
                self._report_status("monitoring")

        except Exception as e:
            logger.error("[%s] 任务执行失败: %s", self.pm_id[:8], e)
            self._report_status("failed")
            self._report_progress(0.0, "failed", str(e))

        finally:
            self._running = False

    def _analyze_with_skill(self, task: dict) -> dict:
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
                           self.pm_id[:8], matched.get("name", ""), matched.get("match_score", 0))
                return plan
        except Exception as e:
            logger.debug("[%s] 模板匹配异常, 回退 LLM: %s", self.pm_id[:8], e)

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

        resp = self.runtime._call_llm_with_routing(
            prompt,
            {"_model_preference": "", "_fallback_models": [],
             "description": task_desc}
        )
        content = resp.get("content", "")

        # 尝试解析 JSON
        try:
            # 去除可能的 markdown 代码块标记
            if "```" in content:
                lines = content.split("\n")
                json_lines = [l for l in lines if not l.strip().startswith("```")]
                content = "\n".join(json_lines)
            plan = json.loads(content.strip())
        except json.JSONDecodeError:
            # 解析失败，回退为 single 模式
            logger.warning("[%s] LLM 输出 JSON 解析失败, 回退 single 模式", self.pm_id[:8])
            plan = {
                "complexity": "simple",
                "pattern": "single",
                "team_size": 1,
                "decomposition": [{"name": task.get("name", "执行"), "skill": "code_generation", "depends_on": [], "description": task_desc}],
                "reasoning": "JSON 解析失败，回退单体模式",
            }

        return plan

    def _execute_directly(self, task: dict) -> dict:
        """PM 自己执行简单任务。"""
        task_desc = task.get("description", task.get("name", ""))
        # 将任务描述注入 input_data, 确保 handler 能获取到需求文本
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
        result = self.runtime.execute(subtask)
        return {
            "summary": result.get("output", {}).get("code", result.get("output", {}).get("summary", "完成"))[:200],
            "status": result.get("status", "completed"),
        }

    def _create_team_and_dispatch(self, task: dict, plan: dict):
        """根据规划创建团队并分发子任务 (依赖感知拓扑排序)。

        策略:
        1. 为所有子任务创建子 Agent (需先存在于 station 上)
        2. 只分发无依赖 (depends_on 为空) 的子任务
        3. 有依赖的子任务暂存为 pending, 等前序完成后自动注入结果并分发
        """
        decomposition = plan.get("decomposition", [])
        pattern = plan.get("pattern", "orchestrator")

        # 存储 plan 和 task 供后续依赖传递使用
        self._plan = plan
        self._task = task

        # 获取可用 work_station 列表
        stations = self._get_available_stations()
        if not stations:
            # 没有可用站点，在本机执行
            logger.info("[%s] 无可用 work_station, 本地执行全部子任务", self.pm_id[:8])
            for sub in decomposition:
                self._execute_subtask_locally(task, sub)
            return

        # 创建团队
        team_id = str(uuid.uuid4())
        team = {
            "team_id": team_id,
            "pm_id": self.pm_id,
            "team_name": f"Team-{team_id[:8]}",
            "team_type": pattern,
            "device_id": self.device_id,
            "parent_team_id": "",
            "members": [],
            "status": "active",
            "current_task": task.get("name", ""),
            "created_at": time.time(),
        }

        # 为每个子任务选择 work_station 并创建子 Agent
        station_idx = 0
        for sub in decomposition:
            station = stations[station_idx % len(stations)]
            station_idx += 1

            # 预生成 agent_id (用于 prompt 中引用)
            agent_id = f"sub-{uuid.uuid4().hex[:10]}"
            agent_name = sub.get("name", "worker")
            skill = sub.get("skill", "code_generation")
            task_desc = sub.get("description", "")
            sub_name = sub.get("name", "")

            # 为此子任务生成定制 system prompt
            system_prompt = self._build_subagent_prompt_for_sub(
                task, sub, plan, agent_id, agent_name
            )

            # 在目标 work_station 创建子 Agent (含定制 prompt)
            agent_info = self._create_subagent_on_station(
                station, agent_name, [skill], task_desc,
                system_prompt=system_prompt,
                preferred_agent_id=agent_id,
            )

            if agent_info:
                member = {
                    "member_id": str(uuid.uuid4()),
                    "team_id": team_id,
                    "agent_id": agent_info.get("agent_id", ""),
                    "agent_name": agent_info.get("agent_name", ""),
                    "device_id": station.get("device_id", ""),
                    "role": "worker",
                    "skills": [sub.get("skill", "")],
                    "current_task": sub_name,
                    "status": "busy" if not sub.get("depends_on") else "waiting",
                    "progress": 0.0,
                }
                team["members"].append(member)
                self._subagents[member["member_id"]] = member

                # 记录 task_name → station / agent_info 映射
                self._task_station[sub_name] = station
                self._task_agent[sub_name] = agent_info

                # 依赖感知分发: 无依赖立即分发, 有依赖暂存
                if not sub.get("depends_on"):
                    self._dispatched.add(sub_name)
                    self._record_subtask_start(sub_name)  # F1.3
                    self._dispatch_subtask(station, agent_info, task, sub, plan=plan)
                else:
                    self._pending_subtasks[sub_name] = {
                        "sub": sub, "station": station, "agent_info": agent_info,
                    }
                    logger.debug("[%s] 子任务 '%s' 等待依赖: %s", self.pm_id[:8], sub_name, sub.get('depends_on'))

        # 上报团队结构到 Secretary
        self._teams[team_id] = team
        self._report_team(team)
        self._report_status("monitoring", team_structure=team)

    def _execute_subtask_locally(self, task: dict, sub: dict):
        """在本地执行子任务 (无可用远程站点时)。"""
        sub_desc = sub.get("description", sub.get("name", ""))
        # 将子任务描述注入 input_data, 确保 handler 能获取到需求文本
        input_data = dict(task.get("input_data", {}))
        if not input_data.get("requirement") and not input_data.get("description"):
            input_data["requirement"] = sub_desc
        subtask = {
            "subtask_id": str(uuid.uuid4()),
            "parent_task_id": task.get("task_id", ""),
            "name": sub.get("name", ""),
            "description": sub_desc,
            "required_skill": sub.get("skill", "code_generation"),
            "input_data": input_data,
            "model_preference": "",
            "fallback_models": [],
        }
        result = self.runtime.execute(subtask)
        status = "completed" if result.get("status") == "completed" else "failed"
        self._report_progress(
            0.5, status,
            f"子任务 {sub.get('name', '')} {status}: {result.get('error', '')[:200]}"
        )

    # ── HTTP 通信 ────────────────────────────────────────────────

    def _build_subagent_prompt_for_sub(
        self, task: dict, sub: dict, plan: dict, agent_id: str, agent_name: str
    ) -> str:
        """为单个子任务生成定制 system prompt。

        调用 agent_prompt.build_subagent_prompt(), 注入:
        - 通用准则 (BASE_SUBAGENT_PROMPT)
        - 角色定位 (按 skill 类型)
        - 团队上下文 (PM/团队名/协作模式/队友)
        - 依赖关系 (前序/后续)
        - 质量要求 + 里程碑
        - 输入数据
        - PM 额外叮嘱
        """
        decomposition = plan.get("decomposition", [])
        sub_name = sub.get("name", "")
        skill = sub.get("skill", "code_generation")

        # 构建队友信息 (排除自己)
        teammates = []
        for d in decomposition:
            if d.get("name") != sub_name:
                teammates.append({
                    "agent_name": d.get("name", ""),
                    "role": d.get("skill", ""),
                    "task": d.get("description", "")[:100],
                })

        # 前序依赖
        dependencies = []
        for dep_name in sub.get("depends_on", []):
            for d in decomposition:
                if d.get("name") == dep_name:
                    dependencies.append({
                        "task_name": dep_name,
                        "agent_name": d.get("name", ""),
                        "output_summary": d.get("description", "")[:100],
                    })
                    break

        # 后续依赖 (谁依赖我的输出)
        dependents = []
        for d in decomposition:
            if sub_name in d.get("depends_on", []):
                dependents.append({
                    "task_name": d.get("name", ""),
                    "agent_name": d.get("name", ""),
                })

        # 里程碑 (从子任务描述推导)
        milestones = []
        desc = sub.get("description", "")
        if desc:
            milestones.append(f"理解任务: {desc[:80]}")
            milestones.append("执行核心工作并产出结果")
            milestones.append("自检输出格式和完整性")
            milestones.append("上报最终结果")

        # PM 额外叮嘱
        extra = ""
        if dependents:
            extra += (
                "你的输出将被下游任务使用，请确保格式规范、内容完整。\n"
                "如果输出是代码，确保可运行；如果是审查结果，确保问题分级清晰。\n"
            )
        if sub.get("depends_on"):
            extra += (
                "你需要等待前序任务的输出。PM 会通过 input_data 提供前序结果，"
                "收到后再开始执行。\n"
            )

        return build_subagent_prompt(
            pm_id=self.pm_id,
            agent_id=agent_id,
            agent_name=agent_name,
            task_name=sub_name,
            task_description=sub.get("description", ""),
            skill=skill,
            team_name=f"Team-{self.pm_id[:8]}",
            collaboration_mode=plan.get("pattern", ""),
            teammates=teammates,
            dependencies=dependencies,
            dependents=dependents,
            milestones=milestones,
            input_data=task.get("input_data", {}),
            extra_instructions=extra,
        )

    def _get_available_stations(self) -> list[dict]:
        """从 Secretary 获取可用 work_station 列表 (含自身)。"""
        try:
            resp = requests.get(
                f"{self.secretary_url}/api/hosts",
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                hosts = data.get("hosts", [])
                # 只返回在线且有 API 端口的站点
                return [h for h in hosts if h.get("online") and h.get("api_port")]
        except Exception as e:
            logger.error("[%s] 获取 work_station 列表失败: %s", self.pm_id[:8], e)
        return []

    def _create_subagent_on_station(self, station: dict, agent_name: str, skills: list,
                                     task_description: str = "",
                                     system_prompt: str = "",
                                     preferred_agent_id: str = "") -> Optional[dict]:
        """在目标 work_station 上创建子 Agent。

        Args:
            station: 目标工作站信息
            agent_name: 子 Agent 名称
            skills: 技能列表
            task_description: 任务描述
            system_prompt: PM 生成的定制 system prompt
            preferred_agent_id: PM 预生成的 agent_id (已在 prompt 中引用)
        """
        ip = station.get("ip", "")
        port = station.get("api_port", 0)
        if not ip or not port:
            return None

        try:
            resp = requests.post(
                f"http://{ip}:{port}/pm/create-subagent",
                json={
                    "agent_name": agent_name,
                    "skills": skills,
                    "task_description": task_description,
                    "system_prompt": system_prompt,
                    "preferred_agent_id": preferred_agent_id,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.error("[%s] 创建子 Agent 失败 (%s:%s): %s", self.pm_id[:8], ip, port, e)
        return None

    def _update_subagent_prompt(self, station: dict, agent_id: str, new_prompt: str) -> bool:
        """动态更新子 Agent 的 system prompt (优化2)。

        PM 可在任务执行中途调用此方法, 通过 Worker 的 /pm/update-prompt 端点
        更新子 Agent 的定制 prompt, 用于纠偏、补充上下文、调整策略。

        Args:
            station: 子 Agent 所在的 work_station
            agent_id: 子 Agent ID
            new_prompt: 新的 system prompt 内容

        Returns:
            True 成功, False 失败
        """
        ip = station.get("ip", "")
        port = station.get("api_port", 0)
        if not ip or not port:
            return False

        try:
            resp = requests.post(
                f"http://{ip}:{port}/pm/update-prompt",
                json={
                    "agent_id": agent_id,
                    "system_prompt": new_prompt,
                },
                timeout=10,
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("ok"):
                    logger.info("[%s] 子 Agent %s prompt 已更新", self.pm_id[:8], agent_id)
                    return True
        except Exception as e:
            logger.error("[%s] 更新子 Agent prompt 失败: %s", self.pm_id[:8], e)
        return False

    def _dispatch_subtask(self, station: dict, agent_info: dict, task: dict, sub: dict,
                          plan: dict = None):
        """向目标 work_station 的子 Agent 分发任务。

        Args:
            station: 目标工作站
            agent_info: 子 Agent 创建返回的信息
            task: 原始任务
            sub: 子任务规划项
            plan: PM 的架构决策 (用于构建团队上下文)
        """
        ip = station.get("ip", "")
        port = station.get("api_port", 0)

        # 合并 input_data + 团队上下文
        input_data = dict(task.get("input_data", {}))
        if plan:
            dispatch_ctx = build_dispatch_context(task, sub, plan, self.pm_id)
            input_data.update(dispatch_ctx)

        try:
            resp = requests.post(
                f"http://{ip}:{port}/tasks/execute",
                json={
                    "subtask_id": str(uuid.uuid4()),
                    "parent_task_id": task.get("task_id", ""),
                    "name": sub.get("name", ""),
                    "description": sub.get("description", ""),
                    "required_skill": sub.get("skill", "code_generation"),
                    "input_data": input_data,
                    "model_preference": "",
                    "fallback_models": [],
                    "pm_id": self.pm_id,
                    "reporter_id": agent_info.get("agent_id", ""),
                },
                timeout=300,
            )
            if resp.status_code == 200:
                result = resp.json()
                status = "completed" if result.get("status") == "completed" else "failed"
                self._report_progress(
                    0.5, status if status == "completed" else "in_progress",
                    f"子任务 {sub.get('name', '')}: {result.get('error', '已分发')[:200]}"
                )
        except Exception as e:
            logger.error("[%s] 分发子任务失败: %s", self.pm_id[:8], e)

    # ── 进度收集 ──────────────────────────────────────────────────

    def _progress_loop(self):
        """定期收集进度并上报 Secretary (优化4: 全部完成后触发聚合)。"""
        _aggregated = False
        while self._running:
            time.sleep(10)  # 每 10 秒收集一次

            # F1.3: 全局超时检测
            if self._is_global_timed_out():
                logger.error("[%s] 全局任务超时 (%.0fs), 强制终止",
                            self.pm_id[:8], self._global_timeout)
                self._report_progress(0.0, "failed", f"全局超时 ({self._global_timeout}s)")
                self._running = False
                break

            # F1.3: 子任务超时检测
            self._check_subtask_timeouts()

            if not self._subagents:
                continue
            # 聚合进度
            total = len(self._subagents)
            completed = sum(1 for m in self._subagents.values() if m.get("status") == "completed")
            failed = sum(1 for m in self._subagents.values() if m.get("status") == "failed")
            overall = completed / total if total > 0 else 0.0
            self._report_progress(overall, "in_progress" if completed < total else "completed",
                                  f"整体进度: {completed}/{total} 子任务完成")

            # 优化4: 全部子任务完成 (含 failed) 且无 pending, 触发结果聚合
            if not _aggregated and not self._pending_subtasks:
                finished = completed + failed
                if finished >= total and total > 0:
                    _aggregated = True
                    self._aggregate_results()

    def receive_progress_report(self, report: dict):
        """接收子 Agent 主动上报的进度 (通过 Worker API 转发)。

        优化1: 当子任务完成时, 自动检查是否有等待依赖的后续任务可以分发。
        优化5: 当子任务失败时, 触发接管策略 (同站重试→换站重试→PM本地接管)。
        """
        reporter_id = report.get("reporter_id", "")
        task_name = report.get("task_name", "")
        status = report.get("status", "in_progress")

        if reporter_id in self._subagents:
            self._subagents[reporter_id]["progress"] = report.get("progress", 0.0)
            self._subagents[reporter_id]["status"] = status
            self._subagents[reporter_id]["current_task"] = task_name

        # 优化1: 任务完成时存储输出并尝试分发依赖链
        if status == "completed" and task_name:
            output = report.get("output", report.get("message", ""))
            self._subtask_outputs[task_name] = output

            # 优化6: 验证自检结果
            self_check = report.get("self_check", {})
            if not self_check:
                logger.warning("[%s] 子任务 '%s' 完成但未附带自检结果", self.pm_id[:8], task_name)
            elif not self_check.get("passed", False):
                notes = self_check.get("notes", "")
                logger.warning("[%s] 子任务 '%s' 自检未通过: %s", self.pm_id[:8], task_name, notes[:200])
            else:
                logger.info("[%s] 子任务 '%s' 完成, 自检通过: %s", self.pm_id[:8], task_name, self_check.get('notes', '')[:100])
                logger.info("[%s] 子任务 '%s' 完成, 输出已存储", self.pm_id[:8], task_name)

            # F2.5: 质量验证 (生成-验证器模式)
            quality = self._verify_output_quality(task_name, output)
            if quality and not quality.get("accepted", True):
                logger.warning("[%s] 子任务 '%s' 质量未达标 (score=%.1f): %s",
                              self.pm_id[:8], task_name, quality.get("score", 0),
                              quality.get("issues", "")[:150])
                # 质量不达标且未超过重试上限 → 触发重做
                retry_count = self._retry_counts.get(task_name, 0)
                if retry_count < self._max_retries:
                    self._subtask_outputs.pop(task_name, None)  # 清除不合格输出
                    self._handle_subagent_failure(task_name, f"质量验证未通过: {quality.get('issues', '')}")
                    return  # 不继续分发依赖链

            self._try_dispatch_pending()

        # 优化5: 任务失败时触发接管策略
        if status == "failed" and task_name:
            error_msg = report.get("message", "未知错误")
            logger.error("[%s] 子任务 '%s' 失败: %s", self.pm_id[:8], task_name, error_msg[:200])
            self._handle_subagent_failure(task_name, error_msg)

        # 同步子任务状态到 Secretary (前端进度实时展示)
        self._sync_subtasks()

        # 转发到 Secretary
        self._report_progress(
            report.get("progress", 0.0),
            status,
            report.get("message", ""),
            reporter_id=reporter_id,
            reporter_type="member",
            task_name=task_name,
        )

    def _try_dispatch_pending(self):
        """检查 pending 子任务, 如果所有依赖都已完成则注入结果并分发。"""
        if not self._pending_subtasks:
            return

        ready = []
        for sub_name, info in list(self._pending_subtasks.items()):
            sub = info["sub"]
            deps = sub.get("depends_on", [])
            # 检查所有依赖是否都已完成
            if all(d in self._subtask_outputs for d in deps):
                ready.append(sub_name)

        for sub_name in ready:
            info = self._pending_subtasks.pop(sub_name)
            sub = info["sub"]
            station = info["station"]
            agent_info = info["agent_info"]

            # 收集所有依赖的输出, 注入到 input_data
            dep_outputs = {}
            for dep_name in sub.get("depends_on", []):
                dep_outputs[dep_name] = self._subtask_outputs.get(dep_name, "")

            # 合并到 task 的 input_data
            task = dict(self._task)
            input_data = dict(task.get("input_data", {}))
            input_data["_dependency_outputs"] = dep_outputs
            task["input_data"] = input_data

            self._dispatched.add(sub_name)
            self._record_subtask_start(sub_name)  # F1.3
            logger.info("[%s] 依赖就绪, 分发待执行子任务 '%s'", self.pm_id[:8], sub_name)
            self._dispatch_subtask(station, agent_info, task, sub, plan=self._plan)

    def _aggregate_results(self):
        """优化4: 收集所有子任务结果, 调用 LLM 聚合为最终交付物。

        流程:
        1. 从 self._subtask_outputs 和 _subagents 收集全部子任务结果
        2. 调用 build_aggregation_prompt() 生成聚合 prompt
        3. 用 runtime LLM 调用生成最终结果
        4. 上报聚合结果到 Secretary
        """
        if not self._plan or not self._task:
            return

        decomposition = self._plan.get("decomposition", [])
        task_name = self._task.get("name", "")
        task_desc = self._task.get("description", "")

        # 收集各子任务结果
        subtask_results = []
        for d in decomposition:
            sub_name = d.get("name", "")
            # 从 _subtask_outputs 获取输出, 或从 _subagents 获取状态
            output = self._subtask_outputs.get(sub_name, "")
            # 从 subagents 中查找状态
            status = "completed" if sub_name in self._subtask_outputs else "unknown"
            for m in self._subagents.values():
                if m.get("current_task") == sub_name:
                    status = m.get("status", status)
                    break
            subtask_results.append({
                "name": sub_name,
                "skill": d.get("skill", ""),
                "status": status,
                "output": output,
            })

        logger.info("[%s] 开始聚合 %d 个子任务结果", self.pm_id[:8], len(subtask_results))

        # 生成聚合 prompt 并调用 LLM
        agg_prompt = build_aggregation_prompt(task_name, task_desc, subtask_results, self._plan)
        try:
            resp = self.runtime._call_llm_with_routing(
                agg_prompt,
                {"_model_preference": "", "_fallback_models": [],
                 "description": "结果聚合"}
            )
            aggregated = resp.get("content", "")
        except Exception as e:
            logger.error("[%s] LLM 聚合失败: %s", self.pm_id[:8], e)
            aggregated = f"[聚合失败] {e}"

        # 上报聚合结果
        self._report_status("completed")
        self._report_progress(
            1.0, "completed",
            f"任务聚合完成: {task_name}",
            task_name=task_name,
        )

        # 将聚合结果也存入 subtask_outputs 供后续查询
        self._subtask_outputs["_aggregated"] = aggregated
        logger.info("[%s] 聚合完成, 结果长度: %d 字符", self.pm_id[:8], len(aggregated))

        # ── 优化9: 交付闭环 — 将最终交付物上报 Secretary ──
        self._deliver_result(task_name, task_desc, aggregated, subtask_results)

    def _deliver_result(self, task_name: str, task_desc: str,
                        aggregated: str, subtask_results: list):
        """优化9: 将最终交付物上报 Secretary, 触发 Boss 验收流程。

        Secretary 收到后:
        1. 存储交付物到 DB
        2. WebSocket 广播 "task_delivered" 到 Web UI
        3. Bot 推送通知到手机
        4. 聊天窗口主动告知 Boss

        Args:
            task_name: 任务名称
            task_desc: 任务描述
            aggregated: 聚合后的最终交付物 (Markdown)
            subtask_results: 各子任务结果摘要
        """
        # 构建交付物摘要 (避免过长)
        summary = aggregated[:500] if len(aggregated) > 500 else aggregated
        completed_count = sum(1 for r in subtask_results if r.get("status") == "completed")
        total_count = len(subtask_results)

        delivery = {
            "pm_id": self.pm_id,
            "task_id": self._task.get("task_id", ""),
            "task_name": task_name,
            "task_description": task_desc,
            "deliverable": aggregated,
            "summary": summary,
            "subtask_stats": {
                "total": total_count,
                "completed": completed_count,
                "failed": total_count - completed_count,
            },
            "delivered_at": time.time(),
        }

        try:
            resp = requests.post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/deliver",
                json=delivery,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("[%s] 交付物已上报 Secretary", self.pm_id[:8])
            else:
                logger.error("[%s] 交付物上报失败: HTTP %d", self.pm_id[:8], resp.status_code)
        except Exception as e:
            logger.error("[%s] 交付物上报异常: %s", self.pm_id[:8], e)

        # F3.2: 任务产物自动分发到共享目录 (跨站同步)
        self._distribute_artifacts(task_name, aggregated)

        # ── 优化14: 记录任务记忆 ──
        self._record_task_memory(task_name, task_desc, subtask_results)

    def _distribute_artifacts(self, task_name: str, content: str):
        """F3.2: 将任务产物写入共享目录, 触发跨站文件同步。

        产物存储在: shared_folder/deliverables/{task_id}_{task_name}.md
        其他站点通过 cloud_sync 自动拉取。
        """
        try:
            from .shared_folder import SharedFolder
            from .config import load_config
            cfg = load_config()
            sf = SharedFolder(cfg)

            # 构建产物目录
            import os
            deliverables_dir = os.path.join(sf.path, "deliverables")
            os.makedirs(deliverables_dir, exist_ok=True)

            # 文件名: task_id_taskname.md
            task_id = self._task.get("task_id", "unknown")[:8]
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_name)[:40]
            filename = f"{task_id}_{safe_name}.md"
            filepath = os.path.join(deliverables_dir, filename)

            # 写入产物
            header = f"# {task_name}\n\n> PM: {self.pm_id[:8]} | 时间: {time.strftime('%Y-%m-%d %H:%M')}\n\n"
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(header + content)

            logger.info("[%s] F3.2 产物已分发: %s", self.pm_id[:8], filename)
        except Exception as e:
            logger.debug("[%s] 产物分发失败 (non-critical): %s", self.pm_id[:8], e)

    def _record_task_memory(self, task_name: str, task_desc: str, subtask_results: list):
        """优化14: 任务完成后记录模式到 Secretary 的任务记忆表。

        记录内容:
        - 任务名称和关键词 (用于未来同类任务匹配)
        - 协作模式 (parallel/sequential/hierarchical)
        - 团队规模
        - 耗时
        - 成功/失败
        - 错误模式 (如果有)

        Secretary 收到后存入 task_memory 表, 未来提交同类任务时
        可参考历史经验选择最优协作模式和团队配置。
        """
        # 提取关键词 (从任务名和描述中提取)
        keywords = []
        for word in task_name.replace(":", " ").replace(":", " ").split():
            if len(word) >= 2:
                keywords.append(word)
        # 从描述中补充关键词
        for word in task_desc[:100].replace(":", " ").replace(":", " ").split():
            if len(word) >= 2 and word not in keywords:
                keywords.append(word)
        keywords = keywords[:10]  # 最多10个

        # 推断任务类型
        task_type = self._infer_task_type(task_name, task_desc)

        # 计算耗时
        duration = time.time() - self._task.get("_start_time", time.time())

        # 判断成功/失败
        completed_count = sum(1 for r in subtask_results if r.get("status") == "completed")
        success = completed_count >= len(subtask_results) * 0.5 if subtask_results else True

        # 提取错误模式
        error_pattern = ""
        for r in subtask_results:
            if r.get("status") != "completed" and r.get("error"):
                error_pattern = r["error"][:100]
                break

        # 团队规模
        team_size = len(self._subagents) + len(self._teams)

        memory_data = {
            "pm_id": self.pm_id,
            "task_name": task_name,
            "task_keywords": keywords,
            "task_type": task_type,
            "collaboration_mode": self._plan.get("collaboration_mode", "") if self._plan else "",
            "team_size": team_size,
            "duration_secs": duration,
            "success": success,
            "error_pattern": error_pattern,
            "device_id": self.device_id,
        }

        try:
            resp = requests.post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/task-memory",
                json=memory_data,
                timeout=10,
            )
            if resp.status_code == 200:
                logger.info("[%s] 任务记忆已记录 (type=%s, success=%s)", self.pm_id[:8], task_type, success)
            else:
                logger.error("[%s] 任务记忆记录失败: HTTP %d", self.pm_id[:8], resp.status_code)
        except Exception as e:
            logger.error("[%s] 任务记忆记录异常: %s", self.pm_id[:8], e)

    @staticmethod
    def _infer_task_type(task_name: str, task_desc: str) -> str:
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

    # ── F2.5: 质量验证器 ───────────────────────────────────────

    def _verify_output_quality(self, task_name: str, output: str) -> Optional[dict]:
        """F2.5: 生成-验证器模式 — 用 LLM 评估子任务输出质量。

        返回:
            {"accepted": bool, "score": float (0-10), "issues": str}
            或 None (跳过验证, 如输出过短)
        """
        # 跳过条件: 输出过短 (可能是简单状态报告) 或无 LLM 可用
        if not output or len(str(output)) < 50:
            return None

        # 查找子任务描述
        sub_desc = ""
        for d in self._plan.get("decomposition", []):
            if d.get("name") == task_name:
                sub_desc = d.get("description", "")
                break

        verify_prompt = f"""你是质量验证器。请评估以下子任务输出的质量。

## 子任务要求
{sub_desc or task_name}

## 实际输出 (前2000字)
{str(output)[:2000]}

## 评估标准
1. 完整性: 是否覆盖了任务要求的所有方面
2. 正确性: 内容是否逻辑正确、无明显错误
3. 可用性: 输出是否可直接使用 (代码可运行/文档可阅读)

请严格输出 JSON (不要 markdown):
{{"accepted": true/false, "score": 0-10, "issues": "问题描述(无问题则为空)"}}

注意: score >= 6 则 accepted=true。只有明显缺陷才判定为不通过。"""

        try:
            resp = self.runtime._call_llm_with_routing(
                verify_prompt,
                {"_model_preference": "", "_fallback_models": [], "description": "质量验证"}
            )
            content = resp.get("content", "")
            # 解析 JSON
            import json as _j
            # 尝试提取 JSON
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                result = _j.loads(content[start:end])
                return {
                    "accepted": result.get("accepted", True),
                    "score": float(result.get("score", 7)),
                    "issues": result.get("issues", ""),
                }
        except Exception as e:
            logger.debug("[%s] 质量验证异常 (skip): %s", self.pm_id[:8], e)

        return None  # 验证失败时不阻塞流程

    def _handle_subagent_failure(self, task_name: str, error_msg: str):
        """优化5: 子 Agent 失败后的接管策略。

        三级策略:
        1. 同站重试 (重试次数 < max_retries)
        2. 换站重试 (有其他可用 station)
        3. PM 本地接管 (无其他选项时)
        """
        retry_count = self._retry_counts.get(task_name, 0)

        # 从 plan 中找到对应的子任务定义
        decomposition = self._plan.get("decomposition", []) if self._plan else []
        sub = None
        for d in decomposition:
            if d.get("name") == task_name:
                sub = d
                break
        if not sub:
            logger.warning("[%s] 失败子任务 '%s' 未在 plan 中找到, 跳过接管", self.pm_id[:8], task_name)
            return

        original_station = self._task_station.get(task_name, {})
        original_agent = self._task_agent.get(task_name, {})

        # 策略1: 同站重试
        if retry_count < self._max_retries:
            self._retry_counts[task_name] = retry_count + 1
            logger.info("[%s] 同站重试 '%s' (第 %d 次)", self.pm_id[:8], task_name, retry_count + 1)
            # 更新 prompt: 追加失败上下文
            agent_id = original_agent.get("agent_id", "")
            if agent_id and original_station:
                # 重新生成分发上下文
                task = dict(self._task)
                input_data = dict(task.get("input_data", {}))
                # 标注上次失败原因
                input_data["_retry_context"] = {
                    "attempt": retry_count + 1,
                    "previous_error": error_msg[:500],
                }
                task["input_data"] = input_data
                self._dispatch_subtask(original_station, original_agent, task, sub, plan=self._plan)
            return

        # 策略2: 换站重试
        stations = self._get_available_stations()
        other_stations = [s for s in stations
                          if s.get("device_id") != original_station.get("device_id")]
        if other_stations:
            new_station = other_stations[0]
            logger.info("[%s] 换站重试 '%s' → %s", self.pm_id[:8], task_name, new_station.get('device_name', new_station.get('ip', '')))
            self._retry_counts[task_name] = retry_count + 1

            # 在新站点创建子 Agent
            new_agent_id = f"sub-{uuid.uuid4().hex[:10]}"
            agent_name = sub.get("name", "worker")
            skill = sub.get("skill", "code_generation")
            system_prompt = self._build_subagent_prompt_for_sub(
                self._task, sub, self._plan, new_agent_id, agent_name
            )
            new_agent_info = self._create_subagent_on_station(
                new_station, agent_name, [skill], sub.get("description", ""),
                system_prompt=system_prompt,
                preferred_agent_id=new_agent_id,
            )
            if new_agent_info:
                self._task_station[task_name] = new_station
                self._task_agent[task_name] = new_agent_info
                self._dispatch_subtask(new_station, new_agent_info, self._task, sub, plan=self._plan)
            return

        # 策略3: PM 本地接管
        logger.info("[%s] PM 本地接管子任务 '%s'", self.pm_id[:8], task_name)
        result = self._execute_subtask_locally(self._task, sub)

        # ── 优化10: 如果本地接管也失败, 上报 escalated 到 Secretary ──
        # (当前 _execute_subtask_locally 不返回明确状态, 通过进度上报判断)
        # 这里主动上报升级状态, 让 Boss 知晓
        self._report_escalation(task_name, error_msg, sub)

    def _report_escalation(self, task_name: str, error_msg: str, sub: dict):
        """优化10: 三级接管全失败后, 上报 escalated 状态到 Secretary。

        Secretary 收到后:
        1. WebSocket 广播 "task_escalated" 到 Web UI
        2. Bot 推送高优先级通知到手机
        3. 聊天窗口展示升级问题和选项, 等待 Boss 决策

        Args:
            task_name: 失败的子任务名
            error_msg: 最后的错误信息
            sub: 子任务定义
        """
        escalation = {
            "pm_id": self.pm_id,
            "task_id": self._task.get("task_id", ""),
            "task_name": self._task.get("name", ""),
            "failed_subtask": task_name,
            "error": error_msg[:500],
            "retry_count": self._retry_counts.get(task_name, 0),
            "options": [
                "忽略此子任务, 继续执行其他部分",
                "降低质量要求, 用简化方案重试",
                "手动指定一台主机重试",
                "放弃整个任务",
            ],
            "escalated_at": time.time(),
        }

        # 通过 status 上报 (Secretary 会识别 escalated 状态)
        self._report_status("escalated")
        self._report_progress(
            -2.0,  # 特殊标记: escalated
            "escalated",
            f"子任务 '{task_name}' 三级接管全失败: {error_msg[:200]}",
            reporter_type="pm_escalation",
            task_name=task_name,
        )

        # 额外 POST 升级详情 (含选项)
        try:
            requests.post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/status",
                json={
                    "status": "escalated",
                    "escalation": escalation,
                },
                timeout=5,
            )
        except Exception:
            pass

        logger.warning("[%s] 子任务 '%s' 已上报 escalated, 等待 Boss 决策", self.pm_id[:8], task_name)

    # ── 优化7: 反向沟通通道 ──────────────────────────────────────

    def receive_input(self, input_data: dict):
        """接收来自 Boss 的回复输入 (由 Worker 调用)。

        当 PM 通过 _request_clarification() 请求 Boss 决策后,
        Secretary 将 Boss 的回复通过此方法注入, 唤醒等待中的 PM 线程。

        Args:
            input_data: Boss 回复数据, 至少包含 {"response": "...", "choice": "..."}
        """
        self._clarification_response = input_data
        self._clarification_event.set()
        logger.info("[%s] 收到 Boss 回复: %s", self.pm_id[:8], str(input_data)[:200])

    # ── F2.3: 多轮任务细化 ─────────────────────────────────────

    def _refine_requirements(self, task: dict) -> dict:
        """F2.3: 多轮对话式任务细化。

        仅当任务描述过短/模糊时触发 (描述 < 20 字且无 input_data)。
        最多 2 轮追问, 将 Boss 回复累积到 task description 中。
        如果 Boss 未回复 (超时), 则按原有描述继续执行。
        """
        desc = task.get("description", task.get("name", ""))
        input_data = task.get("input_data", {})

        # 触发条件: 描述过短 且 无额外输入数据
        if len(desc) >= 20 or input_data:
            return task

        logger.info("[%s] 任务描述模糊 (%d字), 尝试细化", self.pm_id[:8], len(desc))

        max_rounds = 2
        accumulated_context = []

        for round_num in range(max_rounds):
            # 生成追问
            question = self._generate_refinement_question(desc, accumulated_context, round_num)
            if not question:
                break

            # 向 Boss 发起追问 (短超时, 不阻塞太久)
            response = self._request_clarification(
                question=question,
                timeout=120.0,  # 2分钟超时
            )

            if not response or response.get("timed_out"):
                logger.info("[%s] 细化轮次%d 未收到回复, 继续执行", self.pm_id[:8], round_num + 1)
                break

            # 累积回复
            answer = response.get("response", response.get("choice", ""))
            if answer:
                accumulated_context.append(f"Q{round_num+1}: {question}\nA{round_num+1}: {answer}")

        # 将累积的上下文注入任务描述
        if accumulated_context:
            enriched_desc = desc + "\n\n## 补充信息 (Boss 细化)\n" + "\n".join(accumulated_context)
            task = dict(task)  # 浅拷贝
            task["description"] = enriched_desc
            logger.info("[%s] 任务描述已细化: %d → %d 字",
                       self.pm_id[:8], len(desc), len(enriched_desc))

        return task

    def _generate_refinement_question(self, desc: str, context: list, round_num: int) -> str:
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

    def _request_clarification(self, question: str, options: list = None,
                                timeout: float = None) -> dict:
        """向 Boss 发起澄清请求, 阻塞等待回复。

        当 PM 在执行中遇到需要人类决策的问题时调用此方法:
        1. 上报 status='awaiting_input' 到 Secretary (附带问题+选项)
        2. 阻塞当前线程, 等待 Boss 通过 Secretary 回复
        3. 超时或收到回复后返回决策结果

        Args:
            question: 请求 Boss 决策的问题描述
            options: 可选决策列表, 如 ["方案A: xxx", "方案B: yyy"]
            timeout: 等待超时秒数 (默认使用 self._clarification_timeout)

        Returns:
            {"response": "Boss的文本回复", "choice": "选中的选项", "timestamp": ...}
            超时返回 {"response": "", "choice": "", "timed_out": True}
        """
        # 重置 event
        self._clarification_event.clear()
        self._clarification_response = {}

        # 向 Secretary 上报等待输入状态
        self._report_status(
            "awaiting_input",
            task_list=[{
                "name": "请求决策",
                "status": "awaiting_input",
                "description": question,
            }],
        )
        # 附带问题详情到进度上报
        self._report_progress(
            -1.0,  # 特殊标记: awaiting_input
            "awaiting_input",
            question,
            reporter_type="pm_clarification",
            task_name=question[:100],
        )
        # 可选: 将 options 写入 DB (通过 status payload 扩展)
        if options:
            try:
                import requests as _r
                _r.post(
                    f"{self.secretary_url}/api/pm/{self.pm_id}/status",
                    json={
                        "status": "awaiting_input",
                        "clarification_question": question,
                        "clarification_options": options,
                    },
                    timeout=5,
                )
            except Exception:
                pass

        actual_timeout = timeout if timeout is not None else self._clarification_timeout
        logger.info("[%s] 等待 Boss 决策 (超时=%ds): %s", self.pm_id[:8], actual_timeout, question[:120])

        # 阻塞等待回复
        received = self._clarification_event.wait(timeout=actual_timeout)

        if not received:
            print(f"[PM {self.pm_id[:8]}] 等待 Boss 决策超时, 使用默认策略继续")
            return {"response": "", "choice": "", "timed_out": True}

        # 恢复 running 状态
        self._report_status("executing")
        return self._clarification_response

    # ── 优化8: 取消/暂停 ───────────────────────────────────────────

    def cancel(self):
        """取消 PM Agent 及所有子 Agent 的执行。"""
        self._running = False
        # 唤醒可能在等待澄清的线程
        self._clarification_event.set()
        self._clarification_response = {"response": "", "choice": "", "cancelled": True}
        self._report_status("cancelled")
        self._report_progress(0.0, "cancelled", "任务已被 Boss 取消")
        print(f"[PM {self.pm_id[:8]}] 任务已取消")

    def pause(self):
        """暂停 PM Agent 的任务执行。

        注意: 当前通过设置 running=False 实现暂停,
        已分发的子任务不会立即停止 (它们独立运行)。
        恢复时需重新调用 start_task()。
        """
        self._running = False
        # 唤醒可能在等待澄清的线程
        self._clarification_event.set()
        self._clarification_response = {"response": "", "choice": "", "paused": True}
        self._report_status("paused")
        self._report_progress(0.0, "paused", "任务已被 Boss 暂停")
        print(f"[PM {self.pm_id[:8]}] 任务已暂停")

    # ── 上报 Secretary ───────────────────────────────────────────

    def _report_status(self, status: str, team_structure: dict = None,
                       task_list: list = None, collaboration_mode: str = None):
        """向 Secretary 上报 PM 状态变更。"""
        try:
            payload = {"status": status}
            if team_structure is not None:
                payload["team_structure"] = team_structure
            if task_list is not None:
                payload["task_list"] = task_list
            if collaboration_mode is not None:
                payload["collaboration_mode"] = collaboration_mode
            requests.post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/status",
                json=payload, timeout=5,
            )
        except Exception as e:
            print(f"[PM {self.pm_id[:8]}] 上报状态失败: {e}")

    def _report_team(self, team: dict):
        """向 Secretary 上报团队信息。"""
        try:
            requests.post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/teams",
                json=team, timeout=5,
            )
        except Exception as e:
            print(f"[PM {self.pm_id[:8]}] 上报团队信息失败: {e}")

    def _report_progress(self, progress: float, status: str, message: str,
                         reporter_id: str = "", reporter_type: str = "pm",
                         task_name: str = ""):
        """向 Secretary 上报进度。"""
        try:
            requests.post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/progress",
                json={
                    "pm_id": self.pm_id,
                    "reporter_id": reporter_id or self.pm_id,
                    "reporter_type": reporter_type,
                    "task_name": task_name,
                    "progress": progress,
                    "status": status,
                    "message": message,
                    "timestamp": time.time(),
                },
                timeout=5,
            )
        except Exception as e:
            print(f"[PM {self.pm_id[:8]}] 上报进度失败: {e}")

    def _sync_subtasks(self):
        """向 Secretary 同步子任务状态列表 (供前端进度展示)。

        将当前 plan decomposition + 运行时状态合并为 SubTask 结构,
        写入任务记录的 subtasks 字段, 解决前端进度始终 0% 的问题。
        """
        try:
            subtasks = self._build_subtask_status()
            if not subtasks:
                return
            requests.post(
                f"{self.secretary_url}/api/pm/{self.pm_id}/subtasks",
                json={
                    "task_id": (self._task or {}).get("task_id", ""),
                    "subtasks": subtasks,
                },
                timeout=5,
            )
        except Exception as e:
            print(f"[PM {self.pm_id[:8]}] 同步子任务失败: {e}")

    def _build_subtask_status(self) -> list:
        """构建当前子任务状态列表 (从 plan + 运行时状态推导)。"""
        decomposition = (self._plan or {}).get("decomposition", [])
        result = []
        for sub in decomposition:
            name = sub.get("name", "")
            # 状态推导: 已有输出=completed; 否则查 subagent 运行态
            if name in self._subtask_outputs:
                status = "completed"
            else:
                status = "pending"
                for m in self._subagents.values():
                    if m.get("current_task") == name:
                        raw = m.get("status", "pending")
                        if raw in ("busy", "executing", "working"):
                            status = "running"
                        elif raw in ("completed", "failed"):
                            status = raw
                        else:
                            status = "assigned"
                        break
            result.append({
                "name": name,
                "description": sub.get("description", ""),
                "required_skill": sub.get("skill", ""),
                "depends_on": sub.get("depends_on", []),
                "status": status,
            })
        return result

    # ── 技能加载 ──────────────────────────────────────────────────

    def _load_skill(self) -> str:
        """从本地技能缓存加载 multi-agent-architect skill。"""
        skill_dir = Path.home() / ".lan_mesh" / "skills_cache" / "multi-agent-architect"
        skill_md = skill_dir / "SKILL.md"
        if not skill_md.is_file():
            print(f"[PM {self.pm_id[:8]}] multi-agent-architect skill 未找到, 使用内置摘要")
            return self._builtin_skill_summary()

        try:
            content = skill_md.read_text(encoding="utf-8")
            # 去掉 YAML front matter
            if content.startswith("---"):
                parts = content.split("---", 2)
                if len(parts) >= 3:
                    content = parts[2].strip()
            return content
        except Exception:
            return self._builtin_skill_summary()

    @staticmethod
    def _builtin_skill_summary() -> str:
        """内置技能摘要 (技能缓存不可用时的回退)。"""
        return """多智能体协作架构决策框架:
1. 默认单智能体，仅当上下文触顶/工具过载/天然并行性/视角异构时才拆分
2. 拆分判定: 讲背景→合并, 看状态→拆分
3. 成本比值 <1.2 不拆, >1.5 可拆
4. 五种模式: 生成-验证器/编排者-子Agent/Agent Teams/Message Bus/Shared State
5. 简单任务用 single, 中等用 orchestrator, 复杂用 teams"""
