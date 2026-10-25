"""
SQLite 数据库存储层 - Secretary 端主机注册记录持久化

参考 QuickLAN 的 SQLite 使用方式，用于存储主机信息与心跳历史。
P2 #7: 启动时自动备份 DB 到 ~/.lan_mesh/backups/ (保留最近 3 代)。
"""
import datetime
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from .protocol import HostRecord, HostEvent, AgentCard, Task, SubTask, PMAgent, AgentTeam, ProgressReport
from .database_schema import SCHEMA_0, SCHEMA_1
from .repository_registry import Repositories
from .logger import get_logger

logger = get_logger("database")

# ── Schema 版本管理 ───────────────────────────────────────────
# 每次 schema 变更时递增 SCHEMA_VERSION 并添加对应的迁移函数。
# 迁移函数签名: (conn: sqlite3.Connection) -> None

_repository_lock = threading.RLock()

SCHEMA_VERSION = 12


def _migration_v1(conn: sqlite3.Connection):
    """迁移 v1: 从旧版 ad-hoc ALTER TABLE 迁移到版本化管理。

    包含历史兼容性列添加 (幂等操作)。
    """
    # tasks 表: project_id 列
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN project_id TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    # hosts 表: 评级列
    for col, dtype, default in [
        ("rating_tier", "TEXT", "''"),
        ("rating_score", "INTEGER", "0"),
        ("rating_summary", "TEXT", "''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE hosts ADD COLUMN {col} {dtype} NOT NULL DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    # tasks 表: pm_agent_id 列
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN pm_agent_id TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass


def _migration_v2(conn: sqlite3.Connection):
    """迁移 v2: resource_usage_log 增加 usage_id (幂等键) 与 reported 游标。"""
    for col, dtype, default in [
        ("usage_id", "TEXT", "''"),
        ("reported", "INTEGER", "0"),
    ]:
        try:
            conn.execute(f"ALTER TABLE resource_usage_log "
                         f"ADD COLUMN {col} {dtype} NOT NULL DEFAULT {default}")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("""
            CREATE UNIQUE INDEX IF NOT EXISTS idx_resource_usage_uid
                ON resource_usage_log(usage_id) WHERE usage_id != ''
        """)
    except sqlite3.OperationalError:
        pass


def _migration_v3(conn: sqlite3.Connection):
    """迁移 v3: resource_usage_log 增加 task_id/project_id 成本归因列 (R6)。"""
    for col in ("task_id", "project_id"):
        try:
            conn.execute(f"ALTER TABLE resource_usage_log "
                         f"ADD COLUMN {col} TEXT NOT NULL DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    try:
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_resource_usage_task
                ON resource_usage_log(task_id) WHERE task_id != ''
        """)
    except sqlite3.OperationalError:
        pass


def _migration_v4(conn: sqlite3.Connection):
    """迁移 v4: hosts 表增加代码版本列 (S2/S3 跨主机版本统计与升级提醒)。"""
    try:
        conn.execute("ALTER TABLE hosts "
                     "ADD COLUMN code_version TEXT NOT NULL DEFAULT ''")
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("ALTER TABLE hosts "
                     "ADD COLUMN version_ts REAL NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass


def _migration_v5(conn: sqlite3.Connection):
    """迁移 v5: llm_call_log 审计表 (运行时 LLM 调用性能追踪)。

    新库由 executescript 创建; 旧库走迁移保证幂等。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_call_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            call_type     TEXT NOT NULL DEFAULT 'chat',
            model         TEXT NOT NULL DEFAULT '',
            input_tokens  INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            ttft_ms       REAL NOT NULL DEFAULT 0,
            total_ms      REAL NOT NULL DEFAULT 0,
            status        TEXT NOT NULL DEFAULT 'ok',
            task_id       TEXT NOT NULL DEFAULT '',
            error         TEXT NOT NULL DEFAULT '',
            created_at    REAL NOT NULL DEFAULT 0
        )
    """)
    try:
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_llm_call_model_ts
                ON llm_call_log(model, created_at)
        """)
    except sqlite3.OperationalError:
        pass
    try:
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_llm_call_status
                ON llm_call_log(status, created_at)
        """)
    except sqlite3.OperationalError:
        pass


def _migration_v6(conn: sqlite3.Connection):
    """迁移 v6: error_log 持久化表 (iter-47 F1.4 错误记录落盘)。

    新库由 executescript 创建; 旧库走迁移保证幂等。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS error_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    REAL NOT NULL DEFAULT 0,
            module       TEXT NOT NULL DEFAULT '',
            error_type   TEXT NOT NULL DEFAULT '',
            message      TEXT NOT NULL DEFAULT '',
            context_json TEXT NOT NULL DEFAULT '{}',
            traceback    TEXT NOT NULL DEFAULT ''
        )
    """)
    try:
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_error_log_module_ts
                ON error_log(module, timestamp)
        """)
    except sqlite3.OperationalError:
        pass


