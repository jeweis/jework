from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import settings


@dataclass(frozen=True)
class UserWorkspacePreference:
    user_id: int
    selected_tags: list[str]
    updated_at: str | None


class UserWorkspacePreferenceService:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_workspace_preferences (
                    user_id INTEGER PRIMARY KEY,
                    selected_tags_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def get_preference(self, user_id: int) -> UserWorkspacePreference:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT user_id, selected_tags_json, updated_at
                FROM user_workspace_preferences
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
        if row is None:
            return UserWorkspacePreference(
                user_id=user_id,
                selected_tags=[],
                updated_at=None,
            )
        return UserWorkspacePreference(
            user_id=int(row["user_id"]),
            selected_tags=self._normalize_tags_json(row["selected_tags_json"]),
            updated_at=row["updated_at"],
        )

    def update_selected_tags(
        self,
        *,
        user_id: int,
        selected_tags: list[str],
    ) -> UserWorkspacePreference:
        normalized_tags = self._normalize_tags(selected_tags)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO user_workspace_preferences (
                    user_id, selected_tags_json, updated_at
                )
                VALUES (?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    selected_tags_json = excluded.selected_tags_json,
                    updated_at = excluded.updated_at
                """,
                (user_id, json.dumps(normalized_tags, ensure_ascii=False), now),
            )
            conn.commit()
        return UserWorkspacePreference(
            user_id=user_id,
            selected_tags=normalized_tags,
            updated_at=now,
        )

    def _normalize_tags_json(self, raw: str | None) -> list[str]:
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if not isinstance(payload, list):
            return []
        return self._normalize_tags([str(item) for item in payload])

    def _normalize_tags(self, values: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for value in values:
            tag = value.strip()
            if not tag:
                continue
            if len(tag) > 32:
                tag = tag[:32]
            if tag in seen:
                continue
            seen.add(tag)
            normalized.append(tag)
        normalized.sort()
        return normalized


user_workspace_preference_service = UserWorkspacePreferenceService(
    str(settings.sqlite_db_path)
)
