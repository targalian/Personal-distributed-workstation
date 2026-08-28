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
from .logger import get_logger

logger = get_logger("database")

# ── Schema 版本管理 ───────────────────────────────────────────
# 每次 schema 变更时递增 SCHEMA_VERSION 并添加对应的迁移函数。
# 迁移函数签名: (conn: sqlite3.Connection) -> None

SCHEMA_VERSION = 8


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


# 迁移注册表: version → 迁移函数
_MIGRATIONS: dict[int, callable] = {
    1: _migration_v1,
    2: _migration_v2,
    3: _migration_v3,
    4: _migration_v4,
    5: _migration_v5,
    6: _migration_v6,
    7: _migration_v7,
    8: _migration_v8,
}


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
                latency_ms    REAL,
                code_version  TEXT NOT NULL DEFAULT '',
                version_ts    REAL NOT NULL DEFAULT 0
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

            CREATE TABLE IF NOT EXISTS host_events (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id   TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                timestamp   REAL NOT NULL,
                detail      TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_host_events_device
                ON host_events(device_id, timestamp);

            CREATE TABLE IF NOT EXISTS skills (
                skill_id       TEXT PRIMARY KEY,
                name           TEXT NOT NULL DEFAULT '',
                description    TEXT NOT NULL DEFAULT '',
                category       TEXT NOT NULL DEFAULT 'general',
                tags           TEXT NOT NULL DEFAULT '[]',
                default_access TEXT NOT NULL DEFAULT '["all"]',
                content_path   TEXT NOT NULL DEFAULT '',
                origin         TEXT NOT NULL DEFAULT 'builtin',
                version        TEXT NOT NULL DEFAULT '1.0',
                created_at     REAL NOT NULL DEFAULT 0,
                updated_at     REAL NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS skill_assignments (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_id      TEXT NOT NULL,
                assignee_type TEXT NOT NULL,
                assignee_id   TEXT NOT NULL,
                assigned_at   REAL NOT NULL DEFAULT 0,
                UNIQUE(skill_id, assignee_type, assignee_id)
            );

            CREATE INDEX IF NOT EXISTS idx_skill_assignments
                ON skill_assignments(assignee_type, assignee_id);
        """)
        # PM Agent 架构演进: 新增表
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS pm_agents (
                pm_id              TEXT PRIMARY KEY,
                agent_name         TEXT NOT NULL DEFAULT '',
                task_id            TEXT NOT NULL DEFAULT '',
                project_id         TEXT NOT NULL DEFAULT '',
                device_id          TEXT NOT NULL DEFAULT '',
                hostname           TEXT NOT NULL DEFAULT '',
                ip                 TEXT NOT NULL DEFAULT '',
                api_port           INTEGER NOT NULL DEFAULT 0,
                status             TEXT NOT NULL DEFAULT 'starting',
                team_structure      TEXT NOT NULL DEFAULT '{}',
                task_list           TEXT NOT NULL DEFAULT '[]',
                collaboration_mode TEXT NOT NULL DEFAULT '',
                created_at         REAL NOT NULL DEFAULT 0,
                updated_at         REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_pm_agents_status ON pm_agents(status);
            CREATE INDEX IF NOT EXISTS idx_pm_agents_task ON pm_agents(task_id);

            CREATE TABLE IF NOT EXISTS agent_teams (
                team_id        TEXT PRIMARY KEY,
                pm_id          TEXT NOT NULL DEFAULT '',
                team_name      TEXT NOT NULL DEFAULT '',
                team_type      TEXT NOT NULL DEFAULT '',
                device_id      TEXT NOT NULL DEFAULT '',
                parent_team_id TEXT NOT NULL DEFAULT '',
                members        TEXT NOT NULL DEFAULT '[]',
                status         TEXT NOT NULL DEFAULT 'pending',
                current_task   TEXT NOT NULL DEFAULT '',
                created_at     REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_agent_teams_pm ON agent_teams(pm_id);

            CREATE TABLE IF NOT EXISTS progress_reports (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                pm_id         TEXT NOT NULL DEFAULT '',
                reporter_id   TEXT NOT NULL DEFAULT '',
                reporter_type TEXT NOT NULL DEFAULT '',
                task_name     TEXT NOT NULL DEFAULT '',
                progress      REAL NOT NULL DEFAULT 0,
                status        TEXT NOT NULL DEFAULT 'in_progress',
                message       TEXT NOT NULL DEFAULT '',
                timestamp     REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_progress_pm ON progress_reports(pm_id, timestamp);

            CREATE TABLE IF NOT EXISTS chat_history (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                role        TEXT NOT NULL DEFAULT 'user',
                content     TEXT NOT NULL DEFAULT '',
                action_taken TEXT NOT NULL DEFAULT '',
                timestamp   REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_chat_history_ts
                ON chat_history(timestamp);

            -- 优化14: 任务上下文记忆表
            CREATE TABLE IF NOT EXISTS task_memory (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                task_name       TEXT NOT NULL DEFAULT '',
                task_keywords   TEXT NOT NULL DEFAULT '[]',
                task_type       TEXT NOT NULL DEFAULT '',
                collaboration_mode TEXT NOT NULL DEFAULT '',
                team_size       INTEGER NOT NULL DEFAULT 0,
                duration_secs   REAL NOT NULL DEFAULT 0,
                success         INTEGER NOT NULL DEFAULT 1,
                error_pattern   TEXT NOT NULL DEFAULT '',
                boss_feedback   TEXT NOT NULL DEFAULT '',
                device_id       TEXT NOT NULL DEFAULT '',
                created_at      REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_task_memory_type
                ON task_memory(task_type, success);
            CREATE INDEX IF NOT EXISTS idx_task_memory_ts
                ON task_memory(created_at);

            -- R1: 模型资源用量日志 (每次 LLM 调用一行, 可审计可聚合)
            -- R3: usage_id 幂等键 (跨主机上报去重), reported 上报游标
            -- R6: task_id/project_id 成本归因
            CREATE TABLE IF NOT EXISTS resource_usage_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                resource_id     TEXT NOT NULL DEFAULT '',
                model_id        TEXT NOT NULL DEFAULT '',
                plan_type       TEXT NOT NULL DEFAULT '',
                input_tokens    INTEGER NOT NULL DEFAULT 0,
                output_tokens   INTEGER NOT NULL DEFAULT 0,
                cost            REAL NOT NULL DEFAULT 0,
                created_at      REAL NOT NULL DEFAULT 0,
                usage_id        TEXT NOT NULL DEFAULT '',
                reported        INTEGER NOT NULL DEFAULT 0,
                task_id         TEXT NOT NULL DEFAULT '',
                project_id      TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_resource_usage_rid_ts
                ON resource_usage_log(resource_id, created_at);
            -- 注意: task_id 部分索引只能建在 _migration_v3 中
            -- (executescript 先于迁移执行, 旧库无 task_id 列会炸)

            -- Graph Engine: 图执行检查点表
            CREATE TABLE IF NOT EXISTS graph_checkpoints (
                checkpoint_id  TEXT PRIMARY KEY,
                task_id        TEXT NOT NULL,
                phase          TEXT NOT NULL DEFAULT '',
                dag_json       TEXT NOT NULL DEFAULT '{}',
                context_json   TEXT NOT NULL DEFAULT '{}',
                history_json   TEXT NOT NULL DEFAULT '[]',
                created_at     REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_checkpoint_task
                ON graph_checkpoints(task_id, created_at);

            -- iter-53: PM 执行态快照表 (断点恢复 — 重启后从快照续跑)
            CREATE TABLE IF NOT EXISTS pm_snapshots (
                pm_id      TEXT PRIMARY KEY,
                task_id    TEXT NOT NULL,
                phase      TEXT NOT NULL DEFAULT '',
                state_json TEXT NOT NULL DEFAULT '{}',
                updated_at REAL NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_pm_snapshot_task
                ON pm_snapshots(task_id, updated_at);

            -- P0/P1: LLM 调用审计表 (运行时性能追踪)
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
            );

            CREATE INDEX IF NOT EXISTS idx_llm_call_model_ts
                ON llm_call_log(model, created_at);
            CREATE INDEX IF NOT EXISTS idx_llm_call_status
                ON llm_call_log(status, created_at);

            -- iter-47: 错误记录持久化表 (F1.4 落盘, 重启不丢诊断历史)
            CREATE TABLE IF NOT EXISTS error_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    REAL NOT NULL DEFAULT 0,
                module       TEXT NOT NULL DEFAULT '',
                error_type   TEXT NOT NULL DEFAULT '',
                message      TEXT NOT NULL DEFAULT '',
                context_json TEXT NOT NULL DEFAULT '{}',
                traceback    TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_error_log_module_ts
                ON error_log(module, timestamp);

            -- iter-49: 自愈动作执行日志表 (F4.2 修复环节, 跨重启保留)
            CREATE TABLE IF NOT EXISTS heal_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    REAL NOT NULL DEFAULT 0,
                category     TEXT NOT NULL DEFAULT '',
                action       TEXT NOT NULL DEFAULT '',
                result       TEXT NOT NULL DEFAULT '',
                detail       TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_heal_log_ts
                ON heal_log(timestamp);
        """)
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

    def upsert_host(self, record: HostRecord):
        """插入或更新主机记录。"""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO hosts (
                device_id, device_name, role, hostname, platform,
                ip, api_port, cpu_count, memory_total_mb, disk_total_gb,
                cpu_percent, memory_percent, disk_percent,
                shared_folder, shared_file_count, online,
                registered_at, last_seen, latency_ms,
                rating_tier, rating_score, rating_summary,
                code_version, version_ts
            ) VALUES (
                ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?
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
                latency_ms=excluded.latency_ms,
                rating_tier=excluded.rating_tier,
                rating_score=excluded.rating_score,
                rating_summary=excluded.rating_summary,
                code_version=excluded.code_version,
                version_ts=excluded.version_ts
        """, (
            record.device_id, record.device_name, record.role,
            record.hostname, record.platform,
            record.ip, record.api_port,
            record.cpu_count, record.memory_total_mb, record.disk_total_gb,
            record.cpu_percent, record.memory_percent, record.disk_percent,
            record.shared_folder, record.shared_file_count,
            1 if record.online else 0,
            record.registered_at, record.last_seen, record.latency_ms,
            record.rating_tier, record.rating_score, record.rating_summary,
            record.code_version, record.version_ts,
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
        )

    def get_host(self, device_id: str) -> Optional[HostRecord]:
        """查询单台主机记录。"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM hosts WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row:
            return self._row_to_host(row)
        return None

    def list_hosts(self) -> list[HostRecord]:
        """列出所有主机记录。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM hosts ORDER BY online DESC, device_name ASC"
        ).fetchall()
        return [self._row_to_host(r) for r in rows]

    def set_offline(self, device_id: str):
        """标记主机为离线。"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE hosts SET online = 0 WHERE device_id = ?", (device_id,)
        )
        conn.commit()

    def prune_offline(self, ttl: float) -> list[str]:
        """将超过 TTL 未活跃的主机标记为离线。返回新标记离线的主机 ID 列表。"""
        conn = self._get_conn()
        cutoff = time.time() - ttl
        # 先查出即将离线的设备
        rows = conn.execute(
            "SELECT device_id FROM hosts WHERE last_seen < ? AND online = 1",
            (cutoff,),
        ).fetchall()
        gone_ids = [r["device_id"] for r in rows]
        conn.execute(
            "UPDATE hosts SET online = 0 WHERE last_seen < ? AND online = 1",
            (cutoff,),
        )
        conn.commit()
        return gone_ids

    def cleanup_old_heartbeats(self, max_age_hours: int = 24):
        """清理超过指定时长的心跳历史。"""
        conn = self._get_conn()
        cutoff = time.time() - max_age_hours * 3600
        conn.execute(
            "DELETE FROM heartbeat_log WHERE timestamp < ?", (cutoff,)
        )
        conn.commit()

    # ── 主机事件 & 评级 (Station Director) ──────────────────────────

    def log_host_event(self, device_id: str, event_type: str, detail: str = ""):
        """记录主机事件 (join/leave/register/rating_change)。"""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO host_events (device_id, event_type, timestamp, detail) VALUES (?, ?, ?, ?)",
            (device_id, event_type, time.time(), detail),
        )
        conn.commit()

    def get_host_events(self, device_id: str = None, limit: int = 50) -> list[dict]:
        """查询主机事件历史。device_id=None 时返回全站事件。"""
        conn = self._get_conn()
        if device_id:
            rows = conn.execute(
                "SELECT * FROM host_events WHERE device_id = ? ORDER BY timestamp DESC LIMIT ?",
                (device_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM host_events ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def update_host_rating(self, device_id: str, tier: str, score: int, summary: str):
        """更新主机评级。"""
        conn = self._get_conn()
        old = conn.execute(
            "SELECT rating_tier FROM hosts WHERE device_id = ?", (device_id,)
        ).fetchone()
        conn.execute(
            "UPDATE hosts SET rating_tier = ?, rating_score = ?, rating_summary = ? WHERE device_id = ?",
            (tier, score, summary, device_id),
        )
        conn.commit()
        # 记录评级变更事件
        old_tier = old["rating_tier"] if old else ""
        if old_tier and old_tier != tier:
            self.log_host_event(device_id, "rating_change", f"{old_tier}->{tier}")

    def get_host_stats(self) -> dict:
        """返回主机舰队统计摘要。"""
        conn = self._get_conn()
        total = conn.execute("SELECT COUNT(*) as c FROM hosts").fetchone()["c"]
        online = conn.execute("SELECT COUNT(*) as c FROM hosts WHERE online = 1").fetchone()["c"]
        offline = total - online
        tiers = {}
        for t in ("S", "A", "B", "C", "D"):
            count = conn.execute(
                "SELECT COUNT(*) as c FROM hosts WHERE rating_tier = ? AND online = 1", (t,)
            ).fetchone()["c"]
            tiers[t] = count
        return {
            "total": total,
            "online": online,
            "offline": offline,
            "tiers": tiers,
        }

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
                              status, subtasks, created_at, completed_at, created_by, project_id, pm_agent_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                name=excluded.name, description=excluded.description,
                input_data=excluded.input_data, output_data=excluded.output_data,
                status=excluded.status, subtasks=excluded.subtasks,
                completed_at=excluded.completed_at, project_id=excluded.project_id,
                pm_agent_id=excluded.pm_agent_id
        """, (
            task.task_id, task.name, task.description,
            json.dumps(task.input_data), json.dumps(task.output_data),
            task.status, json.dumps(task.subtasks),
            task.created_at, task.completed_at, task.created_by,
            task.project_id, task.pm_agent_id,
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
            pm_agent_id=row["pm_agent_id"] if "pm_agent_id" in row.keys() else "",
        )

    def update_task_status(self, task_id: str, status: str):
        """更新任务状态 (用于取消/暂停)。"""
        conn = self._get_conn()
        conn.execute(
            "UPDATE tasks SET status = ? WHERE task_id = ?",
            (status, task_id),
        )
        conn.commit()

    def delete_task(self, task_id: str) -> bool:
        """彻底删除任务及关联的 PM Agent 和 Checkpoint 记录。"""
        conn = self._get_conn()
        conn.execute("DELETE FROM pm_agents WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM graph_checkpoints WHERE task_id = ?", (task_id,))
        conn.execute("DELETE FROM pm_snapshots WHERE task_id = ?", (task_id,))
        cursor = conn.execute("DELETE FROM tasks WHERE task_id = ?", (task_id,))
        conn.commit()
        return cursor.rowcount > 0

    def delete_pm_agent(self, pm_id: str) -> bool:
        """删除指定 PM Agent 记录。"""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM pm_agents WHERE pm_id = ?", (pm_id,))
        conn.commit()
        return cursor.rowcount > 0

    def update_task_subtasks(self, task_id: str, subtasks: list):
        """更新任务的子任务列表 (PM 同步进度用)。"""
        import json
        conn = self._get_conn()
        conn.execute(
            "UPDATE tasks SET subtasks = ? WHERE task_id = ?",
            (json.dumps(subtasks, ensure_ascii=False), task_id),
        )
        conn.commit()

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
                pm_agent_id=r["pm_agent_id"] if "pm_agent_id" in r.keys() else "",
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

    # ── Skill Registry CRUD ──────────────────────────────────────

    def upsert_skill(self, skill_id: str, name: str, description: str,
                      category: str, tags: list, default_access: list,
                      content_path: str, version: str = "1.0",
                      origin: str = "builtin"):
        """插入或更新技能记录。

        origin 仅首次插入时写入 (iter-61): 更新不覆盖来源标记,
        防止重扫内置目录把市场安装技能误标回 builtin。
        """
        conn = self._get_conn()
        now = time.time()
        conn.execute("""
            INSERT INTO skills (
                skill_id, name, description, category, tags,
                default_access, content_path, origin, version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(skill_id) DO UPDATE SET
                name=excluded.name,
                description=excluded.description,
                category=excluded.category,
                tags=excluded.tags,
                default_access=excluded.default_access,
                content_path=excluded.content_path,
                version=excluded.version,
                updated_at=excluded.updated_at
        """, (
            skill_id, name, description, category,
            json.dumps(tags, ensure_ascii=False),
            json.dumps(default_access, ensure_ascii=False),
            content_path, origin, version, now, now,
        ))
        conn.commit()

    def get_skill(self, skill_id: str) -> Optional[dict]:
        """查询单个技能记录。"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM skills WHERE skill_id = ?", (skill_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_skill(row)

    def list_skills(self, category: str = None) -> list[dict]:
        """列出所有技能,可按分类过滤。"""
        conn = self._get_conn()
        if category:
            rows = conn.execute(
                "SELECT * FROM skills WHERE category = ? ORDER BY updated_at DESC",
                (category,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM skills ORDER BY updated_at DESC"
            ).fetchall()
        return [self._row_to_skill(r) for r in rows]

    def delete_skill(self, skill_id: str):
        """删除技能及其所有分配记录。"""
        conn = self._get_conn()
        conn.execute("DELETE FROM skills WHERE skill_id = ?", (skill_id,))
        conn.execute("DELETE FROM skill_assignments WHERE skill_id = ?", (skill_id,))
        conn.commit()

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

    def assign_skill(self, skill_id: str, assignee_type: str, assignee_id: str):
        """分配技能给角色/Agent/主机。"""
        conn = self._get_conn()
        conn.execute(
            """INSERT OR IGNORE INTO skill_assignments
               (skill_id, assignee_type, assignee_id, assigned_at)
               VALUES (?, ?, ?, ?)""",
            (skill_id, assignee_type, assignee_id, time.time()),
        )
        conn.commit()

    def revoke_skill(self, skill_id: str, assignee_type: str, assignee_id: str):
        """撤销技能分配。"""
        conn = self._get_conn()
        conn.execute(
            """DELETE FROM skill_assignments
               WHERE skill_id = ? AND assignee_type = ? AND assignee_id = ?""",
            (skill_id, assignee_type, assignee_id),
        )
        conn.commit()

    def get_skill_assignments(self, skill_id: str) -> list[dict]:
        """查询技能的所有分配记录。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM skill_assignments WHERE skill_id = ? ORDER BY assigned_at DESC",
            (skill_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_skills_for_assignee(self, assignee_type: str, assignee_id: str) -> list[str]:
        """查询指定角色/Agent/主机被分配的技能 ID 列表。"""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT skill_id FROM skill_assignments
               WHERE assignee_type = ? AND assignee_id = ?""",
            (assignee_type, assignee_id),
        ).fetchall()
        return [r["skill_id"] for r in rows]

    # ── PM Agent CRUD (架构演进) ──────────────────────────────────

    def upsert_pm_agent(self, pm: PMAgent):
        """插入或更新 PM Agent 记录。"""
        conn = self._get_conn()
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
        conn = self._get_conn()
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
        conn = self._get_conn()
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
                         collaboration_mode: str = None):
        """更新 PM Agent 状态 (由 Worker 上报)。"""
        conn = self._get_conn()
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

    # ── Agent Team CRUD ──────────────────────────────────────────

    def upsert_team(self, team: AgentTeam):
        """插入或更新团队记录。"""
        conn = self._get_conn()
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
        conn = self._get_conn()
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
        conn = self._get_conn()
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
            return self.get_teams_by_pm(pm_id)
        conn = self._get_conn()
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

    # ── Progress Report CRUD ─────────────────────────────────────

    def save_progress_report(self, report: ProgressReport):
        """保存进度报告。"""
        conn = self._get_conn()
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

    # ── Chat History CRUD ─────────────────────────────────────────

    def save_chat_message(self, role: str, content: str, action_taken: str = "", timestamp: float = 0):
        """保存一条聊天记录到持久化存储。"""
        conn = self._get_conn()
        ts = timestamp or time.time()
        conn.execute(
            "INSERT INTO chat_history (role, content, action_taken, timestamp) VALUES (?, ?, ?, ?)",
            (role, content, action_taken, ts),
        )
        conn.commit()

    def get_chat_history(self, limit: int = 100) -> list[dict]:
        """查询最近的聊天记录。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT role, content, action_taken, timestamp FROM chat_history ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def clear_chat_history(self):
        """清空全部聊天记录。"""
        conn = self._get_conn()
        conn.execute("DELETE FROM chat_history")
        conn.commit()

    def get_progress_reports(self, pm_id: str, limit: int = 50) -> list[dict]:
        """查询 PM 的进度报告列表。"""
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT * FROM progress_reports WHERE pm_id = ? ORDER BY timestamp DESC LIMIT ?",
            (pm_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 优化14: 任务上下文记忆 CRUD ──────────────────────────────

    def save_task_memory(self, task_name: str, task_keywords: list,
                         task_type: str, collaboration_mode: str,
                         team_size: int, duration_secs: float,
                         success: bool, error_pattern: str = "",
                         boss_feedback: str = "", device_id: str = ""):
        """保存一条任务记忆 (PM 完成后调用)。

        记录任务模式, 用于未来同类任务的决策参考:
        - 什么类型的任务适合什么协作模式
        - 历史上哪些任务失败过, 失败原因是什么
        - Boss 对交付物的反馈偏好
        """
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO task_memory (
                task_name, task_keywords, task_type, collaboration_mode,
                team_size, duration_secs, success, error_pattern,
                boss_feedback, device_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            task_name, json.dumps(task_keywords, ensure_ascii=False),
            task_type, collaboration_mode, team_size, duration_secs,
            1 if success else 0, error_pattern, boss_feedback,
            device_id, time.time(),
        ))
        conn.commit()

    def query_task_memory(self, task_type: str = "", keyword: str = "",
                          limit: int = 10) -> list[dict]:
        """查询任务记忆 (按类型或关键词)。

        用于新任务提交时参考历史经验:
        - 同类型任务的历史成功/失败率
        - 推荐的协作模式和团队规模
        - 常见错误模式预警
        """
        conn = self._get_conn()
        if task_type:
            rows = conn.execute(
                "SELECT * FROM task_memory WHERE task_type = ? ORDER BY created_at DESC LIMIT ?",
                (task_type, limit),
            ).fetchall()
        elif keyword:
            rows = conn.execute(
                "SELECT * FROM task_memory WHERE task_keywords LIKE ? ORDER BY created_at DESC LIMIT ?",
                (f"%{keyword}%", limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM task_memory ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        results = []
        for r in rows:
            d = dict(r)
            d["task_keywords"] = json.loads(d["task_keywords"]) if d["task_keywords"] else []
            d["success"] = bool(d["success"])
            results.append(d)
        return results

    def get_task_memory_stats(self, task_type: str = "") -> dict:
        """统计任务记忆 (成功率、平均耗时、推荐模式)。"""
        conn = self._get_conn()
        if task_type:
            rows = conn.execute(
                "SELECT * FROM task_memory WHERE task_type = ?", (task_type,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM task_memory").fetchall()

        if not rows:
            return {"total": 0, "success_rate": 0, "avg_duration": 0, "recommended_mode": ""}

        total = len(rows)
        success_count = sum(1 for r in rows if r["success"])
        avg_duration = sum(r["duration_secs"] for r in rows) / total if total else 0

        # 统计最常用的协作模式
        mode_counts: dict[str, int] = {}
        for r in rows:
            mode = r["collaboration_mode"]
            if mode:
                mode_counts[mode] = mode_counts.get(mode, 0) + 1
        recommended_mode = max(mode_counts, key=mode_counts.get) if mode_counts else ""

        # 常见错误模式
        error_patterns: dict[str, int] = {}
        for r in rows:
            ep = r["error_pattern"]
            if ep:
                error_patterns[ep] = error_patterns.get(ep, 0) + 1

        return {
            "total": total,
            "success_rate": success_count / total if total else 0,
            "avg_duration": avg_duration,
            "recommended_mode": recommended_mode,
            "common_errors": sorted(error_patterns.items(), key=lambda x: -x[1])[:3],
        }

    # ── R1: 模型资源用量 ────────────────────────────────────────

    def insert_resource_usage(self, resource_id: str, model_id: str,
                              plan_type: str, input_tokens: int,
                              output_tokens: int, cost: float,
                              usage_id: str = "", task_id: str = "",
                              project_id: str = "") -> bool:
        """写入一条 LLM 调用用量记录。

        Args:
            usage_id: 幂等键 (R3 跨主机上报去重); 非空且已存在 → 跳过。
            task_id/project_id: 成本归因 (R6); 留空表示无归因上下文。

        Returns:
            是否实际写入 (重复上报返回 False)
        """
        conn = self._get_conn()
        if usage_id:
            dup = conn.execute(
                "SELECT 1 FROM resource_usage_log WHERE usage_id = ? LIMIT 1",
                (usage_id,)).fetchone()
            if dup:
                return False
        conn.execute("""
            INSERT INTO resource_usage_log
                (resource_id, model_id, plan_type, input_tokens, output_tokens,
                 cost, created_at, usage_id, task_id, project_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (resource_id, model_id, plan_type, int(input_tokens),
              int(output_tokens), cost, time.time(), usage_id,
              task_id, project_id))
        conn.commit()
        return True

    def query_unreported_usage(self, limit: int = 200) -> list:
        """查询未上报 Secretary 的用量记录 (R3 跨主机上报)。"""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT id, usage_id, model_id, input_tokens, output_tokens,
                   task_id, project_id
            FROM resource_usage_log
            WHERE reported = 0
            ORDER BY id ASC LIMIT ?
        """, (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def mark_usage_reported(self, ids: list) -> None:
        """标记用量记录已上报 (R3)。"""
        if not ids:
            return
        conn = self._get_conn()
        placeholders = ",".join("?" for _ in ids)
        conn.execute(f"""
            UPDATE resource_usage_log SET reported = 1
            WHERE id IN ({placeholders})
        """, [int(i) for i in ids])
        conn.commit()

    def sum_resource_usage(self, resource_id: str, since_ts: float) -> dict:
        """聚合指定资源池在窗口内的用量。

        Returns:
            {"tokens": 输入+输出 token 总数, "cost": 折算金额总数}
        """
        conn = self._get_conn()
        row = conn.execute("""
            SELECT COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost), 0) AS cost
            FROM resource_usage_log
            WHERE resource_id = ? AND created_at >= ?
        """, (resource_id, since_ts)).fetchone()
        return {"tokens": row["tokens"], "cost": row["cost"]}

    def query_cost_by_task(self, limit: int = 100) -> list:
        """R6: 按 task_id 聚合成本分摊 (含未归因汇总行)。

        Returns:
            [{"task_id", "project_id", "calls", "tokens", "cost",
              "last_at"}] 按 cost 降序; 无归因记录 task_id 为空串。
        """
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT task_id,
                   MAX(project_id) AS project_id,
                   COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                   COALESCE(SUM(cost), 0) AS cost,
                   MAX(created_at) AS last_at
            FROM resource_usage_log
            GROUP BY task_id
            ORDER BY cost DESC, tokens DESC
            LIMIT ?
        """, (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    def avg_tokens_per_task(self, days: int = 30) -> dict:
        """F4.4: 近 N 天有任务归因的资源用量, 每任务 token 均值 (供预算预估)。

        Returns:
            {"avg": float, "samples": int}; 无归因记录返回全零。
        """
        conn = self._get_conn()
        cutoff = time.time() - int(days) * 86400
        rows = conn.execute("""
            SELECT task_id, SUM(input_tokens + output_tokens) AS tokens
            FROM resource_usage_log
            WHERE created_at >= ? AND task_id != ''
            GROUP BY task_id
        """, (cutoff,)).fetchall()
        if not rows:
            return {"avg": 0.0, "samples": 0}
        total = sum(r["tokens"] for r in rows)
        return {"avg": round(total / len(rows), 1), "samples": len(rows)}

    # ── Graph Checkpoint CRUD ───────────────────────────────────

    def save_checkpoint(self, checkpoint_id: str, task_id: str, phase: str,
                        dag_json: str, context_json: str, history_json: str):
        """保存图执行检查点。"""
        conn = self._get_conn()
        conn.execute("""
            INSERT OR REPLACE INTO graph_checkpoints
                (checkpoint_id, task_id, phase, dag_json, context_json, history_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (checkpoint_id, task_id, phase, dag_json, context_json, history_json, time.time()))
        conn.commit()

    def get_latest_checkpoint(self, task_id: str) -> Optional[dict]:
        """获取指定任务的最新检查点。"""
        conn = self._get_conn()
        row = conn.execute("""
            SELECT * FROM graph_checkpoints
            WHERE task_id = ?
            ORDER BY created_at DESC LIMIT 1
        """, (task_id,)).fetchone()
        if not row:
            return None
        return dict(row)

    def list_checkpoints(self, task_id: str) -> list:
        """列出指定任务的所有检查点 (按时间倒序)。"""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT checkpoint_id, task_id, phase, created_at
            FROM graph_checkpoints
            WHERE task_id = ?
            ORDER BY created_at DESC
        """, (task_id,)).fetchall()
        return [dict(r) for r in rows]

    def delete_checkpoints(self, task_id: str):
        """删除指定任务的所有检查点。"""
        conn = self._get_conn()
        conn.execute("DELETE FROM graph_checkpoints WHERE task_id = ?", (task_id,))
        conn.commit()

    # ── PM 执行态快照 CRUD (iter-53 断点恢复) ────────────────────

    def save_pm_snapshot(self, pm_id: str, task_id: str, phase: str,
                         state_json: str) -> None:
        """iter-53: 保存 PM 执行态快照 (UPSERT, 一个 PM 只保留最新快照)。"""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO pm_snapshots (pm_id, task_id, phase, state_json, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(pm_id) DO UPDATE SET
                task_id = excluded.task_id,
                phase = excluded.phase,
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
        """, (pm_id, task_id, phase, state_json, time.time()))
        conn.commit()

    def get_pm_snapshot(self, pm_id: str) -> Optional[dict]:
        """iter-53: 获取指定 PM 的最新执行态快照。"""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM pm_snapshots WHERE pm_id = ?", (pm_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_pm_snapshot_by_task(self, task_id: str) -> Optional[dict]:
        """iter-53: 按任务 ID 查找最新快照 (resume 端点入口)。"""
        conn = self._get_conn()
        row = conn.execute("""
            SELECT * FROM pm_snapshots WHERE task_id = ?
            ORDER BY updated_at DESC LIMIT 1
        """, (task_id,)).fetchone()
        return dict(row) if row else None

    def delete_pm_snapshot(self, pm_id: str) -> None:
        """iter-53: 删除指定 PM 的执行态快照 (任务终结时清理)。"""
        conn = self._get_conn()
        conn.execute("DELETE FROM pm_snapshots WHERE pm_id = ?", (pm_id,))
        conn.commit()

    # ── P0/P1: LLM 调用审计 ─────────────────────────────────────

    def insert_llm_call(self, call_type: str, model: str,
                        input_tokens: int, output_tokens: int,
                        ttft_ms: float, total_ms: float,
                        status: str = "ok", task_id: str = "",
                        error: str = "") -> None:
        """写入一条 LLM 调用审计记录 (runtime_trace 双写入口)。"""
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO llm_call_log
                (call_type, model, input_tokens, output_tokens,
                 ttft_ms, total_ms, status, task_id, error, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (call_type, model, int(input_tokens), int(output_tokens),
              float(ttft_ms), float(total_ms), status, task_id,
              error[:500], time.time()))
        conn.commit()

    def query_llm_metrics(self, hours: float = 1.0) -> dict:
        """聚合最近 N 小时的 LLM 调用指标 (供 /api/runtime/metrics 使用)。

        Returns:
            {
                "window_hours": 1.0,
                "total_calls": 47,
                "avg_latency_ms": 3200,
                "p99_latency_ms": 12000,
                "avg_ttft_ms": 1200,
                "total_input_tokens": 85000,
                "total_output_tokens": 12000,
                "by_model": {"qwen3.8-max": {"calls": 30, "tokens": 50000, "avg_ms": 2500}},
                "by_status": {"ok": 45, "timeout": 2},
                "recent_errors": [{"model": "...", "error": "...", "count": 3}],
            }
        """
        conn = self._get_conn()
        cutoff = time.time() - hours * 3600

        # 总量 + 平均延迟
        row = conn.execute("""
            SELECT COUNT(*) AS total_calls,
                   COALESCE(AVG(total_ms), 0) AS avg_latency_ms,
                   COALESCE(AVG(ttft_ms), 0) AS avg_ttft_ms,
                   COALESCE(SUM(input_tokens), 0) AS total_input_tokens,
                   COALESCE(SUM(output_tokens), 0) AS total_output_tokens
            FROM llm_call_log WHERE created_at >= ?
        """, (cutoff,)).fetchone()
        total_calls = row["total_calls"]
        avg_latency = row["avg_latency_ms"]
        avg_ttft = row["avg_ttft_ms"]
        total_in = row["total_input_tokens"]
        total_out = row["total_output_tokens"]

        # P99 延迟
        p99_row = conn.execute("""
            SELECT total_ms FROM llm_call_log
            WHERE created_at >= ?
            ORDER BY total_ms ASC
        """, (cutoff,)).fetchall()
        if p99_row:
            idx = min(int(len(p99_row) * 0.99), len(p99_row) - 1)
            p99_latency = p99_row[idx]["total_ms"]
        else:
            p99_latency = 0

        # 按模型聚合
        model_rows = conn.execute("""
            SELECT model, COUNT(*) AS calls,
                   COALESCE(SUM(input_tokens + output_tokens), 0) AS tokens,
                   COALESCE(AVG(total_ms), 0) AS avg_ms
            FROM llm_call_log WHERE created_at >= ?
            GROUP BY model ORDER BY calls DESC
        """, (cutoff,)).fetchall()
        by_model = {
            r["model"]: {"calls": r["calls"], "tokens": r["tokens"],
                         "avg_ms": round(r["avg_ms"], 1)}
            for r in model_rows
        }

        # 按状态聚合
        status_rows = conn.execute("""
            SELECT status, COUNT(*) AS cnt
            FROM llm_call_log WHERE created_at >= ?
            GROUP BY status
        """, (cutoff,)).fetchall()
        by_status = {r["status"]: r["cnt"] for r in status_rows}

        # 最近错误 Top5
        err_rows = conn.execute("""
            SELECT model, error, COUNT(*) AS cnt
            FROM llm_call_log
            WHERE created_at >= ? AND status != 'ok' AND error != ''
            GROUP BY model, error ORDER BY cnt DESC LIMIT 5
        """, (cutoff,)).fetchall()
        recent_errors = [{"model": r["model"], "error": r["error"][:200],
                          "count": r["cnt"]} for r in err_rows]

        return {
            "window_hours": hours,
            "total_calls": total_calls,
            "avg_latency_ms": round(avg_latency, 1),
            "p99_latency_ms": round(p99_latency, 1),
            "avg_ttft_ms": round(avg_ttft, 1),
            "total_input_tokens": int(total_in),
            "total_output_tokens": int(total_out),
            "by_model": by_model,
            "by_status": by_status,
            "recent_errors": recent_errors,
        }

    def query_llm_recent(self, limit: int = 50) -> list[dict]:
        """查询最近 N 条 LLM 调用明细 (供调试/排查)。"""
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT id, call_type, model, input_tokens, output_tokens,
                   ttft_ms, total_ms, status, task_id, error, created_at
            FROM llm_call_log
            ORDER BY id DESC LIMIT ?
        """, (int(limit),)).fetchall()
        return [dict(r) for r in rows]

    # ── iter-54: 日志容量修剪 (补强#2) ──────────────────────────

    def prune_logs(self, retention_days: float = 30.0) -> dict:
        """iter-54: 按保留期修剪日志表, 防止 DB 无限增长 (补强#2)。

        每表删除超过保留期的历史行; resource_usage_log 仅删已上报
        (reported=1) 的行, 未上报的保留给 R3 上报线程补报。
        返回各表删除行数统计。
        """
        cutoff = time.time() - float(retention_days) * 86400
        conn = self._get_conn()
        stats: dict = {}

        # 每次 LLM 调用一行, 增长最快; 审计查询最大窗口 7 天 (metrics 端点夹取)
        stats["llm_call_log"] = conn.execute(
            "DELETE FROM llm_call_log WHERE created_at < ?", (cutoff,)
        ).rowcount
        # 聊天记录 (role/content/timestamp)
        stats["chat_history"] = conn.execute(
            "DELETE FROM chat_history WHERE timestamp < ?", (cutoff,)
        ).rowcount
        # 用量日志: 只删已上报行, 未上报 (reported=0) 保留等离线补报
        stats["resource_usage_log"] = conn.execute(
            "DELETE FROM resource_usage_log WHERE created_at < ? AND reported = 1",
            (cutoff,),
        ).rowcount
        # PM 进度报告 (查询仅取最近 50 条)
        stats["progress_reports"] = conn.execute(
            "DELETE FROM progress_reports WHERE timestamp < ?", (cutoff,)
        ).rowcount
        # 心跳历史固定 24h 保留 (与 cleanup_old_heartbeats 同一语义)
        hb_cutoff = time.time() - 24 * 3600
        stats["heartbeat_log"] = conn.execute(
            "DELETE FROM heartbeat_log WHERE timestamp < ?", (hb_cutoff,)
        ).rowcount
        conn.commit()
        return stats

    def vacuum(self) -> None:
        """iter-54: VACUUM 回收删除后未释放的磁盘空间 (修剪周期内调用)。"""
        conn = self._get_conn()
        conn.commit()  # 提交本线程挂起事务, 避免 VACUUM 死锁
        conn.execute("VACUUM")

    # ── iter-47: 错误记录持久化 (F1.4) ──────────────────────────

    def save_error_record(self, record: dict) -> None:
        """iter-47: 持久化一条错误记录到 error_log (error_tracker 回调调用)。

        容量上限 2000 行: 超出删除最旧行, 防止 DB 无限增长。
        """
        try:
            context_json = json.dumps(record.get("context") or {},
                                      ensure_ascii=False)
        except Exception:
            context_json = "{}"
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO error_log
                (timestamp, module, error_type, message, context_json, traceback)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (float(record.get("timestamp") or time.time()),
              str(record.get("module", "")),
              str(record.get("error_type", "")),
              str(record.get("message", ""))[:500],
              context_json,
              str(record.get("traceback", ""))[:2000]))
        # 容量修剪: 只保留最近 2000 行 (与 INSERT 同事务提交)
        conn.execute("""
            DELETE FROM error_log WHERE id NOT IN (
                SELECT id FROM error_log ORDER BY id DESC LIMIT 2000
            )
        """)
        conn.commit()

    def query_error_history(self, limit: int = 100, module: str = "") -> list[dict]:
        """iter-47: 查询持久化错误记录 (按写入序倒序, 供 /api/errors/history)。"""
        limit = max(1, min(int(limit), 500))
        conn = self._get_conn()
        if module:
            rows = conn.execute("""
                SELECT id, timestamp, module, error_type, message, context_json
                FROM error_log WHERE module = ?
                ORDER BY id DESC LIMIT ?
            """, (module, limit)).fetchall()
        else:
            rows = conn.execute("""
                SELECT id, timestamp, module, error_type, message, context_json
                FROM error_log ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            raw = d.pop("context_json", "{}")
            try:
                d["context"] = json.loads(raw or "{}")
            except Exception:
                d["context"] = {}
            result.append(d)
        return result

    # ── iter-49: 自愈动作执行日志 (F4.2 修复环节) ──────────────

    def save_heal_record(self, record: dict) -> None:
        """iter-49: 持久化一条自愈动作执行记录到 heal_log。

        result 取值: ok / failed / manual_required; detail 为执行摘要。
        容量上限 500 行: 超出删除最旧行。
        """
        conn = self._get_conn()
        conn.execute("""
            INSERT INTO heal_log
                (timestamp, category, action, result, detail)
            VALUES (?, ?, ?, ?, ?)
        """, (float(record.get("timestamp") or time.time()),
              str(record.get("category", "")),
              str(record.get("action", "")),
              str(record.get("result", "")),
              str(record.get("detail", ""))[:500]))
        # 容量修剪: 只保留最近 500 行 (与 INSERT 同事务提交)
        conn.execute("""
            DELETE FROM heal_log WHERE id NOT IN (
                SELECT id FROM heal_log ORDER BY id DESC LIMIT 500
            )
        """)
        conn.commit()

    def query_heal_history(self, limit: int = 50) -> list[dict]:
        """iter-49: 查询自愈执行历史 (按写入序倒序, 供 /api/errors/heal/history)。"""
        limit = max(1, min(int(limit), 200))
        conn = self._get_conn()
        rows = conn.execute("""
            SELECT id, timestamp, category, action, result, detail
            FROM heal_log ORDER BY id DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]
