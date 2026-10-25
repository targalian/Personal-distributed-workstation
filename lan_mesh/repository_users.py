"""Domain SQL repository; Database remains the sole connection and schema owner."""

from __future__ import annotations
from typing import Any
from .logger import get_logger
from .repository_registry import Repository
logger = get_logger("database")

class UsersRepository(Repository):
    """UsersRepository over the Database owner; SQL and connection policy are preserved."""

    def list_users_db(self) -> list[dict]:
        """列出全部用户 (含 token 哈希与尾 4 位快照, 仅内部使用)。"""
        conn = self.database._get_conn()
        rows = conn.execute(
            "SELECT name, role, token_hash, token_tail4, created_at, "
            "updated_at FROM users ORDER BY name"
        ).fetchall()
        return [{"name": r[0], "role": r[1], "token_hash": r[2],
                 "token_tail4": r[3], "created_at": r[4], "updated_at": r[5]}
                for r in rows]


    def upsert_user_db(self, name: str, role: str, token_hash: str,
                       token_tail4: str) -> Any:
        """新增或更新用户 (存在时更新 role/token 哈希)。"""
        import time as _time
        conn = self.database._get_conn()
        now = _time.time()
        conn.execute(
            """
            INSERT INTO users (name, role, token_hash, token_tail4,
                               created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                role = excluded.role,
                token_hash = excluded.token_hash,
                token_tail4 = excluded.token_tail4,
                updated_at = excluded.updated_at
            """,
            (name, role, token_hash, token_tail4, now, now),
        )
        conn.commit()


    def update_user_role_db(self, name: str, role: str) -> Any:
        """仅更新用户角色。"""
        import time as _time
        conn = self.database._get_conn()
        conn.execute(
            "UPDATE users SET role = ?, updated_at = ? WHERE name = ?",
            (role, _time.time(), name),
        )
        conn.commit()


    def delete_user_db(self, name: str) -> Any:
        """删除用户。"""
        conn = self.database._get_conn()
        conn.execute("DELETE FROM users WHERE name = ?", (name,))
        conn.commit()

