"""Domain SQL repository; Database remains the sole connection and schema owner."""

from __future__ import annotations
import json
import time
from typing import Any, Optional
from .protocol import PMAgent, AgentTeam, ProgressReport
from .logger import get_logger
from .repository_registry import Repository
logger = get_logger("database")

class TeamsRepository(Repository):
    """TeamsRepository over the Database owner; SQL and connection policy are preserved."""

    def delete_pm_agent(self, pm_id: str) -> bool:
        """删除指定 PM Agent 记录。"""
        conn = self.database._get_conn()
        cursor = conn.execute("DELETE FROM pm_agents WHERE pm_id = ?", (pm_id,))
        conn.commit()
        return cursor.rowcount > 0


    def upsert_pm_agent(self, pm: PMAgent) -> Any:
        """插入或更新 PM Agent 记录。"""
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO pm_agents (
                pm_id, agent_name, task_id, project_id, device_id,
                hostname, ip, api_port, status, team_structure,
                task_list, collaboration_mode, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(pm_id) DO UPDATE SET
                agent_name=excluded.agent_name,
                task_id=excluded.task_id,
                project_id=excluded.project_id,
                device_id=excluded.device_id,
                hostname=excluded.hostname,
                ip=excluded.ip,
                api_port=excluded.api_port,
                status=excluded.status,
                team_structure=excluded.team_structure,
                task_list=excluded.task_list,
                collaboration_mode=excluded.collaboration_mode,
                updated_at=excluded.updated_at
        """, (
            pm.pm_id, pm.agent_name, pm.task_id, pm.project_id,
            pm.device_id, pm.hostname, pm.ip, pm.api_port,
            pm.status, json.dumps(pm.team_structure),
            json.dumps(pm.task_list), pm.collaboration_mode,
            pm.created_at, pm.updated_at,
        ))
        conn.commit()


    def get_pm_agent(self, pm_id: str) -> Optional[PMAgent]:
        """查询单个 PM Agent。"""
        conn = self.database._get_conn()
        row = conn.execute(
            "SELECT * FROM pm_agents WHERE pm_id = ?", (pm_id,)
        ).fetchone()
        if not row:
            return None
        return PMAgent(
            pm_id=row["pm_id"], agent_name=row["agent_name"],
            task_id=row["task_id"], project_id=row["project_id"],
            device_id=row["device_id"], hostname=row["hostname"],
            ip=row["ip"], api_port=row["api_port"],
            status=row["status"],
            team_structure=json.loads(row["team_structure"]) if row["team_structure"] else {},
            task_list=json.loads(row["task_list"]) if row["task_list"] else [],
            collaboration_mode=row["collaboration_mode"],
            created_at=row["created_at"], updated_at=row["updated_at"],
        )


    def list_pm_agents(self, status: str = None) -> list[PMAgent]:
        """列出所有 PM Agent, 可按状态过滤。"""
        conn = self.database._get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM pm_agents WHERE status = ? ORDER BY created_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM pm_agents ORDER BY created_at DESC"
            ).fetchall()
        return [
            PMAgent(
                pm_id=r["pm_id"], agent_name=r["agent_name"],
                task_id=r["task_id"], project_id=r["project_id"],
                device_id=r["device_id"], hostname=r["hostname"],
                ip=r["ip"], api_port=r["api_port"],
                status=r["status"],
                team_structure=json.loads(r["team_structure"]) if r["team_structure"] else {},
                task_list=json.loads(r["task_list"]) if r["task_list"] else [],
                collaboration_mode=r["collaboration_mode"],
                created_at=r["created_at"], updated_at=r["updated_at"],
            )
            for r in rows
        ]


    def update_pm_status(self, pm_id: str, status: str,
                         team_structure: dict = None, task_list: list = None,
                         collaboration_mode: str = None) -> Any:
        """更新 PM Agent 状态 (由 Worker 上报)。"""
        conn = self.database._get_conn()
        sets = ["status = ?", "updated_at = ?"]
        params = [status, time.time()]
        if team_structure is not None:
            sets.append("team_structure = ?")
            params.append(json.dumps(team_structure))
        if task_list is not None:
            sets.append("task_list = ?")
            params.append(json.dumps(task_list))
        if collaboration_mode is not None:
            sets.append("collaboration_mode = ?")
            params.append(collaboration_mode)
        params.append(pm_id)
        conn.execute(
            f"UPDATE pm_agents SET {', '.join(sets)} WHERE pm_id = ?",
            params,
        )
        conn.commit()


    def upsert_team(self, team: AgentTeam) -> Any:
        """插入或更新团队记录。"""
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO agent_teams (
                team_id, pm_id, team_name, team_type, device_id,
                parent_team_id, members, status, current_task, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_id) DO UPDATE SET
                pm_id=excluded.pm_id,
                team_name=excluded.team_name,
                team_type=excluded.team_type,
                device_id=excluded.device_id,
                parent_team_id=excluded.parent_team_id,
                members=excluded.members,
                status=excluded.status,
                current_task=excluded.current_task
        """, (
            team.team_id, team.pm_id, team.team_name, team.team_type,
            team.device_id, team.parent_team_id,
            json.dumps(team.members), team.status,
            team.current_task, team.created_at,
        ))
        conn.commit()


    def get_team(self, team_id: str) -> Optional[AgentTeam]:
        """查询单个团队。"""
        conn = self.database._get_conn()
        row = conn.execute(
            "SELECT * FROM agent_teams WHERE team_id = ?", (team_id,)
        ).fetchone()
        if not row:
            return None
        return AgentTeam(
            team_id=row["team_id"], pm_id=row["pm_id"],
            team_name=row["team_name"], team_type=row["team_type"],
            device_id=row["device_id"], parent_team_id=row["parent_team_id"],
            members=json.loads(row["members"]) if row["members"] else [],
            status=row["status"], current_task=row["current_task"],
            created_at=row["created_at"],
        )


    def get_teams_by_pm(self, pm_id: str) -> list[AgentTeam]:
        """查询 PM 下属的所有团队。"""
        conn = self.database._get_conn()
        rows = conn.execute(
            "SELECT * FROM agent_teams WHERE pm_id = ? ORDER BY created_at",
            (pm_id,),
        ).fetchall()
        return [
            AgentTeam(
                team_id=r["team_id"], pm_id=r["pm_id"],
                team_name=r["team_name"], team_type=r["team_type"],
                device_id=r["device_id"], parent_team_id=r["parent_team_id"],
                members=json.loads(r["members"]) if r["members"] else [],
                status=r["status"], current_task=r["current_task"],
                created_at=r["created_at"],
            )
            for r in rows
        ]


    def list_teams(self, pm_id: str = None) -> list[AgentTeam]:
        """列出所有团队, 可按 PM 过滤。"""
        if pm_id:
            return self.database.get_teams_by_pm(pm_id)
        conn = self.database._get_conn()
        rows = conn.execute(
            "SELECT * FROM agent_teams ORDER BY created_at DESC"
        ).fetchall()
        return [
            AgentTeam(
                team_id=r["team_id"], pm_id=r["pm_id"],
                team_name=r["team_name"], team_type=r["team_type"],
                device_id=r["device_id"], parent_team_id=r["parent_team_id"],
                members=json.loads(r["members"]) if r["members"] else [],
                status=r["status"], current_task=r["current_task"],
                created_at=r["created_at"],
            )
            for r in rows
        ]


    def save_progress_report(self, report: ProgressReport) -> Any:
        """保存进度报告。"""
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO progress_reports (
                pm_id, reporter_id, reporter_type, task_name,
                progress, status, message, timestamp
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            report.pm_id, report.reporter_id, report.reporter_type,
            report.task_name, report.progress, report.status,
            report.message, report.timestamp,
        ))
        conn.commit()


    def get_progress_reports(self, pm_id: str, limit: int = 50) -> list[dict]:
        """查询 PM 的进度报告列表。"""
        conn = self.database._get_conn()
        rows = conn.execute(
            "SELECT * FROM progress_reports WHERE pm_id = ? ORDER BY timestamp DESC LIMIT ?",
            (pm_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

