"""
Station PM 路由 — PM Agent 管理/进度上报/子任务同步/团队 (P1 #2 拆分产物)

Secretary 激活后可用:
  - PM Agent 列表/详情/团队/进度查询
  - PM 状态上报 (awaiting_input 双写/escalated 升级广播)
  - PM 进度上报 (完成/失败时同步任务状态)
  - PM 子任务同步 (plan decomposition → 任务 subtasks)
  - 团队列表/详情
"""
import time
import uuid as _uuid

from fastapi import APIRouter, HTTPException

from .logger import get_logger
from .station_routes_common import _broadcast, check_secretary

logger = get_logger("station_api")


def build_pm_routes(controller) -> APIRouter:
    """Secretary PM 管理域路由。"""
    router = APIRouter()

    # 便捷别名
    db = controller.db
    state = controller.state

    # ── PM Agent 管理 ──

    @router.get("/api/pm")
    async def list_pm_agents(status: str = None):
        """列出所有 PM Agent 及其状态。"""
        check_secretary(controller)
        agents = db.list_pm_agents(status=status)
        return {
            "pm_agents": [a.to_dict() if hasattr(a, 'to_dict') else a for a in agents],
            "total": len(agents),
        }

    @router.get("/api/pm/{pm_id}")
    async def get_pm_agent(pm_id: str):
        """PM Agent 详情 (含 team_structure)。"""
        check_secretary(controller)
        pm = db.get_pm_agent(pm_id)
        if not pm:
            raise HTTPException(status_code=404, detail="PM Agent 不存在")
        result = pm.to_dict() if hasattr(pm, 'to_dict') else pm
        # 附加团队信息
        teams = db.get_teams_by_pm(pm_id)
        result["teams"] = [t.to_dict() if hasattr(t, 'to_dict') else t for t in teams]
        return result

    @router.post("/api/pm/{pm_id}/snapshot")
    async def save_pm_snapshot(pm_id: str, payload: dict):
        """iter-53: PM 上报执行态快照 (断点恢复数据源, Worker 调用)。"""
        check_secretary(controller)
        import json as _json
        task_id = payload.get("task_id", "")
        phase = payload.get("phase", "")
        state = payload.get("state", {})
        state_json = state if isinstance(state, str) else _json.dumps(state, ensure_ascii=False)
        db.save_pm_snapshot(pm_id, task_id, phase, state_json)
        return {"ok": True, "pm_id": pm_id, "phase": phase}

    @router.get("/api/pm/{pm_id}/snapshot")
    async def get_pm_snapshot(pm_id: str):
        """iter-53: 查询 PM 执行态快照 (诊断/恢复前探视)。"""
        check_secretary(controller)
        snap = db.get_pm_snapshot(pm_id)
        if not snap:
            raise HTTPException(status_code=404, detail="快照不存在")
        return {
            "pm_id": snap["pm_id"], "task_id": snap["task_id"],
            "phase": snap["phase"], "updated_at": snap["updated_at"],
        }

    @router.delete("/api/pm/{pm_id}/snapshot")
    async def delete_pm_snapshot(pm_id: str):
        """iter-53: 删除 PM 执行态快照 (任务终结时 PM 调用清理)。"""
        check_secretary(controller)
        db.delete_pm_snapshot(pm_id)
        return {"ok": True, "pm_id": pm_id}

    @router.get("/api/pm/{pm_id}/teams")
    async def get_pm_teams(pm_id: str):
        """PM 下属团队列表。"""
        check_secretary(controller)
        teams = db.get_teams_by_pm(pm_id)
        return {
            "teams": [t.to_dict() if hasattr(t, 'to_dict') else t for t in teams],
            "total": len(teams),
        }

    @router.get("/api/pm/{pm_id}/progress")
    async def get_pm_progress(pm_id: str, limit: int = 50):
        """PM 进度报告列表。"""
        check_secretary(controller)
        reports = db.get_progress_reports(pm_id, limit=limit)
        return {"reports": reports, "total": len(reports)}

    @router.post("/api/pm/{pm_id}/status")
    async def update_pm_status(pm_id: str, payload: dict):
        """PM 上报状态变更 (Worker 调用)。"""
        check_secretary(controller)
        pm = db.get_pm_agent(pm_id)
        if not pm:
            raise HTTPException(status_code=404, detail="PM Agent 不存在")

        status = payload.get("status", "")
        team_structure = payload.get("team_structure")
        task_list = payload.get("task_list")
        collaboration_mode = payload.get("collaboration_mode")

        db.update_pm_status(
            pm_id, status,
            team_structure=team_structure,
            task_list=task_list,
            collaboration_mode=collaboration_mode,
        )

        # 优化7: awaiting_input 状态时附带澄清问题
        broadcast_data = {
            "pm_id": pm_id, "status": status,
            "collaboration_mode": collaboration_mode or "",
        }
        clarification_question = payload.get("clarification_question", "")
        if clarification_question:
            broadcast_data["clarification_question"] = clarification_question
            broadcast_data["clarification_options"] = payload.get("clarification_options", [])

        # 方案C: awaiting_input 时双写 L1 通知 + L2 线程消息
        if status == "awaiting_input" and clarification_question:
            ch = getattr(controller, 'chat_handler', None)
            if ch:
                ch.notify_pm_clarification(
                    pm_id, clarification_question,
                    options=payload.get("clarification_options", []),
                )
        elif status != "awaiting_input":
            # 非 awaiting 状态时同步更新线程状态
            ch = getattr(controller, 'chat_handler', None)
            if ch:
                ch.update_pm_thread_status(pm_id, status)

        # 优化10: escalated 状态时附带升级详情
        escalation = payload.get("escalation")
        if escalation:
            broadcast_data["escalation"] = escalation

        await _broadcast(state, "pm_status_change", broadcast_data)

        # 优化: awaiting_input 时 Bot 推送 (带 options 触发 Inline Keyboard)
        if status == "awaiting_input" and clarification_question:
            controller.bot_gateway.notify("pm_awaiting_input", {
                "pm_id": pm_id,
                "question": clarification_question,
                "options": payload.get("clarification_options", []),
            })

        # 优化10: escalated 时额外广播 + Bot 推送
        if status == "escalated" and escalation:
            await _broadcast(state, "task_escalated", escalation)
            controller.bot_gateway.notify("task_escalated", {
                "task_name": escalation.get("task_name", ""),
                "failed_subtask": escalation.get("failed_subtask", ""),
                "error": escalation.get("error", "")[:200],
            })
        return {"ok": True, "pm_id": pm_id, "status": status}

    @router.post("/api/pm/{pm_id}/progress")
    async def receive_pm_progress(pm_id: str, payload: dict):
        """PM 上报进度 (Worker 调用)。"""
        check_secretary(controller)
        from .protocol import ProgressReport
        report = ProgressReport(
            report_id=f"rpt-{_uuid.uuid4().hex[:8]}",
            pm_id=pm_id,
            reporter_id=payload.get("reporter_id", ""),
            reporter_type=payload.get("reporter_type", "pm"),
            task_name=payload.get("task_name", ""),
            progress=payload.get("progress", 0.0),
            status=payload.get("status", "in_progress"),
            message=payload.get("message", ""),
            timestamp=payload.get("timestamp", time.time()),
        )
        db.save_progress_report(report)

        # ── 关键: PM 完成/失败时同步更新任务状态 ──
        pm_status = payload.get("status", "")
        if pm_status in ("completed", "failed") and payload.get("reporter_type") == "pm":
            # 通过 pm_agent_id 查找对应任务并更新状态
            try:
                conn = db._get_conn()
                row = conn.execute(
                    "SELECT task_id FROM tasks WHERE pm_agent_id = ? AND status = 'running'",
                    (pm_id,)
                ).fetchone()
                if row:
                    task_id = row["task_id"]
                    new_status = "completed" if pm_status == "completed" else "failed"
                    db.update_task_status(task_id, new_status)
                    logger.info("任务 %s 状态同步: running → %s", task_id[:16], new_status)
            except Exception as e:
                logger.error("任务状态同步失败: %s", e)

        await _broadcast(state, "progress_report", {
            "pm_id": pm_id,
            "reporter_id": report.reporter_id,
            "progress": report.progress,
            "status": report.status,
            "message": report.message,
            "task_name": report.task_name,
        })
        return {"ok": True, "report_id": report.report_id}

    @router.post("/api/pm/{pm_id}/subtasks")
    async def sync_pm_subtasks(pm_id: str, payload: dict):
        """PM 同步子任务状态列表到任务记录 (前端进度实时展示)。

        将 PM 的 plan decomposition + 运行态合并为 SubTask 结构,
        写入对应任务的 subtasks 字段, 并广播 task_updated 事件。
        """
        check_secretary(controller)
        task_id = payload.get("task_id", "")
        incoming = payload.get("subtasks", [])

        # task_id 缺失时通过 pm_agent_id 反查
        if not task_id:
            try:
                conn = db._get_conn()
                row = conn.execute(
                    "SELECT task_id FROM tasks WHERE pm_agent_id = ? ORDER BY created_at DESC LIMIT 1",
                    (pm_id,)
                ).fetchone()
                if row:
                    task_id = row["task_id"]
            except Exception:
                pass
        if not task_id:
            raise HTTPException(status_code=400, detail="缺少 task_id")

        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        # 合并: 保留已有 subtask_id, 按 name 匹配更新状态
        existing = {st.get("name"): st for st in task.subtasks if isinstance(st, dict)}
        merged = []
        for sub in incoming:
            name = sub.get("name", "")
            prev = existing.get(name, {})
            item = {
                "subtask_id": prev.get("subtask_id") or f"sub-{_uuid.uuid4().hex[:8]}",
                "parent_task_id": task_id,
                "name": name,
                "description": sub.get("description", prev.get("description", "")),
                "required_skill": sub.get("required_skill", prev.get("required_skill", "")),
                "depends_on": sub.get("depends_on", prev.get("depends_on", [])),
                "status": sub.get("status", prev.get("status", "pending")),
                "assigned_agent_id": prev.get("assigned_agent_id", ""),
            }
            merged.append(item)

        db.update_task_subtasks(task_id, merged)
        task.subtasks = merged
        await _broadcast(state, "task_updated", task.to_dict())
        return {"ok": True, "task_id": task_id, "subtasks": len(merged)}

    # ── 团队管理 ──

    @router.get("/api/teams")
    async def list_teams(pm_id: str = None):
        """所有团队 (支持按 pm_id 过滤)。"""
        check_secretary(controller)
        teams = db.list_teams(pm_id=pm_id)
        return {
            "teams": [t.to_dict() if hasattr(t, 'to_dict') else t for t in teams],
            "total": len(teams),
        }

    @router.get("/api/teams/{team_id}")
    async def get_team(team_id: str):
        """团队详情 (含 members 嵌套)。"""
        check_secretary(controller)
        team = db.get_team(team_id)
        if not team:
            raise HTTPException(status_code=404, detail="团队不存在")
        return team.to_dict() if hasattr(team, 'to_dict') else team

    return router
