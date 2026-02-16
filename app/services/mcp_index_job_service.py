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
from app.services.mcp_vector_service import (
    IndexFailureRecord,
    IndexProgress,
    mcp_vector_service,
)


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
        return self._create_job_internal(
            user_id=user_id,
            workspace=workspace,
            mode=mode,
        )

    def list_jobs(
        self,
        *,
        requester_id: int,
        requester_is_superadmin: bool,
        workspace: str | None,
        status: str | None,
        page: int,
        size: int,
    ) -> tuple[list[McpIndexJob], int]:
        page = max(1, page)
        size = max(1, min(size, 200))
        offset = (page - 1) * size

        clauses: list[str] = []
        params: list[object] = []

        if not requester_is_superadmin:
            clauses.append("user_id = ?")
            params.append(requester_id)
        if workspace:
            clauses.append("workspace = ?")
            params.append(workspace)
        if status:
            clauses.append("status = ?")
            params.append(status.strip().lower())

        where_clause = ""
        if clauses:
            where_clause = "WHERE " + " AND ".join(clauses)

        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            total_row = conn.execute(
                f"SELECT COUNT(1) AS cnt FROM mcp_index_jobs {where_clause}",
                tuple(params),
            ).fetchone()
            total = int(total_row["cnt"] if total_row else 0)

            rows = conn.execute(
                f"""
                SELECT job_id, user_id, workspace, mode, status,
                       percent, total_files, total_chunks, processed_chunks,
                       failed_chunks, elapsed_ms, error_message,
                       created_at, updated_at
                FROM mcp_index_jobs
                {where_clause}
                ORDER BY updated_at DESC
                LIMIT ? OFFSET ?
                """,
                (*params, size, offset),
            ).fetchall()
            items = [self._to_job(row) for row in rows]
            return items, total

    def retry_job_failures(
        self,
        *,
        source_job_id: str,
        user_id: int,
    ) -> McpIndexJob:
        source = self.get_job(
            job_id=source_job_id,
            requester_id=user_id,
            requester_is_superadmin=True,
        )
        if source.status != "failed":
            raise AppError(
                code="MCP_INDEX_JOB_NOT_FAILED",
                message="only failed job can be retried",
                details={"job_id": source_job_id, "status": source.status},
                status_code=400,
            )
        failures = mcp_vector_service.list_failures(
            workspace=source.workspace,
            job_id=source_job_id,
        )
        if not failures:
            raise AppError(
                code="MCP_INDEX_JOB_NO_FAILURES",
                message="no failure records found for this job",
                details={"job_id": source_job_id},
                status_code=400,
            )
        paths = [item.path for item in failures]
        return self._create_job_internal(
            user_id=user_id,
            workspace=source.workspace,
            mode="retry_failed",
            source_job_id=source_job_id,
            retry_paths=paths,
        )

    def list_job_failures(
        self,
        *,
        source_job_id: str,
        requester_id: int,
        requester_is_superadmin: bool,
        page: int,
        size: int,
    ) -> tuple[list[IndexFailureRecord], int]:
        source = self.get_job(
            job_id=source_job_id,
            requester_id=requester_id,
            requester_is_superadmin=requester_is_superadmin,
        )
        failures = mcp_vector_service.list_failures(
            workspace=source.workspace,
            job_id=source_job_id,
        )
        page = max(1, page)
        size = max(1, min(size, 500))
        offset = (page - 1) * size
        paged = failures[offset : offset + size]
        return paged, len(failures)

    def retry_job_failure_paths(
        self,
        *,
        source_job_id: str,
        user_id: int,
        paths: list[str],
    ) -> McpIndexJob:
        source = self.get_job(
            job_id=source_job_id,
            requester_id=user_id,
            requester_is_superadmin=True,
        )
        failures = mcp_vector_service.list_failures(
            workspace=source.workspace,
            job_id=source_job_id,
        )
        if not failures:
            raise AppError(
                code="MCP_INDEX_JOB_NO_FAILURES",
                message="no failure records found for this job",
                details={"job_id": source_job_id},
                status_code=400,
            )

        # 只允许重试当前失败清单中的文件，避免越权或误传路径触发无关扫描。
        valid_paths = {item.path for item in failures}
        normalized_paths = sorted({path.strip() for path in paths if path.strip()})
        if not normalized_paths:
            raise AppError(
                code="MCP_INDEX_RETRY_PATHS_REQUIRED",
                message="paths is required",
                status_code=400,
            )
        invalid_paths = [path for path in normalized_paths if path not in valid_paths]
        if invalid_paths:
            raise AppError(
                code="MCP_INDEX_RETRY_PATHS_INVALID",
                message="some paths are not in failure records",
                details={"invalid_paths": invalid_paths[:20]},
                status_code=400,
            )

        return self._create_job_internal(
            user_id=user_id,
            workspace=source.workspace,
            mode="retry_failed",
            source_job_id=source_job_id,
            retry_paths=normalized_paths,
        )

    def retry_all_failed_jobs(
        self,
        *,
        user_id: int,
        workspace: str | None = None,
    ) -> list[McpIndexJob]:
        jobs, _ = self.list_jobs(
            requester_id=user_id,
            requester_is_superadmin=True,
            workspace=workspace,
            status="failed",
            page=1,
            size=200,
        )
        created: list[McpIndexJob] = []
        for job in jobs:
            try:
                created.append(
                    self.retry_job_failures(
                        source_job_id=job.job_id,
                        user_id=user_id,
                    )
                )
            except AppError:
                continue
        return created

    def _create_job_internal(
        self,
        *,
        user_id: int,
        workspace: str,
        mode: str,
        source_job_id: str | None = None,
        retry_paths: list[str] | None = None,
    ) -> McpIndexJob:
        normalized_mode = mode.strip().lower()
        if normalized_mode not in {"full", "incremental", "retry_failed"}:
            raise AppError(
                code="MCP_INDEX_JOB_INVALID_MODE",
                message="mode must be full/incremental/retry_failed",
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
                "source_job_id": source_job_id,
                "retry_paths": retry_paths or [],
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

    def _run_job(
        self,
        *,
        job_id: str,
        workspace: str,
        mode: str,
        source_job_id: str | None = None,
        retry_paths: list[str],
    ) -> None:
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

            if mode == "retry_failed":
                if not source_job_id:
                    raise AppError(
                        code="MCP_INDEX_RETRY_SOURCE_REQUIRED",
                        message="source_job_id is required for retry_failed",
                        status_code=400,
                    )
                result = mcp_vector_service.retry_failed_paths(
                    workspace=workspace,
                    source_job_id=source_job_id,
                    retry_job_id=job_id,
                    paths=retry_paths,
                    progress_callback=_on_progress,
                )
            else:
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
