"""TaskMemoryService: Yomk service for the Station task domain."""

import json


from .logger import get_logger
from .service_context import StationService, StationServiceContext
from ._vendor.yomkserverpy.YomkServerPy import YomkServer

logger = get_logger("task_service")


class TaskMemoryService(StationService):
    """Task domain operations, callable independently of the HTTP layer."""

    def __init__(self, server: YomkServer, context: StationServiceContext):
        super().__init__(server, context, "/TaskMemoryService")

    async def receive_task_memory(self, pm_id: str, payload: dict) -> dict:
        """接收 PM Agent 的任务记忆 (任务完成后调用)。

        存储到 task_memory 表, 用于未来同类任务的决策参考:
        - 推荐协作模式
        - 历史成功率
        - 常见错误预警
        """
        controller = self.context.controller
        db = controller.db
        self.require_secretary()
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

    async def get_task_memory_stats(self, task_type: str = "") -> dict:
        """查询任务记忆统计 (成功率、推荐模式等)。"""
        controller = self.context.controller
        db = controller.db
        self.require_secretary()
        stats = db.get_task_memory_stats(task_type=task_type)
        return stats

    async def task_memory_overview(self, limit: int = 20) -> dict:
        """iter-42 (F4.1 可视化): 任务记忆总览 — 全局统计 + 按类型分组 + 最近记录。

        Args:
            limit: 最近记录返回条数 (1~50)

        返回: {total, success_rate, avg_duration, recommended_mode,
               by_type: [{task_type, count, success_rate, avg_duration,
                          recommended_mode}], recent: [...]}
        """
        controller = self.context.controller
        db = controller.db
        self.require_secretary()
        limit = max(1, min(limit, 50))
        rows = db.query_task_memory(limit=500)
        overall = db.get_task_memory_stats()
        by_type = self._group_stats(rows)
        recent = self._recent_rows(rows[:limit])
        return {**overall, "by_type": by_type, "recent": recent}

    def _group_stats(self, rows: list) -> list:
        # 按类型分组聚合 (供面板分类型展示)
        groups: dict[str, list] = {}
        for r in rows:
            groups.setdefault(r.get("task_type") or "general", []).append(r)
        by_type = []
        for t, items in groups.items():
            n = len(items)
            ok = sum(1 for x in items if x.get("success"))
            modes: dict[str, int] = {}
            for x in items:
                m = x.get("collaboration_mode") or ""
                if m:
                    modes[m] = modes.get(m, 0) + 1
            by_type.append({
                "task_type": t,
                "count": n,
                "success_rate": round(ok / n, 3) if n else 0,
                "avg_duration": round(
                    sum(float(x.get("duration_secs") or 0) for x in items) / n, 1
                ) if n else 0,
                "recommended_mode": max(modes, key=modes.get) if modes else "",
            })
        by_type.sort(key=lambda g: g["count"], reverse=True)
        return by_type

    def _recent_rows(self, rows: list) -> list:
        recent = []
        for r in rows:
            kw = r.get("task_keywords") or []
            if isinstance(kw, str):
                try:
                    kw = json.loads(kw)
                except Exception:
                    kw = []
            recent.append({
                "task_name": r.get("task_name", ""),
                "task_type": r.get("task_type", "general"),
                "collaboration_mode": r.get("collaboration_mode", ""),
                "team_size": r.get("team_size", 0),
                "duration_secs": r.get("duration_secs", 0),
                "success": bool(r.get("success")),
                "error_pattern": r.get("error_pattern", ""),
                "boss_feedback": r.get("boss_feedback", ""),
                "keywords": kw[:5],
                "created_at": r.get("created_at", 0),
            })
        return recent
