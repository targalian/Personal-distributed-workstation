"""Unmodified SQLite initialization scripts; migration version remains in database.py."""

SCHEMA_1 = """
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
        """

SCHEMA_0 = """
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
                charter           TEXT NOT NULL DEFAULT '{}',
                roadmap           TEXT NOT NULL DEFAULT '[]',
                decisions         TEXT NOT NULL DEFAULT '[]',
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
        """

