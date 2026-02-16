from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import threading
import uuid

from app.core.config import settings
from app.core.errors import AppError
from app.services.mcp_vector_service import IndexProgress, mcp_vector_service


@dataclass(frozen=True)
class McpIndexJob:
    job_id: str
    user_id: int
    workspace: str
    mode: str
    status: str
    percent: int
    total_files: int
    total_chunks: int
    processed_chunks: int
    failed_chunks: int
    elapsed_ms: int
    error_message: str | None
    created_at: str
    updated_at: str


class McpIndexJobService:
    """索引任务管理服务。"""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_index_jobs (
                    job_id TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    workspace TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    percent INTEGER NOT NULL,
                    total_files INTEGER NOT NULL,
                    total_chunks INTEGER NOT NULL,
                    processed_chunks INTEGER NOT NULL,
                    failed_chunks INTEGER NOT NULL,
                    elapsed_ms INTEGER NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            conn.commit()

    def create_job(self, *, user_id: int, workspace: str, mode: str) -> McpIndexJob:
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"full", "incremental"}:
            raise AppError(
                code="MCP_INDEX_JOB_INVALID_MODE",
                message="mode must be full or incremental",
                details={"mode": mode},
                status_code=400,
            )

        job_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO mcp_index_jobs (
                    job_id, user_id, workspace, mode,
                    status, percent, total_files, total_chunks,
                    processed_chunks, failed_chunks, elapsed_ms,
                    error_message, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    user_id,
                    workspace,
                    normalized_mode,
                    "running",
                    0,
                    0,
                    0,
                    0,
                    0,
                    0,
                    None,
                    now,
                    now,
                ),
            )
            conn.commit()

        thread = threading.Thread(
            target=self._run_job,
            kwargs={
                "job_id": job_id,
                "workspace": workspace,
                "mode": normalized_mode,
            },
            daemon=True,
        )
        thread.start()
        return self.get_job(job_id=job_id, requester_id=user_id, requester_is_superadmin=True)

    def get_job(
        self,
        *,
        job_id: str,
        requester_id: int,
        requester_is_superadmin: bool,
    ) -> McpIndexJob:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT job_id, user_id, workspace, mode, status,
                       percent, total_files, total_chunks, processed_chunks,
                       failed_chunks, elapsed_ms, error_message,
                       created_at, updated_at
                FROM mcp_index_jobs
                WHERE job_id = ?
                LIMIT 1
                """,
                (job_id,),
            ).fetchone()
            if row is None:
                raise AppError(
                    code="MCP_INDEX_JOB_NOT_FOUND",
                    message="index job not found",
                    details={"job_id": job_id},
                    status_code=404,
                )

            user_id = int(row["user_id"])
            if not requester_is_superadmin and user_id != requester_id:
                raise AppError(
                    code="MCP_INDEX_JOB_FORBIDDEN",
                    message="no permission to view this job",
                    status_code=403,
                )
            return self._to_job(row)

    def cleanup_old_audit_logs(self, *, keep_days: int = 30) -> int:
        cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
        cutoff_text = cutoff.isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            cursor = conn.execute(
                """
                DELETE FROM mcp_audit_logs
                WHERE created_at < ?
                """,
                (cutoff_text,),
            )
            conn.commit()
            return int(cursor.rowcount or 0)

    def _run_job(self, *, job_id: str, workspace: str, mode: str) -> None:
        lock = self._get_workspace_lock(workspace)
        if not lock.acquire(blocking=False):
            self._finalize_failed(job_id, "workspace has another running index job")
            return

        try:
            def _on_progress(progress: IndexProgress) -> None:
                percent = 100
                if progress.total_chunks > 0:
                    percent = int((progress.processed_chunks * 100) / progress.total_chunks)
                self._update_progress(
                    job_id,
                    total_files=progress.total_files,
                    total_chunks=progress.total_chunks,
                    processed_chunks=progress.processed_chunks,
                    failed_chunks=progress.failed_chunks,
                    percent=max(0, min(percent, 100)),
                    elapsed_ms=progress.elapsed_ms,
                )

            result = mcp_vector_service.build_index(
                workspace=workspace,
                mode=mode,
                job_id=job_id,
                progress_callback=_on_progress,
            )

            self._update_progress(
                job_id,
                total_files=result.total_files,
                total_chunks=result.total_chunks,
                processed_chunks=result.processed_chunks,
                failed_chunks=result.failed_chunks,
                percent=100,
                elapsed_ms=result.elapsed_ms,
            )
            if result.failed_chunks > 0:
                self._finalize_failed(
                    job_id,
                    (
                        "index finished with failures: "
                        f"failed_chunks={result.failed_chunks}"
                    ),
                )
            else:
                self._finalize_done(job_id=job_id, elapsed_ms=result.elapsed_ms)
        except Exception as exc:  # pragma: no cover
            self._finalize_failed(job_id, str(exc))
        finally:
            lock.release()

    def _update_progress(
        self,
        job_id: str,
        *,
        total_files: int,
        total_chunks: int,
        processed_chunks: int,
        failed_chunks: int,
        percent: int,
        elapsed_ms: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                UPDATE mcp_index_jobs
                SET total_files = ?, total_chunks = ?, processed_chunks = ?,
                    failed_chunks = ?, percent = ?, elapsed_ms = ?,
                    updated_at = ?
                WHERE job_id = ?
                """,
                (
                    total_files,
                    total_chunks,
                    processed_chunks,
                    failed_chunks,
                    max(0, min(percent, 100)),
                    elapsed_ms,
                    now,
                    job_id,
                ),
            )
            conn.commit()

    def _finalize_done(self, *, job_id: str, elapsed_ms: int) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                UPDATE mcp_index_jobs
                SET status = 'done', percent = 100,
                    elapsed_ms = ?, updated_at = ?, error_message = NULL
                WHERE job_id = ?
                """,
                (elapsed_ms, now, job_id),
            )
            conn.commit()

    def _finalize_failed(self, job_id: str, reason: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                UPDATE mcp_index_jobs
                SET status = 'failed', error_message = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (reason[:1000], now, job_id),
            )
            conn.commit()

    def _to_job(self, row: sqlite3.Row) -> McpIndexJob:
        return McpIndexJob(
            job_id=str(row["job_id"]),
            user_id=int(row["user_id"]),
            workspace=str(row["workspace"]),
            mode=str(row["mode"]),
            status=str(row["status"]),
            percent=int(row["percent"]),
            total_files=int(row["total_files"]),
            total_chunks=int(row["total_chunks"]),
            processed_chunks=int(row["processed_chunks"]),
            failed_chunks=int(row["failed_chunks"]),
            elapsed_ms=int(row["elapsed_ms"]),
            error_message=str(row["error_message"]) if row["error_message"] else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def _get_workspace_lock(self, workspace: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(workspace)
            if lock is None:
                lock = threading.Lock()
                self._locks[workspace] = lock
            return lock


mcp_index_job_service = McpIndexJobService(str(settings.sqlite_db_path))
