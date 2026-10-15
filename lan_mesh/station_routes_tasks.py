"""
Station 任务路由 — Agent 管理/任务生命周期/图结构/交付闭环/任务记忆 (P1 #2 拆分产物)

Secretary 激活后可用 (未激活返回 503):
  - Agent 注册/列表/详情
  - 任务提交 (PM Agent 派发: 本机优先 + 远程回退) / 列表 / 取消 / 暂停 / 删除
  - DAG 图结构 (DB 重建) 与 checkpoint 查询
  - 反向沟通 (PM 注入回复) 与交付闭环 (deliver/accept/reject)
  - 任务记忆 (task_memory) 存储与统计
"""
import time


from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from .protocol import AgentCard
from starlette.concurrency import run_in_threadpool
from .logger import get_logger
from .station_routes_common import _broadcast, check_secretary
from .task_resubmission import resubmit_task, task_with_provenance

logger = get_logger("station_api")


def build_task_routes(controller) -> APIRouter:
    """Secretary 任务域路由。"""
    router = APIRouter()

    # 便捷别名
    db = controller.db
    state = controller.state
    discovery = controller.discovery

    # ── Agent 管理 ──

    @router.post("/api/agents/register")
    async def register_agent(payload: dict):
        check_secretary(controller)
        card = AgentCard.from_dict(payload)
        card.last_seen = time.time()
        dev = discovery.find_device(card.device_id)
        if dev:
            card.ip = dev.get("ip", card.ip)
        db.upsert_agent(card)
        await _broadcast(state, "agent_registered", card.to_dict())
        return {"ok": True, "agent_id": card.agent_id}

    @router.get("/api/agents")
    async def list_agents(status: str = None):
        check_secretary(controller)
        agents = db.list_agents(status=status)
        return {
            "agents": [a.to_dict() for a in agents],
            "total": len(agents),
            "idle": sum(1 for a in agents if a.status == "idle"),
            "busy": sum(1 for a in agents if a.status == "busy"),
        }

    @router.get("/api/agents/{agent_id}")
    async def get_agent(agent_id: str):
        check_secretary(controller)
        agent = db.get_agent(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent 不存在")
        return agent.to_dict()

    # ── 任务管理 ──

    @router.post("/api/tasks")
    async def submit_task(payload: dict):
        """Persist task ownership before starting the PM; share chat submission logic."""
        check_secretary(controller)
        project_manager = controller.project_manager
        project_id = payload.get("project_id", "")
        if project_id and project_manager:
            if not project_manager.check_budget(project_id):
                raise HTTPException(status_code=402, detail="项目预算已起支或已暂停,无法提交任务")

        return await run_in_threadpool(
            controller.submit_task_from_chat,
            name=payload.get("name", ""), description=payload.get("description", ""),
            input_data=payload.get("input_data", {}), created_by=payload.get("created_by", "user"),
            priority=payload.get("priority", "normal"), project_id=project_id)

    @router.get("/api/tasks")
    async def list_tasks(status: str = None, limit: int = 50):
        check_secretary(controller)
        tasks = db.list_tasks(status=status, limit=limit)
        return {"tasks": [task_with_provenance(db, t) for t in tasks], "total": len(tasks)}

    @router.post("/api/tasks/{task_id}/resubmit")
    async def resubmit(task_id: str, payload: dict = None):
        check_secretary(controller)
        acknowledged = (payload or {}).get("acknowledge_side_effects", False)
        if not isinstance(acknowledged, bool):
            raise HTTPException(status_code=422, detail="确认字段必须为布尔值")
        try:
            return await run_in_threadpool(resubmit_task, controller, task_id, acknowledged)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=exc.args[0]) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=402, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        check_secretary(controller)
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task_with_provenance(db, task)

    # ── 优化8: 取消/暂停任务 ──

    @router.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        """取消指定任务及其 PM Agent。"""
        check_secretary(controller)
        result = await run_in_threadpool(controller.cancel_task, task_id)
        if result.get("ok"):
            await _broadcast(state, "task_cancelled", {
                "task_id": task_id, "message": result.get("message", ""),
            })
            return JSONResponse(result, status_code=200 if result.get("confirmed") else 202)
        raise HTTPException(status_code=409, detail=result.get("message", "取消失败"))

    @router.get("/api/tasks/{task_id}/execution-check")
    async def inspect_task_execution(task_id: str):
        check_secretary(controller)
        from .pm_inspection import inspect_task_pm
        try:
            return await run_in_threadpool(inspect_task_pm, controller, task_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="任务不存在")

    @router.post("/api/tasks/{task_id}/pause")
    async def pause_task(task_id: str):
        """暂停指定任务及其 PM Agent。"""
        check_secretary(controller)
        result = await run_in_threadpool(controller.pause_task, task_id)
        if result.get("ok"):
            await _broadcast(state, "task_paused", {
                "task_id": task_id, "message": result.get("message", ""),
            })
            return JSONResponse(result, status_code=200 if result.get("confirmed") else 202)
        raise HTTPException(status_code=409, detail=result.get("message", "暂停失败"))

    @router.get("/api/tasks/{task_id}/recovery-check")
    async def assess_task_recovery(task_id: str, offset: int = 0, expected_revision: int | None = None):
        check_secretary(controller)
        from .pm_recovery import assess_task_recovery
        try:
            return await run_in_threadpool(assess_task_recovery, controller, task_id, offset, expected_revision)
        except KeyError:
            raise HTTPException(status_code=404, detail="任务不存在")

    @router.post("/api/tasks/{task_id}/resume")
    async def resume_task(task_id: str, payload: dict):
        check_secretary(controller)
        from .task_resume import request_task_resume
        try:
            result = await run_in_threadpool(request_task_resume, controller, task_id, payload.get("expected_revision"))
            return JSONResponse(result, status_code=200 if result.get("ok") else 202)
        except KeyError:
            raise HTTPException(404, "任务不存在")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @router.get("/api/tasks/{task_id}/resume-authorization")
    async def get_resume_authorization(task_id: str, request_id: str):
        check_secretary(controller)
        from .task_resume import resume_authorization
        try:
            return await run_in_threadpool(resume_authorization, controller, task_id, request_id)
        except KeyError:
            raise HTTPException(404, "任务不存在")
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    @router.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str):
        """彻底删除任务及关联 PM Agent 记录。若任务仍在运行, 先取消再删除。"""
        check_secretary(controller)
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        control = (task.output_data or {}).get("_pm_control", {})
        if control and control.get("state") != "confirmed":
            raise HTTPException(status_code=409, detail="原节点尚未确认停止，请先核实或重发停止请求；保留任务记录")
        if task.status in ("running", "monitoring", "planning", "executing"):
            result = await run_in_threadpool(controller.cancel_task, task_id)
            if not result.get("confirmed"):
                raise HTTPException(status_code=409, detail="原节点尚未确认停止，保留任务记录；请核实后再删除")
        try:
            ok = db.delete_task(task_id, require_stopped=True)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if task.pm_agent_id and task.pm_agent_id in controller._pm_worker_map:
            del controller._pm_worker_map[task.pm_agent_id]
        # 联动清理: 从对话中解绑 PM 线程
        pm_id = task.pm_agent_id or ""
        if pm_id:
            ch = getattr(controller, 'chat_handler', None)
            if ch:
                conv_id = ch.find_conv_by_pm(pm_id)
                if conv_id:
                    ch.detach_pm_thread(conv_id, pm_id)
                # 清理 PM 线程消息 (内存 + JSONL 文件)
                ch.delete_pm_thread(pm_id)
        if ok:
            await _broadcast(state, "task_deleted", {"task_id": task_id, "name": task.name})
            return {"ok": True, "message": f"任务 '{task.name}' 已删除"}
        raise HTTPException(status_code=500, detail="删除失败")

    # ── Graph Engine: DAG 图结构 / Checkpoint / 断点恢复 ──
    # 注: Orchestrator 已废弃 (能力由 PM 四件套接管), 图数据一律从
    # DB checkpoint/子任务重建; 手工编辑执行图下线；resume 已改为当前 PM 的显式检查点恢复。

    @router.get("/api/tasks/{task_id}/graph")
    async def get_task_graph(task_id: str):
        """获取任务的 DAG 图结构 JSON (供前端 SVG 渲染)。

        返回: {nodes: [{id, name, status, skill, x, y, ...}], edges: [{source, target, condition}]}
        """
        check_secretary(controller)
        # 从 DB 重建 DAG (checkpoint 优先, 其次子任务列表)
        import json as _json
        from .task import SubTask, TaskDAG
        ckpt = db.get_latest_checkpoint(task_id)
        if ckpt:
            try:
                dag_data = _json.loads(ckpt.get("dag_json", "{}"))
                if dag_data.get("nodes"):
                    return dag_data
            except (ValueError, TypeError):
                pass
        task = db.get_task(task_id)
        if task and task.subtasks:
            subtasks = [SubTask.from_dict(st) for st in task.subtasks]
            dag = TaskDAG(subtasks)
            return dag.to_graph_json()
        raise HTTPException(status_code=404, detail="任务无 DAG 图数据")

    @router.get("/api/tasks/{task_id}/checkpoints")
    async def list_checkpoints(task_id: str):
        """查看任务的 checkpoint 列表。"""
        check_secretary(controller)
        checkpoints = db.list_checkpoints(task_id)
        return {"checkpoints": checkpoints, "total": len(checkpoints)}

    # ── 优化7: 反向沟通 — PM 注入回复 ──

    @router.get("/api/pm/{pm_id}/clarification")
    async def get_pm_clarification(pm_id: str):
        check_secretary(controller)
        from .pm_clarification import query_question
        try:
            return await run_in_threadpool(query_question, controller, pm_id)
        except (ValueError, OSError):
            raise HTTPException(409, "原问题暂时无法核实，请检查节点连接")

    @router.post("/api/pm/{pm_id}/inject-input")
    async def inject_pm_input(pm_id: str, payload: dict):
        """向 PM Agent 注入来自 Boss/秘书的回复 (反向沟通通道)。

        当 PM 上报 awaiting_input 状态后, 秘书/Web UI 可通过此端点
        将 Boss 的决策回复注入到 PM Agent。
        """
        check_secretary(controller)
        result = await run_in_threadpool(controller.inject_input_to_pm, pm_id, payload)
        if result.get("ok"):
            await _broadcast(state, "pm_input_injected", {
                "pm_id": pm_id, "message": "回复已注入 PM Agent",
            })
            return result
        raise HTTPException(status_code=409, detail=result.get("message", "注入失败"))

    # ── 优化9: 交付闭环 ──

    @router.post("/api/pm/{pm_id}/deliver")
    async def receive_pm_delivery(pm_id: str, payload: dict):
        """接收 PM Agent 的最终交付物 (任务完成后调用)。

        存储交付物 → WebSocket 广播 → Bot 推送 → 聊天窗口通知 Boss。
        Boss 可随后「验收」或「退回」。
        """
        check_secretary(controller)
        task_id = payload.get("task_id", "")
        task_name = payload.get("task_name", "")
        deliverable = payload.get("deliverable", "")
        summary = payload.get("summary", "")
        subtask_stats = payload.get("subtask_stats", {})

        if not isinstance(deliverable, str) or not deliverable.strip():
            raise HTTPException(422, "交付内容不能为空")
        delivery = {
            "pm_id": pm_id, "deliverable": deliverable, "summary": summary,
            "subtask_stats": subtask_stats, "delivered_at": payload.get("delivered_at", time.time()),
        }
        try:
            result = db.record_task_delivery(task_id, pm_id, delivery)
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if result["duplicate"]:
            return {"ok": True, "task_id": task_id, **result,
                    "message": "该交付已接收，保留现有验收结论"}

        # WebSocket 广播
        await _broadcast(state, "task_delivered", {
            "pm_id": pm_id,
            "task_id": task_id,
            "task_name": task_name,
            "summary": summary,
            "subtask_stats": subtask_stats,
        })

        # Bot 推送
        try:
            controller.bot_gateway.notify("task_delivered", {
                "name": task_name, "task_id": task_id[:12] if task_id else "",
            })
        except Exception as exc:
            logger.warning("交付已保存，但通知发送失败: %s", exc)

        logger.info("PM %s 交付物已接收: %s", pm_id[:12], task_name)
        return {"ok": True, "task_id": task_id, "message": "交付物已接收, 等待 Boss 验收"}

    @router.post("/api/tasks/{task_id}/accept")
    async def accept_delivery(task_id: str):
        """Boss 验收交付物。"""
        check_secretary(controller)
        try:
            db.review_task_delivery(task_id, True)
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        await _broadcast(state, "task_updated", {"task_id": task_id, "status": "accepted"})
        logger.info("任务 %s 交付物已验收", task_id[:12])
        return {"ok": True, "message": "已验收"}

    @router.post("/api/tasks/{task_id}/reject")
    async def reject_delivery(task_id: str, payload: dict = None):
        """Boss 退回交付物。"""
        check_secretary(controller)
        reason = (payload or {}).get("reason", "")
        try:
            db.review_task_delivery(task_id, False, reason)
        except KeyError as exc:
            raise HTTPException(404, exc.args[0]) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        await _broadcast(state, "task_updated", {"task_id": task_id, "status": "rejected"})
        logger.info("任务 %s 交付物被退回: %s", task_id[:12], reason[:100])
        return {"ok": True, "message": "已退回"}

    # ── 优化14: 任务记忆 ──

    @router.post("/api/pm/{pm_id}/task-memory")
    async def receive_task_memory(pm_id: str, payload: dict):
        """接收 PM Agent 的任务记忆 (任务完成后调用)。

        存储到 task_memory 表, 用于未来同类任务的决策参考:
        - 推荐协作模式
        - 历史成功率
        - 常见错误预警
        """
        check_secretary(controller)
        db.save_task_memory(
            task_name=payload.get("task_name", ""),
            task_keywords=payload.get("task_keywords", []),
            task_type=payload.get("task_type", "general"),
            collaboration_mode=payload.get("collaboration_mode", ""),
            team_size=payload.get("team_size", 0),
            duration_secs=payload.get("duration_secs", 0),
            success=payload.get("success", True),
            error_pattern=payload.get("error_pattern", ""),
            boss_feedback=payload.get("boss_feedback", ""),
            device_id=payload.get("device_id", ""),
        )
        logger.info("PM %s 任务记忆已存储 (type=%s)", pm_id[:12], payload.get('task_type', ''))
        return {"ok": True, "message": "任务记忆已存储"}

    @router.get("/api/task-memory/stats")
    async def get_task_memory_stats(task_type: str = ""):
        """查询任务记忆统计 (成功率、推荐模式等)。"""
        check_secretary(controller)
        stats = db.get_task_memory_stats(task_type=task_type)
        return stats

    return router
