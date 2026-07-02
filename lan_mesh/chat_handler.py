"""
秘书聊天处理器 — Web 端对话接口

职责:
1. 接收用户 (Boss) 通过 Web 聊天窗口发送的消息
2. 构建工作站状态摘要作为 system prompt 上下文
3. 调用 LLM 生成回复
4. 解析回复中的操作意图并执行
5. 返回回复 + 操作结果

聊天历史存储在内存中 (列表), 重启丢失。后续可持久化到 DB。
"""
import time
from typing import Optional


# ── 操作意图关键词映射 ──────────────────────────────────────────

_ACTION_KEYWORDS = {
    "提交任务": "submit_task",
    "提交一个": "submit_task",
    "创建任务": "submit_task",
    "新任务": "submit_task",
    "启动秘书": "activate_secretary",
    "激活秘书": "activate_secretary",
    "停止秘书": "deactivate_secretary",
    "停用秘书": "deactivate_secretary",
    "状态": "query_status",
    "进度": "query_progress",
    "查看主机": "query_hosts",
    "主机列表": "query_hosts",
    "查看任务": "query_tasks",
    "任务列表": "query_tasks",
}


class ChatHandler:
    """秘书聊天处理器。

    在 Secretary 激活后由 StationController 创建,
    通过 station_api 的 /api/secretary/chat 端点对外服务。
    """

    def __init__(self, runtime, controller):
        """初始化聊天处理器。

        Args:
            runtime: AgentRuntime 实例, 用于 LLM 调用
            controller: StationController 实例, 用于查询工作站状态和执行操作
        """
        self.runtime = runtime
        self.controller = controller
        self._history: list[dict] = []  # 内存中的聊天历史
        self._max_history = 50          # 保留最近 50 条对话

    # ── 公开接口 ──────────────────────────────────────────────────

    def chat(self, message: str, history: Optional[list] = None) -> dict:
        """处理用户消息, 返回回复。

        Args:
            message: 用户输入文本
            history: 可选的外部历史 (Web 端传入), 为 None 则使用内部历史

        Returns:
            {"reply": str, "action_taken": str, "timestamp": float}
        """
        # 使用外部历史或内部历史
        chat_history = history if history is not None else self._history

        # 1. 构建状态上下文
        status_context = self._build_status_context()

        # 2. 构建 system prompt
        system_prompt = self._build_system_prompt(status_context)

        # 3. 拼接对话历史 + 用户消息
        prompt = self._build_prompt(message, chat_history)

        # 4. 调用 LLM (优先使用模型路由器选择模型)
        model_pref = ""
        fallback_models = []
        if self.controller.model_router:
            try:
                routing = self.controller.model_router.route(message, skill="document_summary")
                model_pref = routing.selected_model
                fallback_models = routing.fallback_chain
            except Exception:
                pass

        resp = self.runtime._call_llm_with_routing(
            prompt,
            {"_model_preference": model_pref, "_fallback_models": fallback_models},
        )
        reply_text = resp.get("content", "[LLM 调用失败]")

        # 5. 检测操作意图
        action_taken = ""
        action_result = ""
        action = self._detect_action(message)
        if action:
            action_result = self._execute_action(action, message)
            if action_result:
                reply_text += f"\n\n📋 **操作结果**: {action_result}"
                action_taken = action

        # 6. 保存到内部历史
        now = time.time()
        self._history.append({"role": "user", "content": message, "timestamp": now})
        self._history.append({"role": "assistant", "content": reply_text, "timestamp": now})
        if len(self._history) > self._max_history * 2:
            self._history = self._history[-(self._max_history * 2):]

        return {
            "reply": reply_text,
            "action_taken": action_taken,
            "timestamp": now,
        }

    def get_history(self, limit: int = 50) -> list[dict]:
        """返回最近的聊天历史。"""
        return self._history[-limit:]

    def clear_history(self):
        """清空聊天历史。"""
        self._history.clear()

    # ── 内部方法 ──────────────────────────────────────────────────

    def _build_status_context(self) -> str:
        """从 controller.db 获取工作站状态, 拼接为文本摘要。"""
        lines = []

        try:
            # 主机状态
            hosts = self.controller.db.list_hosts()
            online_hosts = [h for h in hosts if h.online]
            lines.append(f"## 工作站状态")
            lines.append(f"- 在线主机: {len(online_hosts)}/{len(hosts)} 台")
            if online_hosts:
                host_names = [h.device_name or h.hostname or h.device_id[:8] for h in online_hosts[:5]]
                lines.append(f"- 在线主机名: {', '.join(host_names)}")

            # Secretary 状态
            secretary_status = "已激活" if self.controller.secretary_active else "未激活"
            lines.append(f"- Secretary: {secretary_status}")

            # PM Agent 状态
            try:
                pm_agents = self.controller.db.list_pm_agents()
                active_pms = [p for p in pm_agents if p.status in ("planning", "executing", "monitoring")]
                lines.append(f"- 活跃 PM Agent: {len(active_pms)} 个")
                if active_pms:
                    for pm in active_pms[:3]:
                        lines.append(f"  - {pm.agent_name} [{pm.status}] 任务: {pm.task_id}")
            except Exception:
                lines.append("- PM Agent: 数据不可用")

            # 任务状态
            try:
                tasks = self.controller.db.list_tasks(limit=10)
                running_tasks = [t for t in tasks if t.status in ("running", "pending")]
                lines.append(f"- 进行中任务: {len(running_tasks)} 个")
                if running_tasks:
                    for t in running_tasks[:3]:
                        lines.append(f"  - {t.name} [{t.status}]")
            except Exception:
                lines.append("- 任务: 数据不可用")

            # 团队状态
            try:
                teams = self.controller.db.list_teams()
                active_teams = [t for t in teams if t.status in ("active", "pending")]
                lines.append(f"- Agent 团队: {len(active_teams)} 个")
            except Exception:
                pass

        except Exception as e:
            lines.append(f"[状态获取异常: {e}]")

        return "\n".join(lines)

    def _build_system_prompt(self, status_context: str) -> str:
        """构建 LLM system prompt。"""
        return (
            "你是 LAN Mesh 工作站的秘书 AI 助手。你负责接收 Boss 的指令, "
            "回答关于工作站状态的问题, 并协助管理任务和 Agent 团队。\n\n"
            "你可以帮助 Boss:\n"
            "- 查看工作站状态 (在线主机、任务、PM Agent、团队)\n"
            "- 提交新任务 (会自动分配 PM Agent 接管)\n"
            "- 激活/停用 Secretary 模式\n"
            "- 查询任务进度\n\n"
            "回复要求:\n"
            "- 简洁明了, 使用中文\n"
            "- 如果用户想提交任务, 告诉他们可以在 Web 端任务管理 Tab 提交, 或描述任务内容你会协助\n"
            "- 如果用户想查看状态, 基于下方实时数据回答\n\n"
            f"## 当前工作站实时状态\n{status_context}"
        )

    def _build_prompt(self, message: str, history: list) -> str:
        """拼接对话历史 + 用户消息为 prompt。"""
        if not history:
            return message

        # 将历史对话格式化为文本
        lines = []
        for msg in history[-20:]:  # 最近 20 条
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if role == "user":
                lines.append(f"Boss: {content}")
            else:
                lines.append(f"秘书: {content}")

        lines.append(f"Boss: {message}")
        return "\n".join(lines)

    def _detect_action(self, message: str) -> str:
        """检测用户消息中的操作意图。

        Returns:
            操作类型字符串, 无意图则返回空字符串
        """
        msg_lower = message.lower()
        for keyword, action in _ACTION_KEYWORDS.items():
            if keyword in message or keyword in msg_lower:
                return action
        return ""

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
                return self._action_query_status()
            elif action == "query_hosts":
                return self._action_query_hosts()
            elif action == "query_tasks":
                return self._action_query_tasks()
            elif action == "query_progress":
                return self._action_query_progress()
            elif action == "activate_secretary":
                return self._action_activate_secretary()
            elif action == "deactivate_secretary":
                return self._action_deactivate_secretary()
            elif action == "submit_task":
                # 提交任务需要通过 Web 端任务管理 Tab, 这里只给出提示
                return "请在 Web 端「任务管理」Tab 中提交任务, 或描述任务名称和内容, 我会协助您填写。"
            return ""
        except Exception as e:
            return f"操作执行失败: {e}"

    # ── 操作实现 ──────────────────────────────────────────────────

    def _action_query_status(self) -> str:
        """查询综合状态。"""
        context = self._build_status_context()
        return context

    def _action_query_hosts(self) -> str:
        """查询主机列表。"""
        hosts = self.controller.db.list_hosts()
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
        if not self.controller.secretary_active:
            return "Secretary 未激活, 无法查询任务"
        tasks = self.controller.db.list_tasks(limit=10)
        if not tasks:
            return "暂无任务"
        lines = []
        for t in tasks:
            pm_info = f" PM: {t.pm_agent_id[:8]}" if t.pm_agent_id else ""
            lines.append(f"  [{t.status}] {t.name}{pm_info}")
        return f"最近任务 ({len(tasks)} 个):\n" + "\n".join(lines)

    def _action_query_progress(self) -> str:
        """查询 PM Agent 进度。"""
        if not self.controller.secretary_active:
            return "Secretary 未激活"
        try:
            pms = self.controller.db.list_pm_agents()
            if not pms:
                return "暂无 PM Agent"
            lines = []
            for pm in pms:
                reports = self.controller.db.get_progress_reports(pm.pm_id, limit=3)
                latest_progress = reports[0]["progress"] if reports else 0.0
                lines.append(
                    f"  {pm.agent_name} [{pm.status}] "
                    f"进度: {latest_progress*100:.0f}% "
                    f"模式: {pm.collaboration_mode or '-'}"
                )
            return f"PM Agent 进度 ({len(pms)} 个):\n" + "\n".join(lines)
        except Exception as e:
            return f"进度查询失败: {e}"

    def _action_activate_secretary(self) -> str:
        """激活 Secretary。"""
        if self.controller.secretary_active:
            return "Secretary 已处于激活状态"
        result = self.controller.activate_secretary()
        return result.get("message", "Secretary 激活结果未知")

    def _action_deactivate_secretary(self) -> str:
        """停用 Secretary。"""
        if not self.controller.secretary_active:
            return "Secretary 当前未激活"
        result = self.controller.deactivate_secretary()
        return result.get("message", "Secretary 停用结果未知")
