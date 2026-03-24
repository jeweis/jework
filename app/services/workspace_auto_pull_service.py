from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.config import settings


@dataclass(frozen=True)
class WorkspaceAutoPullSettings:
    enabled: bool
    interval_minutes: int
    last_run_at: str | None
    next_run_at: str | None
    updated_by: int | None
    updated_at: str | None


class WorkspaceAutoPullService:
    _ROW_ID = 1
    _DEFAULT_INTERVAL_MINUTES = 60
    _ALLOWED_INTERVALS = {15, 30, 60, 180, 360}

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_auto_pull_settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    interval_minutes INTEGER NOT NULL DEFAULT 60,
                    last_run_at TEXT,
                    next_run_at TEXT,
                    updated_by INTEGER,
                    updated_at TEXT NOT NULL
                )
                """
            )
            existing = conn.execute(
                """
                SELECT 1 FROM workspace_auto_pull_settings WHERE id = ?
                """,
                (self._ROW_ID,),
            ).fetchone()
            if existing is None:
                now = datetime.now(timezone.utc).isoformat()
                conn.execute(
                    """
                    INSERT INTO workspace_auto_pull_settings (
                        id, enabled, interval_minutes, last_run_at,
                        next_run_at, updated_by, updated_at
                    )
                    VALUES (?, 0, ?, NULL, NULL, NULL, ?)
                    """,
                    (self._ROW_ID, self._DEFAULT_INTERVAL_MINUTES, now),
                )
            conn.commit()

    def get_settings(self) -> WorkspaceAutoPullSettings:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT enabled, interval_minutes, last_run_at, next_run_at,
                       updated_by, updated_at
                FROM workspace_auto_pull_settings
                WHERE id = ?
                """,
                (self._ROW_ID,),
            ).fetchone()
        if row is None:
            self.init_db()
            return self.get_settings()
        return WorkspaceAutoPullSettings(
            enabled=bool(row["enabled"]),
            interval_minutes=int(row["interval_minutes"]),
            last_run_at=row["last_run_at"],
            next_run_at=row["next_run_at"],
            updated_by=int(row["updated_by"]) if row["updated_by"] is not None else None,
            updated_at=row["updated_at"],
        )

    def update_settings(
        self,
        *,
        enabled: bool,
        interval_minutes: int,
        updated_by: int,
    ) -> WorkspaceAutoPullSettings:
        normalized_interval = self._normalize_interval(interval_minutes)
        now = datetime.now(timezone.utc)
        next_run_at = (
            (now + timedelta(minutes=normalized_interval)).isoformat()
            if enabled
            else None
        )
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                UPDATE workspace_auto_pull_settings
                SET enabled = ?,
                    interval_minutes = ?,
                    next_run_at = ?,
                    updated_by = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    1 if enabled else 0,
                    normalized_interval,
                    next_run_at,
                    updated_by,
                    now.isoformat(),
                    self._ROW_ID,
                ),
            )
            conn.commit()
        return self.get_settings()

    def should_run_now(self) -> bool:
        settings = self.get_settings()
        if not settings.enabled:
            return False
        if not settings.next_run_at:
            return True
        next_run = datetime.fromisoformat(settings.next_run_at)
        return datetime.now(timezone.utc) >= next_run

    def mark_run_finished(self) -> WorkspaceAutoPullSettings:
        settings = self.get_settings()
        now = datetime.now(timezone.utc)
        next_run_at = (
            now + timedelta(minutes=self._normalize_interval(settings.interval_minutes))
        ).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                UPDATE workspace_auto_pull_settings
                SET last_run_at = ?,
                    next_run_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (now.isoformat(), next_run_at, now.isoformat(), self._ROW_ID),
            )
            conn.commit()
        return self.get_settings()

    def _normalize_interval(self, interval_minutes: int) -> int:
        if interval_minutes in self._ALLOWED_INTERVALS:
            return interval_minutes
        return self._DEFAULT_INTERVAL_MINUTES


workspace_auto_pull_service = WorkspaceAutoPullService(str(settings.sqlite_db_path))
