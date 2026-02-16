from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.config import settings


@dataclass(frozen=True)
class McpAuditRecord:
    user_id: int
    tool_name: str
    workspace: str | None
    path_or_query: str | None
    elapsed_ms: int
    status: str


class McpAuditService:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    tool_name TEXT NOT NULL,
                    workspace TEXT,
                    path_or_query TEXT,
                    elapsed_ms INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def append(self, record: McpAuditRecord) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO mcp_audit_logs (
                    user_id, tool_name, workspace, path_or_query,
                    elapsed_ms, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.user_id,
                    record.tool_name,
                    record.workspace,
                    record.path_or_query,
                    record.elapsed_ms,
                    record.status,
                    now,
                ),
            )
            conn.commit()

    def cleanup_old_logs(self, keep_days: int = 30) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        with closing(sqlite3.connect(self._db_path)) as conn:
            cursor = conn.execute(
                """
                DELETE FROM mcp_audit_logs
                WHERE created_at < ?
                """,
                (cutoff.isoformat(),),
            )
            conn.commit()
            return int(cursor.rowcount or 0)


mcp_audit_service = McpAuditService(str(settings.sqlite_db_path))
