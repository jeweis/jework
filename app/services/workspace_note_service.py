from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3

from app.core.config import settings


@dataclass(frozen=True)
class WorkspaceNoteItem:
    workspace: str
    note: str | None
    updated_at: str | None


class WorkspaceNoteService:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._ensure_table()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_table(self) -> None:
        # 备注独立存储，避免污染工作空间主目录与凭据表。
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_notes (
                    workspace TEXT PRIMARY KEY,
                    note TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def list_notes(self) -> dict[str, WorkspaceNoteItem]:
        # 列表页一次性加载，使用 map 便于按 workspace 名称快速关联。
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT workspace, note, updated_at
                FROM workspace_notes
                """
            ).fetchall()
        result: dict[str, WorkspaceNoteItem] = {}
        for row in rows:
            item = WorkspaceNoteItem(
                workspace=str(row["workspace"]),
                note=str(row["note"]) if row["note"] is not None else None,
                updated_at=str(row["updated_at"]),
            )
            result[item.workspace] = item
        return result

    def get_note(self, workspace: str) -> WorkspaceNoteItem | None:
        with self._conn() as conn:
            row = conn.execute(
                """
                SELECT workspace, note, updated_at
                FROM workspace_notes
                WHERE workspace = ?
                """,
                (workspace,),
            ).fetchone()
        if row is None:
            return None
        return WorkspaceNoteItem(
            workspace=str(row["workspace"]),
            note=str(row["note"]) if row["note"] is not None else None,
            updated_at=str(row["updated_at"]),
        )

    def upsert_note(self, workspace: str, note: str | None, updated_at: str) -> WorkspaceNoteItem:
        # 约定：空字符串等价于清空备注，前端可直接传空值。
        normalized = note.strip() if note is not None else None
        if normalized == "":
            normalized = None

        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO workspace_notes (workspace, note, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(workspace) DO UPDATE SET
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (workspace, normalized, updated_at),
            )

        return WorkspaceNoteItem(
            workspace=workspace,
            note=normalized,
            updated_at=updated_at,
        )

    def delete_note(self, workspace: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                DELETE FROM workspace_notes
                WHERE workspace = ?
                """,
                (workspace,),
            )


workspace_note_service = WorkspaceNoteService(str(settings.sqlite_db_path))
