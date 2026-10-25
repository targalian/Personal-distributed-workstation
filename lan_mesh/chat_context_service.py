"""ChatContextService: extracted domain behavior with explicit component ownership."""

from .role_cards import render_secretary_prompt

from .component_service import ComponentService

class ChatContextService(ComponentService):
    """Single domain implementation behind the existing facade."""

    OPERATIONS = ('_build_discussion_context', '_format_discussion_item', '_format_discussion_overview', '_build_status_context', '_build_system_prompt', '_build_prompt')

    def _build_discussion_context(self, discuss_context: dict) -> str:
        """构建优化讨论上下文, 供纯对话模式避免误执行命令。"""
        if not isinstance(discuss_context, dict):
            return ""
        optimizer = getattr(self.owner.controller, "workstation_optimizer", None)
        if optimizer is None:
            return "【优化讨论上下文】优化模块暂不可用, 请基于 Boss 问题继续讨论。"
        topic = str(discuss_context.get("topic", "")).strip()
        try:
            if topic and topic != "__all__":
                item = optimizer.get_item(topic)
                if item:
                    return self.owner._format_discussion_item(item)
                overview = self.owner._format_discussion_overview(optimizer)
                return f"{overview}\n- 指定优化项 {topic} 不存在, 以上为总览。"
            return self.owner._format_discussion_overview(optimizer)
        except Exception as exc:
            print(f"[Chat] 优化讨论上下文获取失败: {exc}", flush=True)
            return "【优化讨论上下文】优化信息暂不可用, 请基于已知内容讨论。"


    def _format_discussion_item(self, item: dict) -> str:
        """格式化单个优化讨论话题。"""
        return (
            "【优化讨论上下文】\n"
            f"- 标题: {item.get('title', '')}\n"
            f"- ID: {item.get('id', '')}\n"
            f"- 来源: {item.get('source', '')}\n"
            f"- 优先级: {item.get('priority', '')}\n"
            f"- 状态: {item.get('status', '')}\n"
            f"- 说明: {item.get('description', '')}\n"
            f"- Boss 补充: {item.get('decision_reply', '')}"
        )


    def _format_discussion_overview(self, optimizer) -> str:
        """格式化优化队列总览讨论话题。"""
        summary = optimizer.summary()
        items = optimizer.list_items(limit=100)
        interesting = [item for item in items if item.get("status") in (
            "waiting_boss", "candidate", "queued", "running")]
        lines = [
            "【优化讨论上下文】",
            f"- 守护状态: {'运行中' if summary.get('guardian_running') else '未运行'}",
            f"- 队列数量: {summary.get('queue_count', 0)}",
            f"- 待 Boss 决策: {summary.get('waiting_boss_count', 0)}",
        ]
        for item in interesting[:10]:
            lines.append(
                f"- {item.get('id', '')} [{item.get('status', '')}] "
                f"{item.get('title', '')}")
        return "\n".join(lines)


    def _build_status_context(self) -> str:
        """从 controller.db 获取工作站状态, 拼接为文本摘要。"""
        lines = []

        try:
            # 主机状态
            hosts = self.owner.controller.db.list_hosts()
            online_hosts = [h for h in hosts if h.online]
            lines.append(f"## 工作站状态")
            lines.append(f"- 在线主机: {len(online_hosts)}/{len(hosts)} 台")
            if online_hosts:
                host_names = [h.device_name or h.hostname or h.device_id[:8] for h in online_hosts[:5]]
                lines.append(f"- 在线主机名: {', '.join(host_names)}")
            # 主机评级分布 (S/A/B/C/D)
            if hosts:
                tiers = {}
                for h in hosts:
                    tier = getattr(h, 'rating_tier', '') or '?'
                    tiers[tier] = tiers.get(tier, 0) + 1
                tier_summary = ", ".join(f"{t}级:{c}台" for t, c in sorted(tiers.items()))
                if tier_summary:
                    lines.append(f"- 主机评级分布: {tier_summary}")

            # Secretary 状态
            secretary_status = "已激活" if self.owner.controller.secretary_active else "未激活"
            lines.append(f"- Secretary: {secretary_status}")

            # 工作站常驻优化状态 (iter-72)
            try:
                optimizer = getattr(self.owner.controller, "workstation_optimizer", None)
                if optimizer:
                    opt_summary = optimizer.summary()
                    lines.append(
                        f"- 工作站优化: 队列 {opt_summary['queue_count']} 个, "
                        f"待决策 {opt_summary['waiting_boss_count']} 个")
            except Exception:
                pass

            self._append_pm_status(lines)

            # 任务状态
            try:
                tasks = self.owner.controller.db.list_tasks(limit=10)
                running_tasks = [t for t in tasks if t.status in ("running", "pending")]
                lines.append(f"- 进行中任务: {len(running_tasks)} 个")
                if running_tasks:
                    for t in running_tasks[:3]:
                        lines.append(f"  - {t.name} [{t.status}]")
            except Exception:
                lines.append("- 任务: 数据不可用")

            # 团队状态
            try:
                teams = self.owner.controller.db.list_teams()
                active_teams = [t for t in teams if t.status in ("active", "pending")]
                lines.append(f"- Agent 团队: {len(active_teams)} 个")
            except Exception:
                pass

            # 任务记忆统计 (历史经验参考)
            try:
                stats = self.owner.controller.db.get_task_memory_stats()
                if stats.get("total", 0) > 0:
                    rate = round(stats["success_rate"] * 100)
                    lines.append(f"- 历史任务记忆: {stats['total']} 条, 成功率 {rate}%")
            except Exception:
                pass

            # 技能库状态
            try:
                skills = self.owner.controller.db.list_skills()
                if skills:
                    lines.append(f"- 技能库: {len(skills)} 个技能")
            except Exception:
                pass

        except Exception as e:
            lines.append(f"[状态获取异常: {e}]")

        return "\n".join(lines)


    def _append_pm_status(self, lines: list[str]) -> None:
        """Append PM decisions and escalation without duplicating state ownership."""
        # PM Agent 状态
        try:
            pm_agents = self.owner.controller.db.list_pm_agents()
            active_pms = [p for p in pm_agents if p.status in ("planning", "executing", "monitoring")]
            lines.append(f"- 活跃 PM Agent: {len(active_pms)} 个")
            if active_pms:
                for pm in active_pms[:3]:
                    lines.append(f"  - {pm.agent_name} [{pm.status}] 任务: {pm.task_id}")
            # 优化7: 高亮等待决策的 PM
            awaiting_pms = [p for p in pm_agents if p.status == "awaiting_input"]
            if awaiting_pms:
                # 记录最近的 awaiting PM (用于后续回复匹配)
                self.owner._last_awaiting_pm = awaiting_pms[-1].pm_id
                lines.append(f"- ⚠️ 等待决策 PM: {len(awaiting_pms)} 个")
                for pm in awaiting_pms[:2]:
                    lines.append(f"  - {pm.agent_name} 正在等待您的决策! (任务: {pm.task_id})")
                    lines.append(f"    请回复「告诉PM {pm.agent_name} <您的决策>」来回复")
            # 优化10: 高亮升级的 PM
            escalated_pms = [p for p in pm_agents if p.status == "escalated"]
            if escalated_pms:
                self.owner._last_awaiting_pm = escalated_pms[-1].pm_id
                lines.append(f"- 🚨 升级告警 PM: {len(escalated_pms)} 个")
                for pm in escalated_pms[:2]:
                    lines.append(f"  - {pm.agent_name} 遇到严重失败, 需要您的决策! (任务: {pm.task_id})")
                    lines.append(f"    可选: 忽略继续 / 简化重试 / 指定主机 / 放弃任务")
                    lines.append(f"    请回复「告诉PM {pm.agent_name} <您的选择>」")
        except Exception:
            lines.append("- PM Agent: 数据不可用")


    def _build_system_prompt(self, status_context: str) -> str:
        """构建 LLM system prompt (M6: 统一从 role_cards 角色卡拼装)。

        包含秘书身份、能力边界、行为约束和实时工作站状态。
        此 prompt 通过 input_data['_system_prompt'] 注入 LLM 调用。
        """
        return render_secretary_prompt(status_context)


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

