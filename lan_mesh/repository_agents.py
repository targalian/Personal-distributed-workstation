"""Domain SQL repository; Database remains the sole connection and schema owner."""

from __future__ import annotations
import time
from typing import Any, Optional
from .protocol import AgentCard
from .logger import get_logger
from .repository_registry import Repository
logger = get_logger("database")

class AgentsRepository(Repository):
    """AgentsRepository over the Database owner; SQL and connection policy are preserved."""

    def upsert_agent(self, card: AgentCard) -> Any:
        """插入或更新 Agent Card。"""
        import json
        conn = self.database._get_conn()
        conn.execute("""
            INSERT INTO agents (
                agent_id, agent_name, version, device_id, hostname,
                ip, api_port, skills, tools, model_preferences,
                max_concurrent, status, current_task_count,
                registered_at, last_seen
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(agent_id) DO UPDATE SET
                agent_name=excluded.agent_name,
                version=excluded.version,
                hostname=excluded.hostname,
                ip=excluded.ip,
                api_port=excluded.api_port,
                skills=excluded.skills,
                tools=excluded.tools,
                model_preferences=excluded.model_preferences,
                max_concurrent=excluded.max_concurrent,
                status=excluded.status,
                current_task_count=excluded.current_task_count,
                last_seen=excluded.last_seen
        """, (
            card.agent_id, card.agent_name, card.version, card.device_id,
            card.hostname, card.ip, card.api_port,
            json.dumps(card.skills), json.dumps(card.tools),
            json.dumps(card.model_preferences),
            card.max_concurrent_tasks, card.status, card.current_task_count,
            card.registered_at, card.last_seen,
        ))
        conn.commit()


    def get_agent(self, agent_id: str) -> Optional[AgentCard]:
        """查询单个 Agent。"""
        import json
        conn = self.database._get_conn()
        row = conn.execute(
            "SELECT * FROM agents WHERE agent_id = ?", (agent_id,)
        ).fetchone()
        if not row:
            return None
        return AgentCard(
            agent_id=row["agent_id"],
            agent_name=row["agent_name"],
            version=row["version"],
            device_id=row["device_id"],
            hostname=row["hostname"],
            ip=row["ip"],
            api_port=row["api_port"],
            skills=json.loads(row["skills"]),
            tools=json.loads(row["tools"]),
            model_preferences=json.loads(row["model_preferences"]),
            max_concurrent_tasks=row["max_concurrent"],
            status=row["status"],
            current_task_count=row["current_task_count"],
            registered_at=row["registered_at"],
            last_seen=row["last_seen"],
        )


    def list_agents(self, status: str = None) -> list[AgentCard]:
        """列出所有 Agent,可按状态过滤。"""
        import json
        conn = self.database._get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM agents WHERE status = ? ORDER BY agent_name", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM agents ORDER BY status, agent_name"
            ).fetchall()
        return [
            AgentCard(
                agent_id=r["agent_id"], agent_name=r["agent_name"],
                version=r["version"], device_id=r["device_id"],
                hostname=r["hostname"], ip=r["ip"], api_port=r["api_port"],
                skills=json.loads(r["skills"]), tools=json.loads(r["tools"]),
                model_preferences=json.loads(r["model_preferences"]),
                max_concurrent_tasks=r["max_concurrent"],
                status=r["status"], current_task_count=r["current_task_count"],
                registered_at=r["registered_at"], last_seen=r["last_seen"],
            )
            for r in rows
        ]


    def update_agent_status(self, agent_id: str, status: str, task_count: int = None) -> Any:
        """更新 Agent 状态。"""
        conn = self.database._get_conn()
        now = time.time()
        if task_count is not None:
            conn.execute(
                "UPDATE agents SET status = ?, current_task_count = ?, last_seen = ? WHERE agent_id = ?",
                (status, task_count, now, agent_id),
            )
        else:
            conn.execute(
                "UPDATE agents SET status = ?, last_seen = ? WHERE agent_id = ?",
                (status, now, agent_id),
            )
        conn.commit()


    def find_idle_agent_with_skill(self, skill_name: str) -> Optional[AgentCard]:
        """查找空闲且具备指定技能的 Agent。"""
        import json
        conn = self.database._get_conn()
        rows = conn.execute(
            "SELECT * FROM agents WHERE status = 'idle' ORDER BY current_task_count ASC"
        ).fetchall()
        for r in rows:
            skills = json.loads(r["skills"])
            for s in skills:
                if s.get("name") == skill_name:
                    return AgentCard(
                        agent_id=r["agent_id"], agent_name=r["agent_name"],
                        version=r["version"], device_id=r["device_id"],
                        hostname=r["hostname"], ip=r["ip"], api_port=r["api_port"],
                        skills=skills, tools=json.loads(r["tools"]),
                        model_preferences=json.loads(r["model_preferences"]),
                        max_concurrent_tasks=r["max_concurrent"],
                        status=r["status"], current_task_count=r["current_task_count"],
                        registered_at=r["registered_at"], last_seen=r["last_seen"],
                    )
        return None