def _migration_v7(conn: sqlite3.Connection):
    """迁移 v7: heal_log 自愈动作执行日志表 (iter-49 F4.2 修复环节)。

    新库由 executescript 创建; 旧库走迁移保证幂等。
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS heal_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    REAL NOT NULL DEFAULT 0,
            category     TEXT NOT NULL DEFAULT '',
            action       TEXT NOT NULL DEFAULT '',
            result       TEXT NOT NULL DEFAULT '',
            detail       TEXT NOT NULL DEFAULT ''
        )
    """)
    try:
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_heal_log_ts
                ON heal_log(timestamp)
        """)
    except sqlite3.OperationalError:
        pass


def _migration_v8(conn: sqlite3.Connection):
    """迁移 v8: skills 表增加 origin 来源列 (iter-61 F5.3 插件系统)。

    区分内置技能 (builtin) 与第三方市场安装技能 (market),
    卸载保护与来源追踪依赖该列; 旧库补列默认 builtin。
    """
    try:
        conn.execute(
            "ALTER TABLE skills ADD COLUMN origin TEXT NOT NULL DEFAULT 'builtin'"
        )
    except sqlite3.OperationalError:
        pass


def _migration_v10(conn: sqlite3.Connection):
    """迁移 v10: hosts 表增加联邦来源列 (iter-64 F3.4 跨网段联邦)。

    source: lan=本网段发现 / fed=跨网段联邦; federation: 所属联邦名。
    """
    for col, dtype, default in [
        ("source", "TEXT", "'lan'"),
        ("federation", "TEXT", "''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE hosts ADD COLUMN {col} {dtype} NOT NULL DEFAULT {default}")
        except sqlite3.OperationalError:
            pass


def _migration_v9(conn: sqlite3.Connection):
    """迁移 v9: users 表持久化多用户 (iter-63 团队场景深化)。

    config.security.users 从此作为首次启动种子 (DB 空时导入),
    之后以 DB 为准 → token 轮换/角色修改跨重启保留。
    token 仅存 SHA256 哈希 + 尾 4 位快照 (列表展示用), 不存明文。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            name TEXT PRIMARY KEY,
            role TEXT NOT NULL DEFAULT 'viewer',
            token_hash TEXT NOT NULL DEFAULT '',
            token_tail4 TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL DEFAULT 0
        )
        """
    )


# 迁移注册表: version → 迁移函数
def _migration_v11(conn: sqlite3.Connection):
    """Migration v11: persistent workstation optimization items (iter-72)."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS workstation_optimization_items (
            item_id        TEXT PRIMARY KEY,
            source         TEXT NOT NULL,
            title          TEXT NOT NULL,
            description    TEXT NOT NULL,
            priority       TEXT NOT NULL DEFAULT 'normal',
            status         TEXT NOT NULL DEFAULT 'waiting_boss',
            decision_reply TEXT NOT NULL DEFAULT '',
            shadow_run_id  TEXT NOT NULL DEFAULT '',
            result_json    TEXT NOT NULL DEFAULT '{}',
            created_at     REAL NOT NULL,
            updated_at     REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_workstation_opt_status
            ON workstation_optimization_items(status, updated_at)
    """)


def _migration_v12(conn: sqlite3.Connection):
    """Migration v12: project blueprint workbench fields (iter-88)."""
    for col, default in [
        ("charter", "'{}'"),
        ("roadmap", "'[]'"),
        ("decisions", "'[]'"),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE projects ADD COLUMN {col} TEXT NOT NULL DEFAULT {default}"
            )
        except sqlite3.OperationalError:
            pass


_MIGRATIONS: dict[int, callable] = {
    1: _migration_v1,
    2: _migration_v2,
    3: _migration_v3,
    4: _migration_v4,
    5: _migration_v5,
    6: _migration_v6,
    7: _migration_v7,
    8: _migration_v8,
    9: _migration_v9,
    10: _migration_v10,
    11: _migration_v11,
    12: _migration_v12,
}


from typing import Any

class Database:
    """线程安全的 SQLite 数据库封装。

    存储主机注册记录，支持增删改查与心跳更新。
    """

    def __init__(self, db_path: str):
        self.path = Path(db_path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._init_db()
        self.backup()  # P2 #7: 启动时快照备份 (失败不阻断启动)

    def backup(self, keep: int = 3) -> str:
        """P2 #7: 将 DB 安全快照到 ~/.lan_mesh/backups/, 保留最近 keep 代。

        使用 sqlite3 在线备份 API (一致性快照, 无需停写);
        备份失败仅告警不抛异常。返回备份文件路径 (失败时为空串)。
        """
        try:
            bak_dir = Path.home() / ".lan_mesh" / "backups"
            bak_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            dst = bak_dir / f"{self.path.stem}-{ts}.sqlite3"
            src = self._get_conn()
            with sqlite3.connect(str(dst)) as target:
                src.backup(target)
            # 仅保留最近 keep 代 (同名前缀)
            olds = sorted(bak_dir.glob(f"{self.path.stem}-*.sqlite3"))
            for stale in olds[:-keep]:
                try:
                    stale.unlink()
                except OSError:
                    pass
            logger.info("[DB] 已备份至 %s", dst.name)
            return str(dst)
        except Exception as e:
            logger.warning("[DB] 备份失败 (不影响运行): %s", e)
            return ""

    @property
    def repositories(self) -> Repositories:
        """Share domain repositories without sharing a SQLite connection across threads."""
        with _repository_lock:
            if not hasattr(self, '_repositories'):
                self._repositories = Repositories(self)
            return self._repositories

    def _get_conn(self) -> sqlite3.Connection:
        """每个线程获取独立的连接 (SQLite 线程安全要求)。

        iter-57 (补强#5): 并发加固 — busy_timeout 锁等待 30s (避免
        并发写触发 database is locked), WAL 模式读写不互斥 (文件系统
        不支持时降级默认 journal, 不阻断启动)。
        """
        if not hasattr(self._local, "conn"):
            conn = sqlite3.connect(str(self.path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.Error:
                pass  # 文件系统不支持 WAL 时降级默认 journal 模式
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        """初始化数据库表结构 + 版本化迁移。"""
        conn = self._get_conn()
        # 创建 schema 元数据表
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.commit()

        # 创建所有表 (幂等)
        conn.executescript(SCHEMA_0)
        # PM Agent 架构演进: 新增表
        conn.executescript(SCHEMA_1)
        conn.commit()

        # 执行版本化迁移
        self._run_migrations(conn)

    def _get_schema_version(self, conn: sqlite3.Connection) -> int:
        """获取当前 schema 版本号。"""
        row = conn.execute(
            "SELECT value FROM schema_meta WHERE key = 'version'"
        ).fetchone()
        return int(row["value"]) if row else 0

    def _set_schema_version(self, conn: sqlite3.Connection, version: int):
        """设置 schema 版本号。"""
        conn.execute(
            "INSERT OR REPLACE INTO schema_meta (key, value) VALUES ('version', ?)",
            (str(version),),
        )
        conn.commit()

    def _run_migrations(self, conn: sqlite3.Connection):
        """执行所有待应用的迁移。"""
        current = self._get_schema_version(conn)
        if current >= SCHEMA_VERSION:
            return

        for ver in range(current + 1, SCHEMA_VERSION + 1):
            migration_fn = _MIGRATIONS.get(ver)
            if migration_fn:
                logger.info("执行 schema 迁移: v%d → v%d", ver - 1, ver)
                migration_fn(conn)
                self._set_schema_version(conn, ver)
            else:
                logger.warning("缺少迁移函数: v%d, 跳过", ver)
                self._set_schema_version(conn, ver)

        logger.info("数据库 schema 已更新到 v%d", SCHEMA_VERSION)

    # ── 主机记录 CRUD ───────────────────────────────────────────

    def upsert_host(self, record: HostRecord) -> Any:
        return self.repositories.hosts.upsert_host(record=record)

    def log_heartbeat(self, device_id: str, cpu: float, mem: float, disk: float) -> Any:
        return self.repositories.hosts.log_heartbeat(device_id=device_id, cpu=cpu, mem=mem, disk=disk)

    @staticmethod
    def _row_to_host(r) -> HostRecord:
        """将数据库行转换为 HostRecord。"""
        return HostRecord(
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
            rating_tier=r["rating_tier"] if "rating_tier" in r.keys() else "",
            rating_score=r["rating_score"] if "rating_score" in r.keys() else 0,
            rating_summary=r["rating_summary"] if "rating_summary" in r.keys() else "",
            code_version=r["code_version"] if "code_version" in r.keys() else "",
            version_ts=r["version_ts"] if "version_ts" in r.keys() else 0.0,
            source=r["source"] if "source" in r.keys() else "lan",
            federation=r["federation"] if "federation" in r.keys() else "",
        )

    def get_host(self, device_id: str) -> Optional[HostRecord]:
        return self.repositories.hosts.get_host(device_id=device_id)

    def list_hosts(self, source: Optional[str]=None) -> list[HostRecord]:
        return self.repositories.hosts.list_hosts(source=source)

    def set_offline(self, device_id: str) -> Any:
        return self.repositories.hosts.set_offline(device_id=device_id)

    def prune_offline(self, ttl: float) -> list[str]:
        return self.repositories.hosts.prune_offline(ttl=ttl)

    def cleanup_old_heartbeats(self, max_age_hours: int=24) -> Any:
        return self.repositories.hosts.cleanup_old_heartbeats(max_age_hours=max_age_hours)

    # ── 主机事件 & 评级 (Station Director) ──────────────────────────

    def log_host_event(self, device_id: str, event_type: str, detail: str='') -> Any:
        return self.repositories.hosts.log_host_event(device_id=device_id, event_type=event_type, detail=detail)

    def get_host_events(self, device_id: str=None, limit: int=50) -> list[dict]:
        return self.repositories.hosts.get_host_events(device_id=device_id, limit=limit)

    def update_host_rating(self, device_id: str, tier: str, score: int, summary: str) -> Any:
        return self.repositories.hosts.update_host_rating(device_id=device_id, tier=tier, score=score, summary=summary)

    def get_host_stats(self) -> dict:
        return self.repositories.hosts.get_host_stats()

    # ── Agent Card CRUD ──────────────────────────────────────────

    def upsert_agent(self, card: AgentCard) -> Any:
        return self.repositories.agents.upsert_agent(card=card)

    def get_agent(self, agent_id: str) -> Optional[AgentCard]:
        return self.repositories.agents.get_agent(agent_id=agent_id)

    def list_agents(self, status: str=None) -> list[AgentCard]:
        return self.repositories.agents.list_agents(status=status)

    def update_agent_status(self, agent_id: str, status: str, task_count: int=None) -> Any:
        return self.repositories.agents.update_agent_status(agent_id=agent_id, status=status, task_count=task_count)

    def find_idle_agent_with_skill(self, skill_name: str) -> Optional[AgentCard]:
        return self.repositories.agents.find_idle_agent_with_skill(skill_name=skill_name)

    # ── Task CRUD ───────────────────────────────────────────────

    def save_optimization_item(self, item: dict) -> None:
        return self.repositories.operations.save_optimization_item(item=item)

    def get_optimization_item(self, item_id: str) -> Optional[dict]:
        return self.repositories.operations.get_optimization_item(item_id=item_id)

    def list_optimization_items(self, status: str='', limit: int=100) -> list[dict]:
        return self.repositories.operations.list_optimization_items(status=status, limit=limit)

    def _optimization_row_to_dict(self, row: sqlite3.Row) -> dict:
        """Convert an optimization row to the API-facing dictionary."""
        try:
            result = json.loads(row["result_json"])
        except (TypeError, ValueError):
            result = {}
        return {
            "id": row["item_id"], "source": row["source"], "title": row["title"],
            "description": row["description"], "priority": row["priority"],
            "status": row["status"], "decision_reply": row["decision_reply"],
            "shadow_run_id": row["shadow_run_id"], "result": result,
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def save_task(self, task: Task) -> Any:
        return self.repositories.tasks.save_task(task=task)

    def get_task(self, task_id: str) -> Optional[Task]:
        return self.repositories.tasks.get_task(task_id=task_id)

    def update_task_status(self, task_id: str, status: str) -> Any:
        return self.repositories.tasks.update_task_status(task_id=task_id, status=status)

    def delete_task(self, task_id: str) -> bool:
        return self.repositories.tasks.delete_task(task_id=task_id)

    def delete_pm_agent(self, pm_id: str) -> bool:
        return self.repositories.teams.delete_pm_agent(pm_id=pm_id)

    def update_task_subtasks(self, task_id: str, subtasks: list) -> Any:
        return self.repositories.tasks.update_task_subtasks(task_id=task_id, subtasks=subtasks)

    def list_tasks(self, status: str=None, limit: int=50) -> list[Task]:
        return self.repositories.tasks.list_tasks(status=status, limit=limit)

    # ── Project CRUD ────────────────────────────────────────────

    def upsert_project(self, project) -> Any:
        return self.repositories.projects.upsert_project(project=project)

    def get_project(self, project_id: str) -> Any:
        return self.repositories.projects.get_project(project_id=project_id)

    def list_projects(self, status: str=None) -> list:
        return self.repositories.projects.list_projects(status=status)

    def delete_project(self, project_id: str) -> Any:
        return self.repositories.projects.delete_project(project_id=project_id)

    def update_project_budget(self, project_id: str, used_usd: float) -> Any:
        return self.repositories.projects.update_project_budget(project_id=project_id, used_usd=used_usd)

    def update_project_status(self, project_id: str, status: str) -> Any:
        return self.repositories.projects.update_project_status(project_id=project_id, status=status)

    def _row_to_project(self, row):
        """将数据库行转换为 Project 对象。"""
        from .protocol import Project
        try:
            charter = json.loads(row["charter"])
        except (KeyError, TypeError, json.JSONDecodeError):
            charter = {}
        if not isinstance(charter, dict):
            charter = {}
        try:
            roadmap = json.loads(row["roadmap"])
        except (KeyError, TypeError, json.JSONDecodeError):
            roadmap = []
        if not isinstance(roadmap, list):
            roadmap = []
        try:
            decisions = json.loads(row["decisions"])
        except (KeyError, TypeError, json.JSONDecodeError):
            decisions = []
        if not isinstance(decisions, list):
            decisions = []
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
            charter=charter,
            roadmap=roadmap,
            decisions=decisions,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ── Usage Log ───────────────────────────────────────────────

    def record_usage(self, project_id: str, task_id: str, subtask_id: str, model: str, input_tokens: int, output_tokens: int, cost_usd: float) -> Any:
        return self.repositories.projects.record_usage(project_id=project_id, task_id=task_id, subtask_id=subtask_id, model=model, input_tokens=input_tokens, output_tokens=output_tokens, cost_usd=cost_usd)

    def get_usage_log(self, project_id: str, limit: int=100) -> list[dict]:
        return self.repositories.projects.get_usage_log(project_id=project_id, limit=limit)

    # ── Skill Registry CRUD ──────────────────────────────────────

    def upsert_skill(self, skill_id: str, name: str, description: str, category: str, tags: list, default_access: list, content_path: str, version: str='1.0', origin: str='builtin') -> Any:
        return self.repositories.skills.upsert_skill(skill_id=skill_id, name=name, description=description, category=category, tags=tags, default_access=default_access, content_path=content_path, version=version, origin=origin)

    def get_skill(self, skill_id: str) -> Optional[dict]:
        return self.repositories.skills.get_skill(skill_id=skill_id)

    def list_skills(self, category: str=None) -> list[dict]:
        return self.repositories.skills.list_skills(category=category)

    def delete_skill(self, skill_id: str) -> Any:
        return self.repositories.skills.delete_skill(skill_id=skill_id)

    # ── 多用户 (iter-63 团队场景深化) ─────────────────────────

    def list_users_db(self) -> list[dict]:
        return self.repositories.users.list_users_db()

    def upsert_user_db(self, name: str, role: str, token_hash: str, token_tail4: str) -> Any:
        return self.repositories.users.upsert_user_db(name=name, role=role, token_hash=token_hash, token_tail4=token_tail4)

    def update_user_role_db(self, name: str, role: str) -> Any:
        return self.repositories.users.update_user_role_db(name=name, role=role)

    def delete_user_db(self, name: str) -> Any:
        return self.repositories.users.delete_user_db(name=name)

    @staticmethod
    def _row_to_skill(row) -> dict:
        """将数据库行转换为技能字典。"""
        return {
            "skill_id": row["skill_id"],
            "name": row["name"],
            "description": row["description"],
            "category": row["category"],
            "tags": json.loads(row["tags"]) if row["tags"] else [],
            "default_access": json.loads(row["default_access"]) if row["default_access"] else ["all"],
            "content_path": row["content_path"],
            "origin": row["origin"] if "origin" in row.keys() else "builtin",
            "version": row["version"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    # ── Skill Assignment CRUD ────────────────────────────────────

    def assign_skill(self, skill_id: str, assignee_type: str, assignee_id: str) -> Any:
        return self.repositories.skills.assign_skill(skill_id=skill_id, assignee_type=assignee_type, assignee_id=assignee_id)

    def revoke_skill(self, skill_id: str, assignee_type: str, assignee_id: str) -> Any:
        return self.repositories.skills.revoke_skill(skill_id=skill_id, assignee_type=assignee_type, assignee_id=assignee_id)

    def get_skill_assignments(self, skill_id: str) -> list[dict]:
        return self.repositories.skills.get_skill_assignments(skill_id=skill_id)

    def get_skills_for_assignee(self, assignee_type: str, assignee_id: str) -> list[str]:
        return self.repositories.skills.get_skills_for_assignee(assignee_type=assignee_type, assignee_id=assignee_id)

    # ── PM Agent CRUD (架构演进) ──────────────────────────────────

    def upsert_pm_agent(self, pm: PMAgent) -> Any:
        return self.repositories.teams.upsert_pm_agent(pm=pm)

    def get_pm_agent(self, pm_id: str) -> Optional[PMAgent]:
        return self.repositories.teams.get_pm_agent(pm_id=pm_id)

    def list_pm_agents(self, status: str=None) -> list[PMAgent]:
        return self.repositories.teams.list_pm_agents(status=status)

    def update_pm_status(self, pm_id: str, status: str, team_structure: dict=None, task_list: list=None, collaboration_mode: str=None) -> Any:
        return self.repositories.teams.update_pm_status(pm_id=pm_id, status=status, team_structure=team_structure, task_list=task_list, collaboration_mode=collaboration_mode)

    # ── Agent Team CRUD ──────────────────────────────────────────

    def upsert_team(self, team: AgentTeam) -> Any:
        return self.repositories.teams.upsert_team(team=team)

    def get_team(self, team_id: str) -> Optional[AgentTeam]:
        return self.repositories.teams.get_team(team_id=team_id)

    def get_teams_by_pm(self, pm_id: str) -> list[AgentTeam]:
        return self.repositories.teams.get_teams_by_pm(pm_id=pm_id)

    def list_teams(self, pm_id: str=None) -> list[AgentTeam]:
        return self.repositories.teams.list_teams(pm_id=pm_id)

    # ── Progress Report CRUD ─────────────────────────────────────

    def save_progress_report(self, report: ProgressReport) -> Any:
        return self.repositories.teams.save_progress_report(report=report)

    # ── Chat History CRUD ─────────────────────────────────────────

    def save_chat_message(self, role: str, content: str, action_taken: str='', timestamp: float=0) -> Any:
        return self.repositories.conversations.save_chat_message(role=role, content=content, action_taken=action_taken, timestamp=timestamp)

    def get_chat_history(self, limit: int=100) -> list[dict]:
        return self.repositories.conversations.get_chat_history(limit=limit)

    def clear_chat_history(self) -> Any:
        return self.repositories.conversations.clear_chat_history()

    def get_progress_reports(self, pm_id: str, limit: int=50) -> list[dict]:
        return self.repositories.teams.get_progress_reports(pm_id=pm_id, limit=limit)

    # ── 优化14: 任务上下文记忆 CRUD ──────────────────────────────

    def save_task_memory(self, task_name: str, task_keywords: list, task_type: str, collaboration_mode: str, team_size: int, duration_secs: float, success: bool, error_pattern: str='', boss_feedback: str='', device_id: str='') -> Any:
        return self.repositories.conversations.save_task_memory(task_name=task_name, task_keywords=task_keywords, task_type=task_type, collaboration_mode=collaboration_mode, team_size=team_size, duration_secs=duration_secs, success=success, error_pattern=error_pattern, boss_feedback=boss_feedback, device_id=device_id)

    def query_task_memory(self, task_type: str='', keyword: str='', limit: int=10) -> list[dict]:
        return self.repositories.conversations.query_task_memory(task_type=task_type, keyword=keyword, limit=limit)

    def get_task_memory_stats(self, task_type: str='') -> dict:
        return self.repositories.conversations.get_task_memory_stats(task_type=task_type)

    # ── R1: 模型资源用量 ────────────────────────────────────────

    def insert_resource_usage(self, resource_id: str, model_id: str, plan_type: str, input_tokens: int, output_tokens: int, cost: float, usage_id: str='', task_id: str='', project_id: str='') -> bool:
        return self.repositories.resources.insert_resource_usage(resource_id=resource_id, model_id=model_id, plan_type=plan_type, input_tokens=input_tokens, output_tokens=output_tokens, cost=cost, usage_id=usage_id, task_id=task_id, project_id=project_id)

    def query_unreported_usage(self, limit: int=200) -> list:
        return self.repositories.resources.query_unreported_usage(limit=limit)

    def mark_usage_reported(self, ids: list) -> None:
        return self.repositories.resources.mark_usage_reported(ids=ids)

    def sum_resource_usage(self, resource_id: str, since_ts: float) -> dict:
        return self.repositories.resources.sum_resource_usage(resource_id=resource_id, since_ts=since_ts)

    def query_cost_by_task(self, limit: int=100) -> list:
        return self.repositories.resources.query_cost_by_task(limit=limit)

    def avg_tokens_per_task(self, days: int=30) -> dict:
        return self.repositories.resources.avg_tokens_per_task(days=days)

    # ── Graph Checkpoint CRUD ───────────────────────────────────

    def save_checkpoint(self, checkpoint_id: str, task_id: str, phase: str, dag_json: str, context_json: str, history_json: str) -> Any:
        return self.repositories.checkpoints.save_checkpoint(checkpoint_id=checkpoint_id, task_id=task_id, phase=phase, dag_json=dag_json, context_json=context_json, history_json=history_json)

    def get_latest_checkpoint(self, task_id: str) -> Optional[dict]:
        return self.repositories.checkpoints.get_latest_checkpoint(task_id=task_id)

    def list_checkpoints(self, task_id: str) -> list:
        return self.repositories.checkpoints.list_checkpoints(task_id=task_id)

    def delete_checkpoints(self, task_id: str) -> Any:
        return self.repositories.checkpoints.delete_checkpoints(task_id=task_id)

    # ── PM 执行态快照 CRUD (iter-53 断点恢复) ────────────────────

    def save_pm_snapshot(self, pm_id: str, task_id: str, phase: str, state_json: str) -> None:
        return self.repositories.checkpoints.save_pm_snapshot(pm_id=pm_id, task_id=task_id, phase=phase, state_json=state_json)

    def get_pm_snapshot(self, pm_id: str) -> Optional[dict]:
        return self.repositories.checkpoints.get_pm_snapshot(pm_id=pm_id)

    def get_pm_snapshot_by_task(self, task_id: str) -> Optional[dict]:
        return self.repositories.checkpoints.get_pm_snapshot_by_task(task_id=task_id)

    def delete_pm_snapshot(self, pm_id: str) -> None:
        return self.repositories.checkpoints.delete_pm_snapshot(pm_id=pm_id)

    # ── P0/P1: LLM 调用审计 ─────────────────────────────────────

    def insert_llm_call(self, call_type: str, model: str, input_tokens: int, output_tokens: int, ttft_ms: float, total_ms: float, status: str='ok', task_id: str='', error: str='') -> None:
        return self.repositories.metrics.insert_llm_call(call_type=call_type, model=model, input_tokens=input_tokens, output_tokens=output_tokens, ttft_ms=ttft_ms, total_ms=total_ms, status=status, task_id=task_id, error=error)

    def query_llm_metrics(self, hours: float=1.0) -> dict:
        return self.repositories.metrics.query_llm_metrics(hours=hours)

    def query_llm_recent(self, limit: int=50) -> list[dict]:
        return self.repositories.metrics.query_llm_recent(limit=limit)

    # ── iter-54: 日志容量修剪 (补强#2) ──────────────────────────

    def prune_logs(self, retention_days: float=30.0) -> dict:
        return self.repositories.operations.prune_logs(retention_days=retention_days)

    def vacuum(self) -> None:
        return self.repositories.operations.vacuum()

    # ── iter-47: 错误记录持久化 (F1.4) ──────────────────────────

    def save_error_record(self, record: dict) -> None:
        return self.repositories.operations.save_error_record(record=record)

    def query_error_history(self, limit: int=100, module: str='') -> list[dict]:
        return self.repositories.operations.query_error_history(limit=limit, module=module)

    # ── iter-49: 自愈动作执行日志 (F4.2 修复环节) ──────────────

    def save_heal_record(self, record: dict) -> None:
        return self.repositories.operations.save_heal_record(record=record)

    def query_heal_history(self, limit: int=50) -> list[dict]:
        return self.repositories.operations.query_heal_history(limit=limit)
