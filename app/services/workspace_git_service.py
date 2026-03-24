from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import settings


@dataclass(frozen=True)
class WorkspaceGitSyncMeta:
    workspace: str
    last_pull_at: str | None
    last_pull_status: str | None
    last_pull_message: str | None
    last_pull_trigger_mode: str | None
    last_pull_error_detail: str | None


class WorkspaceGitService:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_git_sync (
                    workspace TEXT PRIMARY KEY,
                    last_pull_at TEXT,
                    last_pull_status TEXT,
                    last_pull_message TEXT,
                    last_pull_trigger_mode TEXT,
                    last_pull_error_detail TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_columns(conn)
            conn.commit()

    def _ensure_columns(self, conn: sqlite3.Connection) -> None:
        existing = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(workspace_git_sync)").fetchall()
        }
        if "last_pull_trigger_mode" not in existing:
            conn.execute(
                """
                ALTER TABLE workspace_git_sync
                ADD COLUMN last_pull_trigger_mode TEXT
                """
            )
        if "last_pull_error_detail" not in existing:
            conn.execute(
                """
                ALTER TABLE workspace_git_sync
                ADD COLUMN last_pull_error_detail TEXT
                """
            )

    def set_pull_result(
        self,
        workspace: str,
        status: str,
        message: str | None,
        trigger_mode: str | None = None,
        error_detail: str | None = None,
        pulled_at: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        last_pull_at = pulled_at or now
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO workspace_git_sync (
                    workspace, last_pull_at, last_pull_status, last_pull_message,
                    last_pull_trigger_mode, last_pull_error_detail, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workspace) DO UPDATE SET
                    last_pull_at=excluded.last_pull_at,
                    last_pull_status=excluded.last_pull_status,
                    last_pull_message=excluded.last_pull_message,
                    last_pull_trigger_mode=excluded.last_pull_trigger_mode,
                    last_pull_error_detail=excluded.last_pull_error_detail,
                    updated_at=excluded.updated_at
                """,
                (
                    workspace,
                    last_pull_at,
                    status,
                    message,
                    trigger_mode,
                    error_detail,
                    now,
                ),
            )
            conn.commit()

    def get_sync_meta_map(self) -> dict[str, WorkspaceGitSyncMeta]:
        result: dict[str, WorkspaceGitSyncMeta] = {}
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT workspace, last_pull_at, last_pull_status, last_pull_message,
                       last_pull_trigger_mode, last_pull_error_detail
                FROM workspace_git_sync
                """
            ).fetchall()
            for row in rows:
                item = WorkspaceGitSyncMeta(
                    workspace=str(row["workspace"]),
                    last_pull_at=row["last_pull_at"],
                    last_pull_status=row["last_pull_status"],
                    last_pull_message=row["last_pull_message"],
                    last_pull_trigger_mode=row["last_pull_trigger_mode"],
                    last_pull_error_detail=row["last_pull_error_detail"],
                )
                result[item.workspace] = item
        return result

    def delete_sync_meta(self, workspace: str) -> None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                DELETE FROM workspace_git_sync
                WHERE workspace = ?
                """,
                (workspace,),
            )
            conn.commit()


workspace_git_service = WorkspaceGitService(str(settings.sqlite_db_path))
