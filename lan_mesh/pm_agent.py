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

    # ── 核心工作流 ────────────────────────────────────────────────

    def _run_task(self, task: dict):
        """任务执行主流程 (工作线程)。"""
        try:
            # 阶段 1: 规划
            self._report_status("planning")
            plan = self._analyze_with_skill(task)
            print(f"[PM {self.pm_id[:8]}] 规划完成: 模式={plan.get('pattern', 'single')}, "
                  f"子任务={len(plan.get('decomposition', []))}")

            # 阶段 2: 执行
            pattern = plan.get("pattern", "single")
            if pattern == "single":
                # 简单任务，PM 自己做
                self._report_status("executing", collaboration_mode="single")
                result = self._execute_directly(task)
                self._report_status("completed", task_list=[{"name": task.get("name", ""), "status": "completed"}])
                self._report_progress(1.0, "completed", f"任务完成: {result.get('summary', '')}")
            else:
                # 复杂任务，创建团队并分发
                self._report_status("executing", collaboration_mode=pattern,
                                    task_list=plan.get("decomposition", []))
                self._create_team_and_dispatch(task, plan)
                self._report_status("monitoring")

        except Exception as e:
            print(f"[PM {self.pm_id[:8]}] 任务执行失败: {e}")
            self._report_status("failed")
            self._report_progress(0.0, "failed", str(e))

        finally:
            self._running = False

    def _analyze_with_skill(self, task: dict) -> dict:
        """用 LLM + multi-agent-architect skill 分析任务。

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
    {{"name": "子任务名", "skill": "code_generation|code_review|document_summary|shell_exec|file_ops|monitoring", "depends_on": [], "description": "子任务描述"}}
  ],
  "reasoning": "决策理由"
}}

决策规则:
- 默认 single (单 Agent)，除非任务复杂度 >= moderate
- simple → single, team_size=1
- moderate → orchestrator, team_size=2-3
- complex → orchestrator 或 teams, team_size=3-5
- decomposition 中的 depends_on 是子任务名称列表 (前序依赖)
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
            print(f"[PM {self.pm_id[:8]}] LLM 输出 JSON 解析失败, 回退 single 模式")
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
        subtask = {
            "subtask_id": str(uuid.uuid4()),
            "parent_task_id": task.get("task_id", ""),
            "name": task.get("name", ""),
            "description": task.get("description", ""),
            "required_skill": "code_generation",
            "input_data": task.get("input_data", {}),
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
            print(f"[PM {self.pm_id[:8]}] 无可用 work_station, 本地执行全部子任务")
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
                    self._dispatch_subtask(station, agent_info, task, sub, plan=plan)
                else:
                    self._pending_subtasks[sub_name] = {
                        "sub": sub, "station": station, "agent_info": agent_info,
                    }
                    print(f"[PM {self.pm_id[:8]}] 子任务 '{sub_name}' 等待依赖: {sub.get('depends_on')}")

        # 上报团队结构到 Secretary
        self._teams[team_id] = team
        self._report_team(team)
        self._report_status("monitoring", team_structure=team)

    def _execute_subtask_locally(self, task: dict, sub: dict):
        """在本地执行子任务 (无可用远程站点时)。"""
        subtask = {
            "subtask_id": str(uuid.uuid4()),
            "parent_task_id": task.get("task_id", ""),
            "name": sub.get("name", ""),
            "description": sub.get("description", ""),
            "required_skill": sub.get("skill", "code_generation"),
            "input_data": task.get("input_data", {}),
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
            print(f"[PM {self.pm_id[:8]}] 获取 work_station 列表失败: {e}")
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
            print(f"[PM {self.pm_id[:8]}] 创建子 Agent 失败 ({ip}:{port}): {e}")
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
                    print(f"[PM {self.pm_id[:8]}] 子 Agent {agent_id} prompt 已更新")
                    return True
        except Exception as e:
            print(f"[PM {self.pm_id[:8]}] 更新子 Agent prompt 失败: {e}")
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
            print(f"[PM {self.pm_id[:8]}] 分发子任务失败: {e}")

    # ── 进度收集 ──────────────────────────────────────────────────

    def _progress_loop(self):
        """定期收集进度并上报 Secretary (优化4: 全部完成后触发聚合)。"""
        _aggregated = False
        while self._running:
            time.sleep(10)  # 每 10 秒收集一次
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
                print(f"[PM {self.pm_id[:8]}] ⚠ 子任务 '{task_name}' 完成但未附带自检结果")
            elif not self_check.get("passed", False):
                notes = self_check.get("notes", "")
                print(f"[PM {self.pm_id[:8]}] ⚠ 子任务 '{task_name}' 自检未通过: {notes[:200]}")
            else:
                print(f"[PM {self.pm_id[:8]}] 子任务 '{task_name}' 完成, 自检通过: {self_check.get('notes', '')[:100]}")
                print(f"[PM {self.pm_id[:8]}] 子任务 '{task_name}' 完成, 输出已存储")
            self._try_dispatch_pending()

        # 优化5: 任务失败时触发接管策略
        if status == "failed" and task_name:
            error_msg = report.get("message", "未知错误")
            print(f"[PM {self.pm_id[:8]}] 子任务 '{task_name}' 失败: {error_msg[:200]}")
            self._handle_subagent_failure(task_name, error_msg)

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
            print(f"[PM {self.pm_id[:8]}] 依赖就绪, 分发待执行子任务 '{sub_name}'")
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

        print(f"[PM {self.pm_id[:8]}] 开始聚合 {len(subtask_results)} 个子任务结果")

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
            print(f"[PM {self.pm_id[:8]}] LLM 聚合失败: {e}")
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
        print(f"[PM {self.pm_id[:8]}] 聚合完成, 结果长度: {len(aggregated)} 字符")

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
            print(f"[PM {self.pm_id[:8]}] 失败子任务 '{task_name}' 未在 plan 中找到, 跳过接管")
            return

        original_station = self._task_station.get(task_name, {})
        original_agent = self._task_agent.get(task_name, {})

        # 策略1: 同站重试
        if retry_count < self._max_retries:
            self._retry_counts[task_name] = retry_count + 1
            print(f"[PM {self.pm_id[:8]}] 同站重试 '{task_name}' (第 {retry_count + 1} 次)")
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
            print(f"[PM {self.pm_id[:8]}] 换站重试 '{task_name}' → {new_station.get('device_name', new_station.get('ip', ''))}")
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
        print(f"[PM {self.pm_id[:8]}] PM 本地接管子任务 '{task_name}'")
        self._execute_subtask_locally(self._task, sub)

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
