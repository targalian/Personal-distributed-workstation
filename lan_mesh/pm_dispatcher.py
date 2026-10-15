"""
PM 分发器 — 团队创建与子任务分发

职责:
1. 获取可用 work_station 列表
2. 创建团队与子 Agent
3. 依赖感知调度 (depends_on 满足后才分发)
4. 构建子 Agent 定制 prompt
5. 本地执行回退
"""
import time
import threading
import uuid
import copy
from graphlib import TopologicalSorter, CycleError
from typing import Optional

import requests

from .agent_prompt import build_subagent_prompt, build_dispatch_context
from .http_retry import auth_headers, http_get
from .logger import get_logger
from .pm_state import PMState
from .execution_routes import normalize_result

logger = get_logger("pm.dispatcher")


class PMDispatcher:
    """分发器: 根据 plan 选择 station → 创建 team → 分发子任务到 Worker。"""

    def __init__(self, pm_id: str, runtime, secretary_url: str,
                 device_id: str, state: PMState, agent):
        """
        Args:
            pm_id: PM Agent ID
            runtime: Worker 的 AgentRuntime 实例
            secretary_url: Secretary API 地址
            device_id: 本机设备 ID
            state: 共享状态
            agent: ProjectManagerAgent 协调器引用 (用于上报)
        """
        self._pm_id = pm_id
        self._runtime = runtime
        self._secretary_url = secretary_url
        self._device_id = device_id
        self._state = state
        self._agent = agent

    # ── 团队创建与分发 ────────────────────────────────────────────

    @staticmethod
    def _validate_plan(decomposition: list):
        if not decomposition or not isinstance(decomposition, list):
            raise ValueError("多任务计划必须包含子任务")
        if any(not isinstance(sub, dict) or not isinstance(sub.get("name"), str)
               or not sub["name"].strip() for sub in decomposition):
            raise ValueError("子任务名称必须为非空字符串")
        names = {sub["name"] for sub in decomposition}
        if len(names) != len(decomposition):
            raise ValueError("子任务名称不能重复")
        graph = {}
        for sub in decomposition:
            deps = sub.get("depends_on", [])
            if not isinstance(deps, list) or any(not isinstance(dep, str) or dep not in names for dep in deps):
                raise ValueError(f"子任务 {sub['name']} 的前置依赖不存在或格式错误")
            graph[sub["name"]] = deps
        try:
            tuple(TopologicalSorter(graph).static_order())
        except CycleError as exc:
            raise ValueError("子任务计划存在循环依赖") from exc

    def create_team_and_dispatch(self, task: dict, plan: dict):
        """根据规划创建团队并分发子任务 (依赖感知拓扑排序)。

        策略:
        1. 为所有子任务创建子 Agent (需先存在于 station 上)
        2. 只分发无依赖 (depends_on 为空) 的子任务
        3. 有依赖的子任务暂存为 pending, 等前序完成后自动注入结果并分发
        """
        decomposition = plan.get("decomposition", [])
        self._validate_plan(decomposition)
        pattern = plan.get("pattern", "orchestrator")
        st = self._state
        if st.stop_event.is_set():
            return

        with st.lock:
            st.plan = plan
            st.task = task
            if not st.persist("dispatching"):
                return

        # 获取可用 work_station 列表
        stations = self.get_available_stations()
        if not stations:
            logger.info("[%s] 无可用 work_station, 本地执行全部子任务", self._pm_id[:8])
            with st.lock:
                for sub in decomposition:
                    st.pending_subtasks[sub["name"]] = {"sub": sub, "station": {}, "agent_info": {}}
            self.try_dispatch_pending()
            return

        # 创建团队
        team_id = str(uuid.uuid4())
        team = {
            "team_id": team_id,
            "pm_id": self._pm_id,
            "team_name": f"Team-{team_id[:8]}",
            "team_type": pattern,
            "device_id": self._device_id,
            "parent_team_id": "",
            "members": [],
            "status": "active",
            "current_task": task.get("name", ""),
            "created_at": time.time(),
        }

        # 为每个子任务选择 work_station 并创建子 Agent
        station_idx = 0
        for sub in decomposition:
            if st.stop_event.is_set():
                return
            station = stations[station_idx % len(stations)]
            station_idx += 1

            agent_id = f"sub-{uuid.uuid4().hex[:10]}"
            agent_name = sub.get("name", "worker")
            skill = sub.get("skill", "code_generation")
            task_desc = sub.get("description", "")
            sub_name = sub.get("name", "")

            system_prompt = self._build_subagent_prompt_for_sub(
                task, sub, plan, agent_id, agent_name
            )

            agent_info = self._create_subagent_on_station(
                station, agent_name, [skill], task_desc,
                system_prompt=system_prompt,
                preferred_agent_id=agent_id,
            )

            if not agent_info:
                # 修复: 远程创建失败时回退到本地执行
                logger.warning("[%s] 子任务 '%s' 远程创建子Agent失败, 回退本地执行",
                              self._pm_id[:8], sub_name)
                with st.lock:
                    st.pending_subtasks[sub_name] = {"sub": sub, "station": {}, "agent_info": {}}
                continue

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

                with st.lock:
                    st.subagents[member["member_id"]] = member
                    st.task_station[sub_name] = station
                    st.task_agent[sub_name] = agent_info

                    if not sub.get("depends_on"):
                        st.dispatched.add(sub_name)
                    else:
                        st.pending_subtasks[sub_name] = {
                            "sub": sub, "station": station, "agent_info": agent_info,
                        }
                        logger.debug("[%s] 子任务 '%s' 等待依赖: %s",
                                    self._pm_id[:8], sub_name, sub.get('depends_on'))

                if not sub.get("depends_on"):
                    self._record_subtask_start(sub_name)
                    # 非阻塞分发: 独立子任务并行执行
                    t = threading.Thread(
                        target=self.dispatch_subtask,
                        args=(station, agent_info, task, sub),
                        kwargs={"plan": plan},
                        daemon=True,
                        name=f"dispatch-{sub_name[:16]}",
                    )
                    t.start()

        # 上报团队结构到 Secretary
        with st.lock:
            st.teams[team_id] = team
            if not st.persist():
                return
        self._agent.report_team(team)
        self._agent.report_status("monitoring", team_structure=team)
        self.try_dispatch_pending()

    # ── 分发子任务 ────────────────────────────────────────────────

    def _begin_attempt(self, name: str, task: dict, agent_id: str, station: dict) -> dict | None:
        st = self._state
        with st.lock:
            current = st.execution_attempts.get(name)
            if st.stop_event.is_set() or name in st.subtask_outputs:
                return None
            if current and current["phase"] in ("active", "processing"):
                return None
            if current:
                st.execution_history[current["subtask_id"]] = copy.deepcopy(current)
            attempt = {"subtask_id": str(uuid.uuid4()), "parent_task_id": task.get("task_id", ""),
                       "pm_id": self._pm_id, "reporter_id": agent_id, "name": name,
                       "station": dict(station), "phase": "active"}
            st.execution_attempts[name] = attempt
            st.subtask_start_times[name] = time.time()
            if not st.persist():
                return None
            return attempt

    def dispatch_subtask(self, station: dict, agent_info: dict, task: dict,
                         sub: dict, plan: dict = None):
        """Dispatch once; uncertain responses are resolved by polling the original station."""
        if not station:
            self.execute_subtask_locally(task, sub)
            return
        name = sub.get("name", "")
        attempt = self._begin_attempt(name, task, agent_info.get("agent_id", ""), station)
        if attempt is None:
            return
        inputs = dict(task.get("input_data", {}))
        if plan:
            inputs.update(build_dispatch_context(task, sub, plan, self._pm_id))
        payload = {key: attempt[key] for key in ("subtask_id", "parent_task_id", "pm_id", "reporter_id", "name")}
        payload.update(description=sub.get("description", ""), required_skill=sub.get("skill", "code_generation"),
                       input_data=inputs, model_preference="", fallback_models=[])
        with self._state.lock:
            attempt["request"] = payload
            if self._state.stop_event.is_set() or not self._state.persist():
                return
        try:
            response = requests.post(
                f"http://{station.get('ip', '')}:{station.get('api_port', 0)}/tasks/execute",
                json=payload, headers=auth_headers(), timeout=15)
            if response.status_code != 200:
                raise RuntimeError(f"HTTP {response.status_code}")
            self._receive_receipt(attempt, response.json(), legacy_sync=True)
        except Exception as exc:
            # A connection reset, 5xx, invalid JSON or timeout can follow execution.
            # Never move the work to another host without a terminal receipt.
            logger.warning("[%s] 子任务 %s 派发未确认 (%s)，查询原站点执行回执", self._pm_id[:8], name, exc)
            self._agent.report_progress(0.0, "in_progress", "派发未确认，正在核实原站点执行回执",
                                        reporter_type="member", task_name=name)

    def _receive_receipt(self, attempt: dict, receipt: dict, legacy_sync: bool = False):
        if not isinstance(receipt, dict):
            return
        status = receipt.get("status")
        if legacy_sync and status == "started" and "subtask_id" not in receipt:
            self._agent.receive_subtask_result(
                task_name=attempt["name"], status="unknown", output_data={},
                agent_id=attempt["reporter_id"], execution_id=attempt["subtask_id"],
                error="远端节点不支持执行回执，请升级节点并核实已发出的任务；不自动重试")
            return
        # Old synchronous Workers return a result on this exact request. Old async
        # Stations cannot prove ownership; they must be upgraded before retrying.
        legacy = legacy_sync and "subtask_id" not in receipt and status in ("completed", "failed")
        if not legacy and any(receipt.get(key) != attempt[key] for key in (
                "subtask_id", "parent_task_id", "pm_id", "reporter_id", "name")):
            logger.warning("[%s] 忽略身份不匹配的执行回执: %s", self._pm_id[:8], attempt["subtask_id"])
            return
        if status in ("completed", "failed", "unknown"):
            result = normalize_result(receipt) if status != "unknown" else receipt
            self._agent.receive_subtask_result(
                task_name=attempt["name"], status=result["status"], output_data=result.get("output", {}),
                agent_id=attempt["reporter_id"], execution_id=attempt["subtask_id"], error=result.get("error", ""))

    def poll_remote_results(self):
        """Read execution receipts; GET retries never cause tool execution."""
        st = self._state
        with st.lock:
            attempts = [dict(a) for a in st.execution_attempts.values() if a["station"] and a["phase"] == "active"]
        for attempt in attempts:
            if st.stop_event.is_set():
                return
            station = attempt["station"]
            try:
                response = requests.get(
                    f"http://{station.get('ip', '')}:{station.get('api_port', 0)}/tasks/executions/{attempt['subtask_id']}",
                    headers=auth_headers(), timeout=3)
                if response.status_code == 200:
                    self._receive_receipt(attempt, response.json())
            except Exception as exc:
                logger.debug("执行回执暂不可读: %s (%s)", attempt["subtask_id"], exc)

    # ── 依赖就绪分发 ──────────────────────────────────────────────

    def try_dispatch_pending(self):
        """检查 pending 子任务, 如果所有依赖都已完成则注入结果并分发。"""
        st = self._state
        if st.stop_event.is_set():
            return
        with st.lock:
            if not st.pending_subtasks:
                return

            ready = []
            for sub_name, info in list(st.pending_subtasks.items()):
                sub = info["sub"]
                deps = sub.get("depends_on", [])
                if all(d in st.subtask_outputs for d in deps):
                    ready.append(sub_name)

            dispatch_items = []
            for sub_name in ready:
                info = st.pending_subtasks.pop(sub_name)
                sub = info["sub"]
                dep_outputs = {}
                for dep_name in sub.get("depends_on", []):
                    dep_outputs[dep_name] = st.subtask_outputs.get(dep_name, "")
                dispatch_items.append((sub_name, sub, info["station"], info["agent_info"], dep_outputs))
                st.dispatched.add(sub_name)

        for sub_name, sub, station, agent_info, dep_outputs in dispatch_items:
            task = dict(st.task)
            input_data = dict(task.get("input_data", {}))
            input_data["_dependency_outputs"] = dep_outputs
            task["input_data"] = input_data

            self._record_subtask_start(sub_name)
            logger.info("[%s] 依赖就绪, 分发待执行子任务 '%s'", self._pm_id[:8], sub_name)
            self.dispatch_subtask(station, agent_info, task, sub, plan=st.plan)

    # ── 站点与子 Agent 管理 ───────────────────────────────────────

    def get_available_stations(self) -> list:
        """从 Secretary 获取可用 work_station 列表 (含自身, 带重试)。"""
        try:
            resp = http_get(
                f"{self._secretary_url}/api/hosts",
                timeout=10, retries=2,
            )
            if resp and resp.status_code == 200:
                data = resp.json()
                hosts = data.get("hosts", [])
                return [h for h in hosts if h.get("online") and h.get("api_port")]
        except Exception as e:
            logger.error("[%s] 获取 work_station 列表失败: %s", self._pm_id[:8], e)
        return []

    def _create_subagent_on_station(self, station: dict, agent_name: str, skills: list,
                                     task_description: str = "",
                                     system_prompt: str = "",
                                     preferred_agent_id: str = "") -> Optional[dict]:
        """在目标 work_station 上创建子 Agent。"""
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
                headers=auth_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.error("[%s] 创建子 Agent 失败 (%s:%s): %s", self._pm_id[:8], ip, port, e)
        return None

    def update_subagent_prompt(self, station: dict, agent_id: str, new_prompt: str) -> bool:
        """动态更新子 Agent 的 system prompt (优化2)。"""
        ip = station.get("ip", "")
        port = station.get("api_port", 0)
        if not ip or not port:
            return False

        try:
            resp = requests.post(
                f"http://{ip}:{port}/pm/update-prompt",
                json={"agent_id": agent_id, "system_prompt": new_prompt},
                headers=auth_headers(),
                timeout=10,
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("ok"):
                    logger.info("[%s] 子 Agent %s prompt 已更新", self._pm_id[:8], agent_id)
                    return True
        except Exception as e:
            logger.error("[%s] 更新子 Agent prompt 失败: %s", self._pm_id[:8], e)
        return False

    # ── 本地执行 ──────────────────────────────────────────────────

    def execute_subtask_locally(self, task: dict, sub: dict):
        """在本地执行子任务 (无可用远程站点时)。

        修复: 执行完成后注入 Monitor 进度追踪, 触发依赖链分发和结果聚合。
        """
        st = self._state
        if st.stop_event.is_set():
            return
        sub_name = sub.get("name", "")
        agent_id = f"local-{self._pm_id}-{sub_name}"
        attempt = self._begin_attempt(sub_name, task, agent_id, {})
        if attempt is None:
            return
        with st.lock:
            member = next((m for m in st.subagents.values() if m.get("current_task") == sub_name), None)
            if member is None:
                member = {"current_task": sub_name}
                st.subagents[agent_id] = member
            member.update(agent_id=agent_id, status="executing")
        sub_desc = sub.get("description", sub_name)
        input_data = dict(task.get("input_data", {}))
        if not input_data.get("requirement") and not input_data.get("description"):
            input_data["requirement"] = sub_desc
        subtask = {
            "subtask_id": attempt["subtask_id"],
            "parent_task_id": task.get("task_id", ""),
            "name": sub_name,
            "description": sub_desc,
            "required_skill": sub.get("skill", "code_generation"),
            "input_data": input_data,
            "model_preference": "",
            "fallback_models": [],
        }
        with st.lock:
            attempt["request"] = subtask
            if st.stop_event.is_set() or not st.persist():
                return

        logger.info("[%s] 本地执行子任务: %s (skill=%s)",
                   self._pm_id[:8], sub_name, sub.get("skill", ""))
        try:
            result = self._runtime.execute(subtask)
        except Exception as exc:
            result = {"status": "failed", "error": str(exc), "output": {}}
        if st.stop_event.is_set():
            return
        result = normalize_result(result)
        status, output = result["status"], result["output"]

        # 注入 Monitor 进度追踪 (触发依赖链分发 + 结果聚合)
        self._agent.receive_subtask_result(
            task_name=sub_name,
            status=status,
            output_data=output if isinstance(output, dict) else {"result": str(output)},
            agent_id=agent_id,
            error=result["error"], execution_id=attempt["subtask_id"],
        )

        self._agent.report_progress(
            1.0 if status == "completed" else 0.0, status,
            f"子任务 {sub_name} {status}: {result.get('error', '')[:200]}",
            reporter_type="member", task_name=sub_name,
        )

    # ── Prompt 构建 ───────────────────────────────────────────────

    def _build_subagent_prompt_for_sub(
        self, task: dict, sub: dict, plan: dict, agent_id: str, agent_name: str
    ) -> str:
        """为单个子任务生成定制 system prompt。"""
        decomposition = plan.get("decomposition", [])
        sub_name = sub.get("name", "")
        skill = sub.get("skill", "code_generation")

        teammates = []
        for d in decomposition:
            if d.get("name") != sub_name:
                teammates.append({
                    "agent_name": d.get("name", ""),
                    "role": d.get("skill", ""),
                    "task": d.get("description", "")[:100],
                })

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

        dependents = []
        for d in decomposition:
            if sub_name in d.get("depends_on", []):
                dependents.append({
                    "task_name": d.get("name", ""),
                    "agent_name": d.get("name", ""),
                })

        milestones = []
        desc = sub.get("description", "")
        if desc:
            milestones.append(f"理解任务: {desc[:80]}")
            milestones.append("执行核心工作并产出结果")
            milestones.append("自检输出格式和完整性")
            milestones.append("上报最终结果")

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
            pm_id=self._pm_id,
            agent_id=agent_id,
            agent_name=agent_name,
            task_name=sub_name,
            task_description=sub.get("description", ""),
            skill=skill,
            team_name=f"Team-{self._pm_id[:8]}",
            collaboration_mode=plan.get("pattern", ""),
            teammates=teammates,
            dependencies=dependencies,
            dependents=dependents,
            milestones=milestones,
            input_data=task.get("input_data", {}),
            extra_instructions=extra,
        )

    # ── 内部工具 ──────────────────────────────────────────────────

    def _record_subtask_start(self, task_name: str):
        """F1.3: 记录子任务开始时间。"""
        with self._state.lock:
            self._state.subtask_start_times[task_name] = time.time()
