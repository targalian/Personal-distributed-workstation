"""Project blueprint domain over the shared project policy."""

from .project_policy import (
    time, Optional, Project, logger,
    _current_phase_index,
)
from .component_service import ComponentService

class ProjectBlueprintService(ComponentService):
    """Single domain implementation behind the existing facade."""

    OPERATIONS = ('update_project_blueprint', 'record_delivery_to_blueprint')

    def update_project_blueprint(
        self,
        project_id: str,
        charter: dict,
        roadmap: list,
        decisions: list,
    ) -> Optional[Project]:
        """更新项目蓝图工作台数据并持久化。"""
        if not isinstance(charter, dict):
            raise ValueError("charter 必须是对象")
        if not isinstance(roadmap, list):
            raise ValueError("roadmap 必须是数组")
        if not isinstance(decisions, list):
            raise ValueError("decisions 必须是数组")
        return self.owner.update_project(
            project_id,
            charter=charter,
            roadmap=roadmap,
            decisions=decisions,
        )


    def record_delivery_to_blueprint(self, project_id: str, task_name: str,
                                     acceptance_review: dict,
                                     task_id: str = "") -> Optional[Project]:
        """iter-95: 把 PM 交付结论回流到项目蓝图的当前路线图阶段。

        Boss ↔ 秘书讨论产出蓝图, PM 依蓝图执行, 交付结论再回写蓝图对应项 —
        形成「蓝图 ↔ 项目」一对一的闭环, Boss 据此提出下一轮修改。

        只更新「当前阶段」(优先 in_progress/active, 其次首个未完成) 的
        delivery 字段, 不改 phase/goal/branch 等 Boss 手填内容:
          - verdict=pass 且无 unmet → status 推进为 review (待 Boss 确认)
          - 有 unmet → status 保持不变, 记录 unmet 清单供 Boss 决策

        无蓝图 / 无当前阶段 / 项目不存在时静默返回 None, 绝不阻断交付。
        """
        if not project_id:
            return None
        project = self.owner.db.get_project(project_id)
        if not project:
            return None
        roadmap = project.roadmap
        if not isinstance(roadmap, list) or not roadmap:
            return None

        idx = _current_phase_index(roadmap)
        if idx is None:
            return None

        phase = dict(roadmap[idx])
        review = acceptance_review if isinstance(acceptance_review, dict) else {}
        unmet = review.get("unmet") if isinstance(review.get("unmet"), list) else []
        verdict = str(review.get("verdict", "")).strip()

        entry = {
            "task_id": task_id,
            "task_name": str(task_name or "")[:200],
            "verdict": verdict or "unreviewed",
            "unmet": [str(u)[:200] for u in unmet][:6],
            "summary": str(review.get("summary", ""))[:300],
            "delivered_at": time.time(),
        }
        deliveries = phase.get("deliveries")
        if not isinstance(deliveries, list):
            deliveries = []
        deliveries.append(entry)
        phase["deliveries"] = deliveries[-5:]

        if verdict == "pass" and not unmet:
            phase["status"] = "review"

        new_roadmap = list(roadmap)
        new_roadmap[idx] = phase
        logger.info("项目蓝图已回流交付结论: %s 阶段=%s verdict=%s",
                    project_id[:8], phase.get("phase", "")[:40], entry["verdict"])
        return self.owner.update_project(project_id, roadmap=new_roadmap)

