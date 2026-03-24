from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3

from app.core.config import settings


@dataclass(frozen=True)
class WorkspaceTagsItem:
    workspace: str
    tags: list[str]
    updated_at: str | None


class WorkspaceTagService:
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
        # 标签独立存储，方便后续做筛选和统计，不污染主工作空间表结构。
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_tags (
                    workspace TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (workspace, tag)
                )
                """
            )

    def list_tags(self) -> dict[str, WorkspaceTagsItem]:
        with self._conn() as conn:
            rows = conn.execute(
                """
                SELECT workspace, tag, updated_at
                FROM workspace_tags
                ORDER BY workspace ASC, tag ASC
                """
            ).fetchall()
        result: dict[str, WorkspaceTagsItem] = {}
        for row in rows:
            workspace = str(row["workspace"])
            tag = str(row["tag"])
            current = result.get(workspace)
            if current is None:
                result[workspace] = WorkspaceTagsItem(
                    workspace=workspace,
                    tags=[tag],
                    updated_at=str(row["updated_at"]),
                )
                continue
            result[workspace] = WorkspaceTagsItem(
                workspace=workspace,
                tags=[*current.tags, tag],
                updated_at=str(row["updated_at"]),
            )
        return result

    def get_tags(self, workspace: str) -> WorkspaceTagsItem | None:
        return self.list_tags().get(workspace)

    def replace_tags(
        self,
        *,
        workspace: str,
        tags: list[str],
        updated_at: str,
    ) -> WorkspaceTagsItem:
        normalized = self._normalize_tags(tags)
        with self._conn() as conn:
            conn.execute(
                """
                DELETE FROM workspace_tags
                WHERE workspace = ?
                """,
                (workspace,),
            )
            if normalized:
                conn.executemany(
                    """
                    INSERT INTO workspace_tags (workspace, tag, updated_at)
                    VALUES (?, ?, ?)
                    """,
                    [(workspace, tag, updated_at) for tag in normalized],
                )
        return WorkspaceTagsItem(
            workspace=workspace,
            tags=normalized,
            updated_at=updated_at,
        )

    def delete_tags(self, workspace: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                DELETE FROM workspace_tags
                WHERE workspace = ?
                """,
                (workspace,),
            )

    def _normalize_tags(self, tags: list[str]) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for item in tags:
            tag = item.strip()
            if not tag:
                continue
            if len(tag) > 32:
                tag = tag[:32]
            if tag in seen:
                continue
            seen.add(tag)
            normalized.append(tag)
        return sorted(normalized)


workspace_tag_service = WorkspaceTagService(str(settings.sqlite_db_path))
