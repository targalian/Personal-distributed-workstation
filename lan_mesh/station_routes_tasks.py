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
import uuid as _uuid

import requests as http_requests

from fastapi import APIRouter, HTTPException

from .protocol import AgentCard, Task
from .http_retry import auth_headers
from .logger import get_logger
from .station_routes_common import _broadcast, check_secretary

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
        """提交新任务 — 创建 Task 记录并分配 PM Agent。

        新流程 (PM Agent 架构):
        1. 创建 Task 记录 (status=pending, pm_agent_id="")
        2. 选择合适 work_station (按评级/在线状态)
        3. POST 到目标 Worker /role/start-pm (携带 task_id, secretary_url)
        4. 更新 Task.pm_agent_id, status=running
        5. WebSocket 广播 task_submitted + pm_registered
        """
        check_secretary(controller)
        project_manager = controller.project_manager
        project_id = payload.get("project_id", "")
        if project_id and project_manager:
            if not project_manager.check_budget(project_id):
                raise HTTPException(status_code=402, detail="项目预算已起支或已暂停,无法提交任务")

        # 1. 创建 Task 记录
        task = Task(
            task_id=f"task-{_uuid.uuid4().hex[:12]}",
            name=payload.get("name", ""),
            description=payload.get("description", ""),
            input_data=payload.get("input_data", {}),
            created_by=payload.get("created_by", "user"),
            project_id=project_id,
            status="pending",
        )
        db.save_task(task)
        await _broadcast(state, "task_submitted", task.to_dict())
        controller.bot_gateway.notify("task_submitted", {
            "name": task.name, "task_id": task.task_id[:8],
        })
        # P3: 任务流追踪 — 提交阶段点 (异常静默)
        try:
            from . import runtime_trace
            runtime_trace.trace_task_event(
                task.task_id, "submitted",
                detail=f"{task.name} (created_by={task.created_by})")
        except Exception:
            pass

        # 2. 选择合适的 work_station (按评级排序, 取在线最高评级)
        from .protocol import PMAgent
        hosts = db.list_hosts()
        online_hosts = [h for h in hosts if h.online and h.device_id != state.device_id]
        # 如果没有在线 Worker, 使用本机
        if not online_hosts:
            online_hosts = [h for h in hosts if h.online]

        if not online_hosts:
            task.status = "failed"
            task.output_data = {"error": "无可用 work_station"}
            db.save_task(task)
            return task.to_dict()

        # 按评级排序 (S > A > B > C > D)
        tier_order = {"S": 5, "A": 4, "B": 3, "C": 2, "D": 1, "": 0}
        online_hosts.sort(
            key=lambda h: tier_order.get(h.rating_tier, 0),
            reverse=True,
        )
        target_host = online_hosts[0]

        # 3. 构造 Secretary URL: 本机派发用 localhost; 远程派发在回退分支重建可达地址
        secretary_url = f"http://127.0.0.1:{state.api_port}"

        # 4. 优先本机派发 (内嵌 Worker, 无需网络往返)
        if controller.secretary_active and controller.chat_runtime:
            result = controller._local_start_pm(task.task_id, secretary_url, task.to_dict())
            if result.get("ok"):
                pm_id = result["pm_id"]
                task.pm_agent_id = pm_id
                task.status = "running"
                db.save_task(task)
                pm_agent = PMAgent(
                    pm_id=pm_id,
                    agent_name=f"PM-{pm_id[:8]}",
                    task_id=task.task_id,
                    project_id=project_id,
                    device_id=state.device_id,
                    hostname=state.device_name,
                    ip="127.0.0.1",
                    api_port=state.api_port,
                    status="starting",
                )
                db.upsert_pm_agent(pm_agent)
                controller._pm_worker_map[pm_id] = {
                    "ip": "127.0.0.1",
                    "api_port": state.api_port,
                    "device_id": state.device_id,
                    "local": True,
                }
                await _broadcast(state, "pm_registered", {
                    "pm_id": pm_id, "task_id": task.task_id,
                    "device_id": state.device_id, "device_name": state.device_name,
                })
                controller.bot_gateway.notify("pm_registered", {
                    "pm_id": pm_id[:12], "task": task.name, "station": state.device_name,
                })
                await _broadcast(state, "task_updated", task.to_dict())
                return task.to_dict()
            else:
                logger.warning("本机 PM 启动失败: %s, 尝试远程派发", result.get('message'))

        # 5. 回退: POST 到远程 Worker 启动 PM Agent
        target_ip = target_host.ip
        if target_host.device_id == state.device_id:
            target_ip = "127.0.0.1"
        # 修复 (任务③): 远程 PM 必须回报到本机 Secretary — 127.0.0.1 在
        # 远端会打到远端自己的 Station, 导致进度上报丢失; 选本机与目标
        # 同网段的可达 IP (无同网段时回退首个非虚拟 IP)
        from .host_info import pick_reachable_ip
        reach_ip = pick_reachable_ip(target_ip)
        remote_secretary_url = (
            f"http://{reach_ip}:{state.api_port}" if reach_ip else secretary_url
        )
        try:
            resp = http_requests.post(
                f"http://{target_ip}:{target_host.api_port}/role/start-pm",
                json={
                    "task_id": task.task_id,
                    "secretary_url": remote_secretary_url,
                    "task_data": task.to_dict(),
                },
                headers=auth_headers(),
                timeout=15,
            )
            if resp.status_code == 200:
                pm_data = resp.json()
                pm_id = pm_data.get("pm_id", "")
                task.pm_agent_id = pm_id
                task.status = "running"
                db.save_task(task)

                pm_agent = PMAgent(
                    pm_id=pm_id,
                    agent_name=f"PM-{pm_id[:8]}",
                    task_id=task.task_id,
                    project_id=project_id,
                    device_id=target_host.device_id,
                    hostname=target_host.hostname or target_host.device_name,
                    ip=target_ip,
                    api_port=target_host.api_port,
                    status="starting",
                )
                db.upsert_pm_agent(pm_agent)
                controller._pm_worker_map[pm_id] = {
                    "ip": target_ip,
                    "api_port": target_host.api_port,
                    "device_id": target_host.device_id,
                }

                await _broadcast(state, "pm_registered", {
                    "pm_id": pm_id,
                    "task_id": task.task_id,
                    "device_id": target_host.device_id,
                    "device_name": target_host.device_name,
                })
                controller.bot_gateway.notify("pm_registered", {
                    "pm_id": pm_id[:12], "task": task.name,
                    "station": target_host.device_name or target_host.hostname,
                })
            else:
                task.status = "failed"
                task.output_data = {"error": f"PM 启动失败: {resp.text}"}
                db.save_task(task)
        except Exception as e:
            task.status = "failed"
            task.output_data = {"error": f"PM 启动异常: {e}"}
            db.save_task(task)

        await _broadcast(state, "task_updated", task.to_dict())
        return task.to_dict()

    @router.get("/api/tasks")
    async def list_tasks(status: str = None, limit: int = 50):
        check_secretary(controller)
        tasks = db.list_tasks(status=status, limit=limit)
        return {"tasks": [t.to_dict() for t in tasks], "total": len(tasks)}

    @router.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        check_secretary(controller)
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task.to_dict()

    # ── 优化8: 取消/暂停任务 ──

    @router.post("/api/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str):
        """取消指定任务及其 PM Agent。"""
        check_secretary(controller)
        result = controller.cancel_task(task_id)
        if result.get("ok"):
            await _broadcast(state, "task_cancelled", {
                "task_id": task_id, "message": result.get("message", ""),
            })
            return result
        raise HTTPException(status_code=409, detail=result.get("message", "取消失败"))

    @router.post("/api/tasks/{task_id}/pause")
    async def pause_task(task_id: str):
        """暂停指定任务及其 PM Agent。"""
        check_secretary(controller)
        result = controller.pause_task(task_id)
        if result.get("ok"):
            await _broadcast(state, "task_paused", {
                "task_id": task_id, "message": result.get("message", ""),
            })
            return result
        raise HTTPException(status_code=409, detail=result.get("message", "暂停失败"))

    @router.delete("/api/tasks/{task_id}")
    async def delete_task(task_id: str):
        """彻底删除任务及关联 PM Agent 记录。若任务仍在运行, 先取消再删除。"""
        check_secretary(controller)
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if task.status in ("running", "monitoring"):
            controller.cancel_task(task_id)
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
            # 清理本机 PM Agent 残留 (含子 Agent 与线程)
            if getattr(controller, '_local_pm_agent', None) and controller._local_pm_agent.pm_id == pm_id:
                controller._local_stop_pm()
        ok = db.delete_task(task_id)
        if ok:
            await _broadcast(state, "task_deleted", {"task_id": task_id, "name": task.name})
            return {"ok": True, "message": f"任务 '{task.name}' 已删除"}
        raise HTTPException(status_code=500, detail="删除失败")

    # ── Graph Engine: DAG 图结构 / Checkpoint / 断点恢复 ──
    # 注: Orchestrator 已废弃 (能力由 PM 四件套接管), 图数据一律从
    # DB checkpoint/子任务重建; 手工编辑图与 resume 随之下线。

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

    @router.post("/api/pm/{pm_id}/inject-input")
    async def inject_pm_input(pm_id: str, payload: dict):
        """向 PM Agent 注入来自 Boss/秘书的回复 (反向沟通通道)。

        当 PM 上报 awaiting_input 状态后, 秘书/Web UI 可通过此端点
        将 Boss 的决策回复注入到 PM Agent。
        """
        check_secretary(controller)
        result = controller.inject_input_to_pm(pm_id, payload)
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

        # 存储交付物到 DB (追加到 task output_data)
        if task_id:
            task = db.get_task(task_id)
            if task:
                if not task.output_data:
                    task.output_data = {}
                task.output_data["_delivery"] = {
                    "pm_id": pm_id,
                    "deliverable": deliverable,
                    "summary": summary,
                    "subtask_stats": subtask_stats,
                    "delivered_at": payload.get("delivered_at", time.time()),
                    "accepted": None,  # 等待 Boss 验收
                }
                db.save_task(task)

        # WebSocket 广播
        await _broadcast(state, "task_delivered", {
            "pm_id": pm_id,
            "task_id": task_id,
            "task_name": task_name,
            "summary": summary,
            "subtask_stats": subtask_stats,
        })

        # Bot 推送
        controller.bot_gateway.notify("task_delivered", {
            "name": task_name, "task_id": task_id[:12] if task_id else "",
        })

        logger.info("PM %s 交付物已接收: %s", pm_id[:12], task_name)
        return {"ok": True, "task_id": task_id, "message": "交付物已接收, 等待 Boss 验收"}

    @router.post("/api/tasks/{task_id}/accept")
    async def accept_delivery(task_id: str):
        """Boss 验收交付物。"""
        check_secretary(controller)
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if not task.output_data or "_delivery" not in task.output_data:
            raise HTTPException(status_code=409, detail="该任务尚无交付物")
        task.output_data["_delivery"]["accepted"] = True
        task.status = "accepted"
        db.save_task(task)
        await _broadcast(state, "task_updated", {"task_id": task_id, "status": "accepted"})
        logger.info("任务 %s 交付物已验收", task_id[:12])
        return {"ok": True, "message": "已验收"}

    @router.post("/api/tasks/{task_id}/reject")
    async def reject_delivery(task_id: str, payload: dict = None):
        """Boss 退回交付物。"""
        check_secretary(controller)
        task = db.get_task(task_id)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        if not task.output_data or "_delivery" not in task.output_data:
            raise HTTPException(status_code=409, detail="该任务尚无交付物")
        reason = (payload or {}).get("reason", "")
        task.output_data["_delivery"]["accepted"] = False
        task.output_data["_delivery"]["reject_reason"] = reason
        task.status = "rejected"
        db.save_task(task)
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
