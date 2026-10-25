"""ChatCommandService: extracted domain behavior with explicit component ownership."""

import re
import time
from typing import Any
from .chat_policy import (_find_node_by_name, _extract_local_path)

from .component_service import ComponentService

class ChatCommandService(ComponentService):
    """Single domain implementation behind the existing facade."""

    OPERATIONS = ('_execute_action', '_parse_task_from_message', '_action_submit_task', '_action_workstation_optimization', '_parse_optimization_source', '_parse_optimization_text', '_parse_optimization_priority', '_parse_optimization_decision', '_is_optimization_query', '_format_optimization_item', '_resolve_project_from_message', '_action_create_project', '_action_edit_task_graph', '_parse_graph_edit', '_action_query_status', '_action_query_hosts', '_action_query_tasks', '_action_query_progress', '_action_activate_secretary', '_action_deactivate_secretary', '_action_cancel_task', '_action_pause_task', '_extract_task_id_from_message', '_action_respond_to_pm', '_extract_pm_id_from_message', '_action_accept_delivery', '_action_reject_delivery', '_find_latest_delivered_task', '_get_task_memory_hint')

    def _execute_action(self, action: str, message: str) -> str:
        """执行检测到的操作。

        Args:
            action: 操作类型
            message: 原始用户消息

        Returns:
            操作结果描述 (字符串)
        """
        try:
            if action == "query_status":
                return self.owner._action_query_status()
            elif action == "query_hosts":
                return self.owner._action_query_hosts()
            elif action == "query_tasks":
                return self.owner._action_query_tasks()
            elif action == "query_progress":
                return self.owner._action_query_progress()
            elif action == "activate_secretary":
                return self.owner._action_activate_secretary()
            elif action == "deactivate_secretary":
                return self.owner._action_deactivate_secretary()
            elif action == "submit_task":
                return self.owner._action_submit_task(message)
            elif action == "cancel_task":
                return self.owner._action_cancel_task(message)
            elif action == "pause_task":
                return self.owner._action_pause_task(message)
            elif action == "respond_to_pm":
                return self.owner._action_respond_to_pm(message)
            elif action == "accept_delivery":
                return self.owner._action_accept_delivery(message)
            elif action == "reject_delivery":
                return self.owner._action_reject_delivery(message)
            elif action == "create_project":
                return self.owner._action_create_project(message)
            elif action == "workstation_optimization":
                return self.owner._action_workstation_optimization(message)
            elif action == "edit_task_graph":
                return self.owner._action_edit_task_graph(message)
            return ""
        except Exception as e:
            return f"操作执行失败: {e}"


    def _parse_task_from_message(self, message: str) -> dict:
        """从用户消息中提取任务名称和描述 (LLM 解析)。"""
        extract_prompt = (
            "从以下用户消息中提取任务信息。返回严格 JSON 格式: "
            "{\"name\": \"简短任务名称(10字以内)\", \"description\": \"详细任务描述\"}。\n"
            "如果描述太模糊无法提取有效信息, 返回 {\"name\": \"\", \"description\": \"\"}。\n\n"
            f"消息: {message}"
        )
        resp = self.owner.runtime._call_llm_with_routing(
            extract_prompt,
            {"_model_preference": "", "_fallback_models": []},
        )
        content = resp.get("content", "")
        try:
            # 提取 JSON 部分
            import json
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(content[start:end])
        except Exception:
            pass
        return {"name": message[:20], "description": message}


    def _action_submit_task(self, message: str) -> str:
        """从对话直接提交任务并分配 PM Agent (优化13: 支持优先级)。"""
        task_info = self.owner._parse_task_from_message(message)
        name = task_info.get("name", "")
        description = task_info.get("description", "")

        if not name or len(description) < 5:
            return (
                "请提供更详细的任务信息, 例如:\n"
                "「提交一个代码审查任务: 检查 xxx 项目的安全漏洞」\n"
                "可选: 加「优先」或「紧急」提高优先级"
            )

        # 优化13: 从消息中推断优先级
        priority = "normal"
        msg_lower = message.lower()
        if any(kw in msg_lower for kw in ["紧急", "urgent", "立刻", "马上"]):
            priority = "urgent"
        elif any(kw in msg_lower for kw in ["优先", "重要", "high", "尽快"]):
            priority = "high"
        elif any(kw in msg_lower for kw in ["不急", "低优先", "low", "有空"]):
            priority = "low"

        # iter-93 (BUG-033): 解析项目归属, 让蓝图驱动与验收自检对该任务生效
        project_id = self.owner._resolve_project_from_message(message)
        # BUG-037: 本路径此前只解析 project_id, 从不解析 project_path —
        # 带路径提取的 _extract_project_context 只在需求收集流里调用, 而
        # 「提交任务…」这类直接指令走本函数, 导致子任务在 "." 上作业。
        input_data = {}
        project_path = _extract_local_path(message)
        if project_path:
            input_data["project_path"] = project_path
        result = self.owner.controller.submit_task_from_chat(
            name=name,
            description=description,
            created_by="secretary",
            priority=priority,
            project_id=project_id,
            input_data=input_data or None,
        )

        # 优化14: 查询任务记忆, 提供历史参考
        memory_hint = self.owner._get_task_memory_hint(name, description)

        status = result.get("status", "unknown")
        if status == "failed":
            error = result.get("output_data", {}).get("error", "未知错误")
            return f"任务创建失败: {error}"
        elif status == "running":
            pm_id = result.get("pm_agent_id", "")[:8]
            priority_label = {"low": "低", "normal": "普通", "high": "高", "urgent": "紧急"}.get(priority, priority)
            msg = (
                f"✅ 任务已创建并分配 PM Agent!\n"
                f"- 任务ID: {result.get('task_id', '')}\n"
                f"- 名称: {name}\n"
                f"- 优先级: {priority_label}\n"
                f"- PM Agent: {pm_id}\n"
                f"- 项目: {project_id[:8] if project_id else '(未绑定)'}\n"
                f"- 状态: 运行中"
            )
            if memory_hint:
                msg += f"\n\n💡 历史参考: {memory_hint}"
            return msg
        return f"任务已创建: {name} (状态: {status})"


    def _action_workstation_optimization(self, message: str) -> str:
        """Handle workstation optimization queries, submissions, and decisions."""
        manager = getattr(self.owner.controller, "workstation_optimizer", None)
        if manager is None:
            return "工作站优化功能未初始化"
        try:
            decision = self.owner._parse_optimization_decision(message)
            if decision:
                item = manager.decide(
                    decision["item_id"], decision["decision"], decision["reply"])
                return self.owner._format_optimization_item(item, "决策已更新")
            if self.owner._is_optimization_query(message):
                summary = manager.summary()
                return (
                    f"工作站优化状态:\n"
                    f"- 守护: {'运行中' if summary['guardian_running'] else '未运行'}\n"
                    f"- 队列: {summary['queue_count']}\n"
                    f"- 待决策: {summary['waiting_boss_count']}"
                )
            source = self.owner._parse_optimization_source(message)
            title, description = self.owner._parse_optimization_text(message)
            priority = self.owner._parse_optimization_priority(message)
            item = manager.submit(source, title, description, priority)
            return self.owner._format_optimization_item(item, "优化项已创建")
        except Exception as exc:
            return f"工作站优化操作失败: {exc}"


    def _parse_optimization_source(self, message: str) -> str:
        """Infer optimization source from natural language."""
        if "瓶颈" in message or "限制" in message:
            return "bottleneck"
        if "agent建议" in message or "任务建议" in message or "自己思考" in message:
            return "agent"
        return "boss"


    def _parse_optimization_text(self, message: str) -> tuple[str, str]:
        """Extract a compact title and full description."""
        text = message.strip()
        for prefix in ("优化工作站:", "优化工作站：", "工作站优化:", "工作站优化：",
                       "遇到瓶颈:", "遇到瓶颈：", "添加优化建议:", "添加优化建议："):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        if ":" in text or "：" in text:
            parts = re.split(r"[:：]", text, maxsplit=1)
            text = parts[1].strip() if len(parts) == 2 and parts[1] else text
        title = text[:40] if len(text) <= 40 else text[:37] + "…"
        return title, text


    def _parse_optimization_priority(self, message: str) -> str:
        """Infer optimization priority from natural language."""
        lowered = message.lower()
        if "紧急" in message or "urgent" in lowered:
            return "urgent"
        if "优先" in message or "重要" in message or "high" in lowered:
            return "high"
        if "不急" in message or "低优先" in message or "low" in lowered:
            return "low"
        return "normal"


    def _parse_optimization_decision(self, message: str) -> dict:
        """Parse approve/reject/clarify commands with an optimization item id."""
        match = re.search(r"(opt-[A-Za-z0-9]+)", message)
        if not match:
            return {}
        if "确认优化" in message or "批准优化" in message:
            return {"item_id": match.group(1), "decision": "approve", "reply": ""}
        if "拒绝优化" in message:
            return {"item_id": match.group(1), "decision": "reject", "reply": ""}
        if "补充说明优化" in message:
            reply = re.sub(r"^.*?(?:补充说明优化[:：])?", "", message).strip()
            return {"item_id": match.group(1), "decision": "clarify", "reply": reply}
        return {}


    def _is_optimization_query(self, message: str) -> bool:
        """Detect read-only workstation optimization status requests."""
        if any(marker in message for marker in ("创建", "添加", "建议:", "建议：")):
            return False
        return any(marker in message for marker in ("状态", "队列", "列表", "多少"))


    def _format_optimization_item(self, item: dict, headline: str) -> str:
        """Format one optimization item for the secretary chat reply."""
        status_label = {
            "candidate": "待 Boss 评估", "waiting_boss": "待 Boss 决策",
            "queued": "已排队", "running": "执行中", "completed": "已完成",
            "rejected": "已拒绝", "failed": "失败",
        }.get(item.get("status", ""), item.get("status", ""))
        return (
            f"🛠️ {headline}\n"
            f"- ID: {item.get('id', '')}\n"
            f"- 标题: {item.get('title', '')}\n"
            f"- 来源: {item.get('source', '')}\n"
            f"- 优先级: {item.get('priority', '')}\n"
            f"- 状态: {status_label}"
        )


    def _resolve_project_from_message(self, message: str) -> str:
        """iter-93 (BUG-033): 从 Boss 消息中解析任务应归属的项目 ID。

        对话派发的任务此前恒无 project_id, 导致蓝图驱动规划 (iter-89) 与
        交付前验收自检 (iter-90) 双双失效 — 两者都靠 task.project_id 回查
        项目 charter, 拿不到就静默降级成"无蓝图"。

        匹配顺序 (命中即返回, 全部未命中返回空串保持旧行为):
          1. 完整 project_id (uuid) 直接出现在消息中;
          2. project_id 前 8 位短码 (Boss 惯用写法, 如 "项目 821230b9");
          3. 活跃项目名称包含于消息中 (取最长匹配, 避免短名误命中)。
        """
        manager = getattr(self.owner.controller, "project_manager", None)
        if manager is None or not message:
            return ""
        try:
            projects = manager.list_projects(status="active")
        except Exception as exc:
            print(f"[Chat] 项目解析失败: {exc}")
            return ""
        best_name_match = ""
        best_name_len = 0
        for project in projects:
            pid = getattr(project, "project_id", "")
            if not pid:
                continue
            if pid in message:
                return pid
            if len(pid) >= 8 and pid[:8] in message:
                return pid
            name = (getattr(project, "name", "") or "").strip()
            if len(name) >= 4 and name in message and len(name) > best_name_len:
                best_name_match = pid
                best_name_len = len(name)
        return best_name_match


    def _action_create_project(self, message: str) -> str:
        """从对话创建项目 (BUG-026: 秘书幻觉修复配套)。"""
        pm = getattr(self.owner.controller, "project_manager", None)
        if pm is None:
            return "项目创建失败: Secretary 未激活或项目管理器不可用"

        # 复用 LLM 解析提取项目名称和描述
        info = self.owner._parse_task_from_message(message)
        name = info.get("name", "")
        description = info.get("description", "")

        if not name or len(description) < 5:
            return (
                "请提供更详细的项目信息, 例如:\n"
                "「创建一个项目: 股票自动交易系统, 预算10美元」"
            )

        try:
            project = pm.create_project(
                name=name,
                description=description,
                budget_limit_usd=10.0,
                allowed_models=[],
                routing_strategy="balanced",
                workspace_base="",
            )
            return (
                f"✅ 项目已创建!\n"
                f"- 项目ID: {project.project_id}\n"
                f"- 名称: {name}\n"
                f"- 描述: {description[:80]}"
            )
        except Exception as e:
            return f"项目创建失败: {e}"


    def _action_edit_task_graph(self, message: str) -> str:
        """自然语言 DAG 编辑 (F4.3, iter-51): Boss 口述修改任务图结构。

        支持指令: 加一步/新增步骤 X / 删除(跳过)步骤 X / 加依赖 X→Y /
        删依赖 X→Y; 任务用 task-xxx ID 或名称定位。
        流程: 定位任务 → 读图 → LLM 解析编辑指令 → 应用 (环检测回滚) → 落盘。
        防幻觉: 真实执行编辑并返回落盘结果, 失败时明确报错不虚报。
        """
        import re as _re

        # 1. 定位任务 (task-xxx ID 优先, 其次任务名匹配)
        task = None
        m = _re.search(r"task-[0-9a-fA-F]{6,}", message)
        if m:
            task = self.owner.controller.db.get_task(m.group(0))
        if not task:
            for t in self.owner.controller.db.list_tasks(limit=50):
                if t.name and t.name in message:
                    task = t
                    break
        if not task:
            return "未找到目标任务, 请在指令中带上任务 ID (task-xxx) 或任务名称"

        # 2. 读取当前图
        graph = self.owner.controller.get_task_graph_data(task.task_id)
        if not graph:
            return f"任务 '{task.name}' 暂无 DAG 图数据, 无法编辑"

        # 3. LLM 解析编辑指令
        edit = self.owner._parse_graph_edit(message, graph)
        if not edit or not edit.get("op"):
            return ("无法解析图编辑指令, 请使用:\n"
                    "- 加一步: 步骤名\n- 删除步骤 步骤名\n"
                    "- 加依赖: 步骤A → 步骤B")

        return self._apply_graph_edit(task, graph, edit)

    def _apply_graph_edit(self, task: Any, graph: dict, edit: dict) -> str:
        """Apply validated graph operations and persist the actual result."""
        import uuid as _uuid
        from .protocol import SubTask
        from .task import TaskDAG
        # 4. 应用编辑 (TaskDAG 自带环检测回滚)
        try:
            dag = TaskDAG.from_graph_json(graph)
            op = edit.get("op", "")
            if op == "add_node":
                name = (edit.get("node_name") or "").strip()
                if not name:
                    return "新增步骤需要名称, 例如: 加一步: 发布验收"
                st = SubTask(
                    subtask_id=f"st-{_uuid.uuid4().hex[:10]}",
                    parent_task_id=task.task_id,
                    name=name,
                    description=edit.get("description") or name,
                )
                if not dag.add_node(st):
                    return f"新增步骤 '{name}' 失败 (节点冲突或产生环)"
            elif op == "remove_node":
                sid = _find_node_by_name(dag, edit.get("node_name", ""))
                if not sid:
                    return f"未找到步骤 '{edit.get('node_name', '')}'"
                dag.remove_node(sid)
            elif op == "add_edge":
                src = _find_node_by_name(dag, edit.get("source", ""))
                tgt = _find_node_by_name(dag, edit.get("target", ""))
                if not src or not tgt:
                    return "加依赖需要存在的前置/后置步骤名, 例如: 加依赖: A → B"
                if not dag.add_edge(src, tgt):
                    return f"加依赖 '{src} → {tgt}' 失败 (重复边或产生环)"
            elif op == "remove_edge":
                src = _find_node_by_name(dag, edit.get("source", ""))
                tgt = _find_node_by_name(dag, edit.get("target", ""))
                if not src or not tgt:
                    return "删依赖需要存在的前置/后置步骤名"
                if not dag.remove_edge(src, tgt):
                    return f"未找到依赖边 '{src} → {tgt}'"
            else:
                return f"不支持的编辑操作: {op}"
        except Exception as e:
            return f"图编辑应用失败: {e}"

        # 5. 落盘并返回真实结果
        result = self.owner.controller.update_task_graph(
            task.task_id, dag.to_graph_json())
        if result.get("ok"):
            return (f"✅ 图编辑完成: {result.get('message')}\n"
                    f"任务 '{task.name}' ({task.task_id}) 已更新")
        return f"图编辑保存失败: {result.get('message', '未知错误')}"


    def _parse_graph_edit(self, message: str, graph: dict) -> dict:
        """LLM 解析自然语言图编辑指令为结构化操作 (失败返回空 dict)。"""
        import json as _json
        node_names = [n.get("name", "") for n in graph.get("nodes", [])]
        prompt = (
            "解析用户的 DAG 图编辑指令, 返回严格 JSON: "
            "{\"op\": \"add_node|remove_node|add_edge|remove_edge\", "
            "\"node_name\": \"步骤名\", \"source\": \"前置步骤名\", "
            "\"target\": \"后置步骤名\", \"description\": \"步骤描述\"}。\n"
            "op 判定: 加/新增/增加步骤 → add_node; 删除/跳过/去掉步骤 → remove_node; "
            "加依赖/先做A再做B → add_edge; 删依赖/解除依赖 → remove_edge。\n"
            f"现有步骤: {node_names}\n"
            "无法解析返回 {\"op\": \"\"}。\n\n"
            f"用户指令: {message}"
        )
        resp = self.owner.runtime._call_llm_with_routing(
            prompt,
            {"_model_preference": "", "_fallback_models": []},
        )
        content = resp.get("content", "")
        try:
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                return _json.loads(content[start:end])
        except Exception:
            pass
        return {}


    def _action_query_status(self) -> str:
        """查询综合状态。"""
        context = self.owner._build_status_context()
        return context


    def _action_query_hosts(self) -> str:
        """查询主机列表。"""
        hosts = self.owner.controller.db.list_hosts()
        if not hosts:
            return "暂无注册主机"
        lines = []
        for h in hosts:
            status = "在线" if h.online else "离线"
            name = h.device_name or h.hostname or h.device_id[:8]
            lines.append(f"  {status} | {name} | {h.ip or '-'} | 评级: {h.rating or '-'}")
        return f"主机列表 ({len(hosts)} 台):\n" + "\n".join(lines)


    def _action_query_tasks(self) -> str:
        """查询任务列表。"""
        if not self.owner.controller.secretary_active:
            return "Secretary 未激活, 无法查询任务"
        tasks = self.owner.controller.db.list_tasks(limit=10)
        if not tasks:
            return "暂无任务"
        lines = []
        for t in tasks:
            pm_info = f" PM: {t.pm_agent_id[:8]}" if t.pm_agent_id else ""
            lines.append(f"  [{t.status}] {t.name}{pm_info}")
        return f"最近任务 ({len(tasks)} 个):\n" + "\n".join(lines)


    def _action_query_progress(self) -> str:
        """查询 PM Agent 进度 (优化11: LLM 智能摘要)。"""
        if not self.owner.controller.secretary_active:
            return "Secretary 未激活"
        try:
            pms = self.owner.controller.db.list_pm_agents()
            if not pms:
                return "暂无 PM Agent"

            # 收集原始数据
            raw_lines = []
            for pm in pms:
                reports = self.owner.controller.db.get_progress_reports(pm.pm_id, limit=3)
                latest_progress = reports[0]["progress"] if reports else 0.0
                latest_msg = reports[0]["message"] if reports else ""
                raw_lines.append(
                    f"- {pm.agent_name} | 状态: {pm.status} | "
                    f"进度: {latest_progress*100:.0f}% | "
                    f"模式: {pm.collaboration_mode or '-'} | "
                    f"最新消息: {latest_msg[:100]}"
                )

            raw_text = "\n".join(raw_lines)

            # 优化11: 用 LLM 生成自然语言摘要
            summary_prompt = (
                "你是 LAN Mesh 工作站的秘书。请用简洁的中文 (2-3句话) 向 Boss 汇报以下 PM Agent 进度。\n"
                "要求: 突出整体进展、异常情况和预计剩余时间。不要逐条列出, 用自然段落。\n\n"
                f"PM Agent 数据:\n{raw_text}"
            )
            try:
                resp = self.owner.runtime._call_llm_with_routing(
                    summary_prompt,
                    {"_model_preference": "", "_fallback_models": []},
                )
                summary = resp.get("content", "").strip()
                if summary and len(summary) > 10:
                    return f"📊 进度汇报:\n{summary}\n\n--- 原始数据 ---\n{raw_text}"
            except Exception:
                pass

            # LLM 失败时回退到原始格式
            return f"PM Agent 进度 ({len(pms)} 个):\n{raw_text}"
        except Exception as e:
            return f"进度查询失败: {e}"


    def _action_activate_secretary(self) -> str:
        """激活 Secretary。"""
        if self.owner.controller.secretary_active:
            return "Secretary 已处于激活状态"
        result = self.owner.controller.activate_secretary()
        return result.get("message", "Secretary 激活结果未知")


    def _action_deactivate_secretary(self) -> str:
        """停用 Secretary。"""
        if not self.owner.controller.secretary_active:
            return "Secretary 当前未激活"
        result = self.owner.controller.deactivate_secretary()
        return result.get("message", "Secretary 停用结果未知")


    def _action_cancel_task(self, message: str) -> str:
        """取消指定任务。"""
        task_id = self.owner._extract_task_id_from_message(message)
        if not task_id:
            # 尝试从消息中找任务名称
            tasks = self.owner.controller.db.list_tasks(status="running", limit=10)
            if not tasks:
                return "当前没有运行中的任务可以取消"
            # 模糊匹配任务名
            for t in tasks:
                if t.name and t.name in message:
                    task_id = t.task_id
                    break
            if not task_id:
                task_list = "\n".join([f"  - [{t.status}] {t.name} (ID: {t.task_id[:12]})" for t in tasks[:5]])
                return f"请指定要取消的任务。当前运行中的任务:\n{task_list}\n\n示例: 取消任务 {tasks[0].name if tasks else 'XXX'}"

        result = self.owner.controller.cancel_task(task_id)
        if result.get("ok"):
            return f"✅ {result.get('message', '任务已取消')}"
        return f"❌ 取消失败: {result.get('message', '未知错误')}"


    def _action_pause_task(self, message: str) -> str:
        """暂停指定任务。"""
        task_id = self.owner._extract_task_id_from_message(message)
        if not task_id:
            tasks = self.owner.controller.db.list_tasks(status="running", limit=10)
            if not tasks:
                return "当前没有运行中的任务可以暂停"
            for t in tasks:
                if t.name and t.name in message:
                    task_id = t.task_id
                    break
            if not task_id:
                task_list = "\n".join([f"  - [{t.status}] {t.name} (ID: {t.task_id[:12]})" for t in tasks[:5]])
                return f"请指定要暂停的任务。当前运行中的任务:\n{task_list}\n\n示例: 暂停任务 {tasks[0].name if tasks else 'XXX'}"

        result = self.owner.controller.pause_task(task_id)
        if result.get("ok"):
            return f"⏸️ {result.get('message', '任务已暂停')}"
        return f"❌ 暂停失败: {result.get('message', '未知错误')}"


    def _extract_task_id_from_message(self, message: str) -> str:
        """从消息中提取 task_id (格式: task-xxxx)。"""
        import re
        match = re.search(r'task-[a-f0-9]{12}', message, re.IGNORECASE)
        return match.group(0) if match else ""


    def _action_respond_to_pm(self, message: str) -> str:
        """向等待决策的 PM Agent 回复 Boss 的决策。

        支持两种方式:
        1. 「告诉PM PM-abc 用方案A」→ 精确指定 PM
        2. 如果只有一个 PM 在等待决策, 可直接用「用方案A」
        """
        pm_id = self.owner._extract_pm_id_from_message(message)

        # 如果没有明确指定 PM, 使用最近的 awaiting PM
        if not pm_id:
            # 刷新 _last_awaiting_pm
            try:
                pm_agents = self.owner.controller.db.list_pm_agents()
                awaiting = [p for p in pm_agents if p.status == "awaiting_input"]
                if awaiting:
                    pm_id = awaiting[-1].pm_id
                    self.owner._last_awaiting_pm = pm_id
            except Exception:
                pass

        if not pm_id:
            pm_id = self.owner._last_awaiting_pm

        if not pm_id:
            return "当前没有 PM Agent 在等待您的决策。"

        # 提取回复内容 (去掉"告诉PM xxx"前缀)
        response_text = message
        for prefix in ["告诉PM", "告知PM", "回复PM"]:
            if prefix in message:
                # 找到前缀后的内容
                idx = message.index(prefix) + len(prefix)
                # 跳过 PM ID 部分
                after_prefix = message[idx:].strip()
                # 如果以 PM ID 开头, 去掉它
                if after_prefix.startswith("PM-") or after_prefix.startswith("pm-"):
                    parts = after_prefix.split(None, 1)
                    response_text = parts[1] if len(parts) > 1 else ""
                else:
                    response_text = after_prefix
                break

        if not response_text.strip():
            return "请提供您的决策内容。示例: 告诉PM PM-abc 用方案A"

        # 获取 PM 信息
        pm = self.owner.controller.db.get_pm_agent(pm_id)
        if not pm:
            return f"PM Agent {pm_id[:12]} 不存在"

        result = self.owner.controller.inject_input_to_pm(pm_id, {
            "response": response_text,
            "choice": response_text,
        })

        if result.get("ok"):
            return f"✅ 已将您的决策发送给 {pm.agent_name}: {response_text[:100]}"
        return f"❌ 发送失败: {result.get('message', '未知错误')}"


    def _extract_pm_id_from_message(self, message: str) -> str:
        """从消息中提取 pm_id (格式: pm-xxxx 或 PM-xxxx)。"""
        import re
        match = re.search(r'[Pp][Mm]-[a-f0-9]{12}', message)
        return match.group(0) if match else ""


    def _action_accept_delivery(self, message: str) -> str:
        """验收最近交付的任务。"""
        task = self.owner._find_latest_delivered_task()
        if not task:
            return "当前没有待验收的任务交付物。"

        task_id = task.task_id
        delivery = task.output_data.get("_delivery", {})
        delivery["accepted"] = True
        delivery["accepted_at"] = time.time()
        task.output_data["_delivery"] = delivery
        task.status = "completed"
        self.owner.controller.db.save_task(task)

        review = delivery.get("acceptance_review") or {}
        review_note = ""
        if review.get("verdict") == "pass":
            review_note = "\n🛡️ 验收自检: 全部达成。"
        elif review.get("verdict") == "risk":
            unmet = "\n".join(f"- {item}" for item in review.get("unmet", [])[:5])
            review_note = f"\n⚠️ 验收自检有未达成项:\n{unmet}"
        return (
            f"✅ 已验收任务「{task.name}」的交付物。"
            f"\n任务状态已更新为 completed。{review_note}"
        )


    def _action_reject_delivery(self, message: str) -> str:
        """退回最近交付的任务, 附带修改意见。"""
        task = self.owner._find_latest_delivered_task()
        if not task:
            return "当前没有待退回的任务交付物。"

        task_id = task.task_id
        delivery = task.output_data.get("_delivery", {})
        delivery["accepted"] = False
        delivery["rejected_at"] = time.time()

        # 提取退回原因
        reason = message
        for kw in ["退回", "重新做"]:
            if kw in message:
                idx = message.index(kw) + len(kw)
                reason = message[idx:].strip()
                break
        if not reason:
            reason = "Boss 未提供具体修改意见"

        delivery["reject_reason"] = reason
        task.output_data["_delivery"] = delivery
        task.status = "running"  # 重新运行
        self.owner.controller.db.save_task(task)

        # 通知 PM Agent (如果还在运行)
        pm_id = task.pm_agent_id
        if pm_id:
            self.owner.controller.inject_input_to_pm(pm_id, {
                "response": f"[退回] {reason}",
                "choice": "reject",
                "task_name": task.name,
            })

        return (
            f"↩️ 已退回任务「{task.name}」的交付物。\n"
            f"退回原因: {reason}\n"
            f"PM Agent 将收到修改意见并重新执行。"
        )


    def _find_latest_delivered_task(self):
        """查找最近的已交付但未验收的任务。"""
        try:
            tasks = self.owner.controller.db.list_tasks(limit=20)
            for t in tasks:
                if t.output_data and "_delivery" in t.output_data:
                    delivery = t.output_data["_delivery"]
                    if delivery.get("accepted") is None:  # 未验收
                        return t
        except Exception:
            pass
        return None


    def _get_task_memory_hint(self, task_name: str, task_desc: str) -> str:
        """优化14: 查询任务记忆, 生成历史参考提示。

        当 Boss 提交新任务时, 查询同类型任务的历史记录:
        - 成功率和平均耗时
        - 推荐的协作模式
        - 常见错误预警
        """
        try:
            db = self.owner.controller.db
            # 推断任务类型 (复用 PM Agent 的逻辑)
            from .pm_agent import ProjectManagerAgent
            task_type = ProjectManagerAgent._infer_task_type(task_name, task_desc)

            stats = db.get_task_memory_stats(task_type=task_type)
            if stats.get("total", 0) == 0:
                return ""

            hints = []
            hints.append(f"同类任务历史 {stats['total']} 次, 成功率 {stats['success_rate']*100:.0f}%")
            if stats.get("avg_duration", 0) > 0:
                mins = stats["avg_duration"] / 60
                hints.append(f"平均耗时 {mins:.1f} 分钟")
            if stats.get("recommended_mode"):
                hints.append(f"推荐模式: {stats['recommended_mode']}")
            if stats.get("common_errors"):
                top_error = stats["common_errors"][0][0]
                hints.append(f"注意: 历史常见错误「{top_error[:50]}」")

            return "; ".join(hints)
        except Exception:
            return ""

