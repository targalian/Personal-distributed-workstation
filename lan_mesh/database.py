"""
SQLite 数据库存储层 - Secretary 端主机注册记录持久化

参考 QuickLAN 的 SQLite 使用方式，用于存储主机信息与心跳历史。
"""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .protocol import HostRecord, AgentCard, Task, SubTask


class Database:
    """线程安全的 SQLite 数据库封装。

    存储主机注册记录，支持增删改查与心跳更新。
    """

    def __init__(self, db_path: str):
        self.path = Path(db_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        """每个线程获取独立的连接 (SQLite 线程安全要求)。"""
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        """初始化数据库表结构。"""
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS hosts (
                device_id     TEXT PRIMARY KEY,
                device_name   TEXT NOT NULL DEFAULT '',
                role          TEXT NOT NULL DEFAULT 'worker',
                hostname      TEXT NOT NULL DEFAULT '',
                platform      TEXT NOT NULL DEFAULT '',
                ip            TEXT NOT NULL DEFAULT '',
                api_port      INTEGER NOT NULL DEFAULT 0,
                cpu_count     INTEGER NOT NULL DEFAULT 0,
                memory_total_mb INTEGER NOT NULL DEFAULT 0,
                disk_total_gb INTEGER NOT NULL DEFAULT 0,
                cpu_percent   REAL NOT NULL DEFAULT 0,
                memory_percent REAL NOT NULL DEFAULT 0,
                disk_percent  REAL NOT NULL DEFAULT 0,
                shared_folder TEXT NOT NULL DEFAULT '',
                shared_file_count INTEGER NOT NULL DEFAULT 0,
                online        INTEGER NOT NULL DEFAULT 1,
                registered_at REAL NOT NULL DEFAULT 0,
                last_seen     REAL NOT NULL DEFAULT 0,
                latency_ms    REAL
            );

            CREATE TABLE IF NOT EXISTS heartbeat_log (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                cpu_percent REAL,
                memory_percent REAL,
                disk_percent REAL
            );

            CREATE INDEX IF NOT EXISTS idx_heartbeat_device
                ON heartbeat_log(device_id, timestamp);

            CREATE TABLE IF NOT EXISTS agents (
                agent_id           TEXT PRIMARY KEY,
                agent_name         TEXT NOT NULL DEFAULT '',
                version            TEXT NOT NULL DEFAULT '0.1.0',
                device_id          TEXT NOT NULL DEFAULT '',
                hostname           TEXT NOT NULL DEFAULT '',
                ip                 TEXT NOT NULL DEFAULT '',
                api_port           INTEGER NOT NULL DEFAULT 0,
                skills             TEXT NOT NULL DEFAULT '[]',
                tools              TEXT NOT NULL DEFAULT '[]',
                model_preferences  TEXT NOT NULL DEFAULT '[]',
                max_concurrent     INTEGER NOT NULL DEFAULT 5,
                status             TEXT NOT NULL DEFAULT 'idle',
                current_task_count INTEGER NOT NULL DEFAULT 0,
                registered_at      REAL NOT NULL DEFAULT 0,
                last_seen          REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS tasks (
                task_id      TEXT PRIMARY KEY,
                name         TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                input_data   TEXT NOT NULL DEFAULT '{}',
                output_data  TEXT NOT NULL DEFAULT '{}',
                status       TEXT NOT NULL DEFAULT 'pending',
                subtasks     TEXT NOT NULL DEFAULT '[]',
                created_at   REAL NOT NULL DEFAULT 0,
                completed_at REAL NOT NULL DEFAULT 0,
                created_by   TEXT NOT NULL DEFAULT 'user'
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_agents_status ON agents(status);

            CREATE TABLE IF NOT EXISTS projects (
                project_id        TEXT PRIMARY KEY,
                name              TEXT NOT NULL DEFAULT '',
                description       TEXT NOT NULL DEFAULT '',
                workspace_path    TEXT NOT NULL DEFAULT '',
                budget_limit_usd  REAL NOT NULL DEFAULT 0,
                budget_used_usd   REAL NOT NULL DEFAULT 0,
                allowed_models    TEXT NOT NULL DEFAULT '[]',
                routing_strategy  TEXT NOT NULL DEFAULT 'balanced',
                status            TEXT NOT NULL DEFAULT 'active',
                created_at        REAL NOT NULL DEFAULT 0,
                updated_at        REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS usage_log (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id    TEXT NOT NULL,
                task_id       TEXT NOT NULL DEFAULT '',
                subtask_id    TEXT NOT NULL DEFAULT '',
                model         TEXT NOT NULL DEFAULT '',
                input_tokens  INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                cost_usd      REAL NOT NULL DEFAULT 0,
                timestamp     REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_usage_project
                ON usage_log(project_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
        """)
        # 兼容已有 tasks 表: 安全添加 project_id 列
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass  # 列已存在
        conn.commit()

    # ── 主机记录 CRUD ───────────────────────────────────────────

    def upsert_host(self, record: HostRecord):
        """插入或更新主机记录。"""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO hosts (
                device_id, device_name, role, hostname, platform,
                ip, api_port, cpu_count, memory_total_mb, disk_total_gb,
                cpu_percent, memory_percent, disk_percent,
                shared_folder, shared_file_count, online,
                registered_at, last_seen, latency_ms
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?
            )
            ON CONFLICT(device_id) DO UPDATE SET
                device_name=excluded.device_name,
                role=excluded.role,
                hostname=excluded.hostname,
                platform=excluded.platform,
                ip=excluded.ip,
                api_port=excluded.api_port,
                cpu_count=excluded.cpu_count,
                memory_total_mb=excluded.memory_total_mb,
                disk_total_gb=excluded.disk_total_gb,
                cpu_percent=excluded.cpu_percent,
                memory_percent=excluded.memory_percent,
                disk_percent=excluded.disk_percent,
                shared_folder=excluded.shared_folder,
                shared_file_count=excluded.shared_file_count,
                online=excluded.online,
                last_seen=excluded.last_seen,
                latency_ms=excluded.latency_ms
        """, (
            record.device_id, record.device_name, record.role,
            record.hostname, record.platform,
            record.ip, record.api_port,
            record.cpu_count, record.memory_total_mb, record.disk_total_gb,
            record.cpu_percent, record.memory_percent, record.disk_percent,
            record.shared_folder, record.shared_file_count,
            1 if record.online else 0,
            record.registered_at, record.last_seen, record.latency_ms,
        ))
        conn.commit()

    def log_heartbeat(self, device_id: str, cpu: float, mem: float, disk: float):
        """记录心跳历史。"""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO heartbeat_log (device_id, timestamp, cpu_percent, memory_percent, disk_percent)
            VALUES (?, ?, ?, ?, ?)
        """, (device_id, time.time(), cpu, mem, disk))
        conn.commit()

    def get_host(self, device_id: str) -> Optional[HostRecord]:
        """查询单台主机记录。"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM hosts WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row:
            return HostRecord(
                device_id=row["device_id"],
                device_name=row["device_name"],
                role=row["role"],
                hostname=row["hostname"],
                platform=row["platform"],
                ip=row["ip"],
                api_port=row["api_port"],
                cpu_count=row["cpu_count"],
                memory_total_mb=row["memory_total_mb"],
                disk_total_gb=row["disk_total_gb"],
                cpu_percent=row["cpu_percent"],
                memory_percent=row["memory_percent"],
                disk_percent=row["disk_percent"],
                shared_folder=row["shared_folder"],
                shared_file_count=row["shared_file_count"],
                online=bool(row["online"]),
                registered_at=row["registered_at"],
                last_seen=row["last_seen"],
                latency_ms=row["latency_ms"],
            )
        return None

    def list_hosts(self) -> list[HostRecord]:
        """列出所有主机记录。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM hosts ORDER BY online DESC, device_name ASC"
        ).fetchall()
        return [
            HostRecord(
                device_id=r["device_id"],
                device_name=r["device_name"],
                role=r["role"],
                hostname=r["hostname"],
                platform=r["platform"],
                ip=r["ip"],
                api_port=r["api_port"],
                cpu_count=r["cpu_count"],
                memory_total_mb=r["memory_total_mb"],
                disk_total_gb=r["disk_total_gb"],
                cpu_percent=r["cpu_percent"],
                memory_percent=r["memory_percent"],
                disk_percent=r["disk_percent"],
                shared_folder=r["shared_folder"],
                shared_file_count=r["shared_file_count"],
                online=bool(r["online"]),
                registered_at=r["registered_at"],
                last_seen=r["last_seen"],
                latency_ms=r["latency_ms"],
            )
            for r in rows
        ]

    def set_offline(self, device_id: str):
        """标记主机为离线。"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE hosts SET online = 0 WHERE device_id = ?", (device_id,)
        )
        conn.commit()

    def prune_offline(self, ttl: float):
        """将超过 TTL 未活跃的主机标记为离线。"""
        conn = self._get_conn()
        cutoff = time.time() - ttl
        conn.execute(
            "UPDATE hosts SET online = 0 WHERE last_seen < ? AND online = 1",
            (cutoff,),
        )
        conn.commit()

    def cleanup_old_heartbeats(self, max_age_hours: int = 24):
        """清理超过指定时长的心跳历史。"""
        conn = self._get_conn()
        cutoff = time.time() - max_age_hours * 3600
        conn.execute(
            "DELETE FROM heartbeat_log WHERE timestamp < ?", (cutoff,)
        )
        conn.commit()

    # ── Agent Card CRUD ──────────────────────────────────────────

    def upsert_agent(self, card: AgentCard):
        """插入或更新 Agent Card。"""
        import json
        conn = self._get_conn()
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
        conn = self._get_conn()
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
        conn = self._get_conn()
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

    def update_agent_status(self, agent_id: str, status: str, task_count: int = None):
        """更新 Agent 状态。"""
        conn = self._get_conn()
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
        conn = self._get_conn()
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

    # ── Task CRUD ───────────────────────────────────────────────

    def save_task(self, task: Task):
        """保存任务 (含子任务)。"""
        import json
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO tasks (task_id, name, description, input_data, output_data,
                              status, subtasks, created_at, completed_at, created_by, project_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                name=excluded.name, description=excluded.description,
                input_data=excluded.input_data, output_data=excluded.output_data,
                status=excluded.status, subtasks=excluded.subtasks,
                completed_at=excluded.completed_at, project_id=excluded.project_id
        """, (
            task.task_id, task.name, task.description,
            json.dumps(task.input_data), json.dumps(task.output_data),
            task.status, json.dumps(task.subtasks),
            task.created_at, task.completed_at, task.created_by,
            task.project_id,
        ))
        conn.commit()

    def get_task(self, task_id: str) -> Optional[Task]:
        """查询单个任务。"""
        import json
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return Task(
            task_id=row["task_id"], name=row["name"], description=row["description"],
            input_data=json.loads(row["input_data"]),
            output_data=json.loads(row["output_data"]),
            status=row["status"],
            subtasks=json.loads(row["subtasks"]),
            created_at=row["created_at"], completed_at=row["completed_at"],
            created_by=row["created_by"],
            project_id=row["project_id"] if "project_id" in row.keys() else "",
        )

    def list_tasks(self, status: str = None, limit: int = 50) -> list[Task]:
        """列出任务,可按状态过滤。"""
        import json
        conn = self._get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Task(
                task_id=r["task_id"], name=r["name"], description=r["description"],
                input_data=json.loads(r["input_data"]),
                output_data=json.loads(r["output_data"]),
                status=r["status"],
                subtasks=json.loads(r["subtasks"]),
                created_at=r["created_at"], completed_at=r["completed_at"],
                created_by=r["created_by"],
                project_id=r["project_id"] if "project_id" in r.keys() else "",
            )
            for r in rows
        ]

    # ── Project CRUD ────────────────────────────────────────────

    def upsert_project(self, project):
        """插入或更新项目记录。"""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO projects (
                project_id, name, description, workspace_path,
                budget_limit_usd, budget_used_usd, allowed_models,
                routing_strategy, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                name=excluded.name, description=excluded.description,
                workspace_path=excluded.workspace_path,
                budget_limit_usd=excluded.budget_limit_usd,
                budget_used_usd=excluded.budget_used_usd,
                allowed_models=excluded.allowed_models,
                routing_strategy=excluded.routing_strategy,
                status=excluded.status, updated_at=excluded.updated_at
        """, (
            project.project_id, project.name, project.description,
            project.workspace_path,
            project.budget_limit_usd, project.budget_used_usd,
            json.dumps(project.allowed_models),
            project.routing_strategy, project.status,
            project.created_at, project.updated_at,
        ))
        conn.commit()

    def get_project(self, project_id: str):
        """查询单个项目。"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?", (project_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_project(row)

    def list_projects(self, status: str = None) -> list:
        """列出所有项目,可按状态过滤。"""
        conn = self._get_conn()
        if status:
            rows = conn.execute(
                "SELECT * FROM projects WHERE status = ? ORDER BY updated_at DESC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY updated_at DESC"
            ).fetchall()
        return [self._row_to_project(r) for r in rows]

    def delete_project(self, project_id: str):
        """归档项目 (软删除,设置 status=archived)。"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE projects SET status = 'archived', updated_at = ? WHERE project_id = ?",
            (time.time(), project_id),
        )
        conn.commit()

    def update_project_budget(self, project_id: str, used_usd: float):
        """更新项目已消费金额。"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE projects SET budget_used_usd = ?, updated_at = ? WHERE project_id = ?",
            (used_usd, time.time(), project_id),
        )
        conn.commit()

    def update_project_status(self, project_id: str, status: str):
        """更新项目状态。"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE projects SET status = ?, updated_at = ? WHERE project_id = ?",
            (status, time.time(), project_id),
        )
        conn.commit()

    def _row_to_project(self, row):
        """将数据库行转换为 Project 对象。"""
        from .protocol import Project
        return Project(
            project_id=row["project_id"],
            name=row["name"],
            description=row["description"],
            workspace_path=row["workspace_path"],
            budget_limit_usd=row["budget_limit_usd"],
            budget_used_usd=row["budget_used_usd"],
            allowed_models=json.loads(row["allowed_models"]),
            routing_strategy=row["routing_strategy"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ── Usage Log ───────────────────────────────────────────────

    def record_usage(self, project_id: str, task_id: str, subtask_id: str,
                     model: str, input_tokens: int, output_tokens: int,
                     cost_usd: float):
        """记录一次模型调用的消费。"""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO usage_log
                (project_id, task_id, subtask_id, model, input_tokens, output_tokens, cost_usd, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (project_id, task_id, subtask_id, model,
              input_tokens, output_tokens, cost_usd, time.time()))
        conn.commit()

    def get_usage_log(self, project_id: str, limit: int = 100) -> list[dict]:
        """查询项目的消费记录。"""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM usage_log WHERE project_id = ?
               ORDER BY timestamp DESC LIMIT ?""",
            (project_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
