from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib import error as urllib_error
from urllib import request as urllib_request

from app.core.config import settings
from app.core.errors import AppError
from app.services.mcp_settings_service import mcp_settings_service
from app.services.workspace_service import workspace_service

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IndexProgress:
    workspace: str
    total_files: int
    total_chunks: int
    processed_chunks: int
    failed_chunks: int
    elapsed_ms: int
    message: str


@dataclass(frozen=True)
class IndexBuildResult:
    workspace: str
    total_files: int
    total_chunks: int
    processed_chunks: int
    failed_chunks: int
    elapsed_ms: int
    head_commit: str


@dataclass(frozen=True)
class IndexFailureRecord:
    job_id: str
    workspace: str
    path: str
    reason: str
    retry_count: int
    created_at: str


class McpVectorService:
    """向量索引与检索服务。"""

    _CODE_SUFFIXES = {
        ".py",
        ".js",
        ".ts",
        ".tsx",
        ".java",
        ".go",
        ".rs",
        ".dart",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".sh",
        ".yaml",
        ".yml",
        ".json",
    }

    _TEXT_SUFFIXES = _CODE_SUFFIXES | {
        ".md",
        ".markdown",
        ".txt",
    }

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_index_state (
                    workspace TEXT PRIMARY KEY,
                    last_indexed_commit TEXT,
                    last_indexed_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_index_failures (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT,
                    workspace TEXT NOT NULL,
                    path TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def build_index(
        self,
        *,
        workspace: str,
        mode: str,
        job_id: str,
        progress_callback: Callable[[IndexProgress], None],
    ) -> IndexBuildResult:
        started = datetime.now(timezone.utc)
        root = workspace_service.get_workspace_path(workspace)
        head_commit = self._resolve_head_commit(root)
        last_indexed_commit = self._get_last_indexed_commit(workspace)

        normalized_mode = mode.strip().lower()
        full_rebuild = (
            normalized_mode == "full"
            or not last_indexed_commit
            or not self._is_git_repository(root)
        )

        if full_rebuild:
            changed_files = self._list_tracked_files(root)
            deleted_files: list[str] = []
            self._delete_workspace_vectors(workspace)
        else:
            changed_files, deleted_files = self._collect_incremental_changes(
                root,
                last_indexed_commit,
                head_commit,
            )

        total_files = len(changed_files) + len(deleted_files)
        # 先预估 chunk 总量，确保前端可获得稳定 percent。
        total_chunks = 0
        file_chunks: list[tuple[str, list[dict[str, object]]]] = []
        for path in changed_files:
            chunks = self._build_file_chunks(root, path)
            file_chunks.append((path, chunks))
            total_chunks += len(chunks)

        if total_files == 0:
            self._set_last_indexed_commit(workspace, head_commit)
            elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
            progress_callback(
                IndexProgress(
                    workspace=workspace,
                    total_files=0,
                    total_chunks=0,
                    processed_chunks=0,
                    failed_chunks=0,
                    elapsed_ms=elapsed_ms,
                    message="索引无需更新，基准 commit 已推进",
                )
            )
            return IndexBuildResult(
                workspace=workspace,
                total_files=0,
                total_chunks=0,
                processed_chunks=0,
                failed_chunks=0,
                elapsed_ms=elapsed_ms,
                head_commit=head_commit,
            )

        progress_callback(
            IndexProgress(
                workspace=workspace,
                total_files=total_files,
                total_chunks=total_chunks,
                processed_chunks=0,
                failed_chunks=0,
                elapsed_ms=0,
                message="索引任务开始",
            )
        )

        processed = 0
        failed = 0

        for path in deleted_files:
            try:
                self._delete_path_vectors(workspace=workspace, path=path)
            except Exception as exc:
                failed += 1
                self._record_failure(job_id, workspace, path, f"delete failed: {exc}")
            progress_callback(
                IndexProgress(
                    workspace=workspace,
                    total_files=total_files,
                    total_chunks=total_chunks,
                    processed_chunks=processed,
                    failed_chunks=failed,
                    elapsed_ms=self._elapsed_ms(started),
                    message=f"删除向量完成: {path}",
                )
            )

        batch_size = max(1, mcp_settings_service.get_settings().embedding_batch_size)
        for path, chunks in file_chunks:
            try:
                self._delete_path_vectors(workspace=workspace, path=path)
                if chunks:
                    for offset in range(0, len(chunks), batch_size):
                        part = chunks[offset : offset + batch_size]
                        texts = [str(item["text"]) for item in part]
                        vectors = self._embed_texts(texts)
                        self._upsert_chunks(
                            workspace=workspace,
                            commit_sha=head_commit,
                            path=path,
                            chunks=part,
                            vectors=vectors,
                        )
                        processed += len(part)
                        if processed % 50 == 0:
                            logger.info(
                                "mcp index progress workspace=%s total_files=%s total_chunks=%s processed_chunks=%s failed_chunks=%s elapsed_ms=%s",
                                workspace,
                                total_files,
                                total_chunks,
                                processed,
                                failed,
                                self._elapsed_ms(started),
                            )
                        progress_callback(
                            IndexProgress(
                                workspace=workspace,
                                total_files=total_files,
                                total_chunks=total_chunks,
                                processed_chunks=processed,
                                failed_chunks=failed,
                                elapsed_ms=self._elapsed_ms(started),
                                message=f"索引文件完成: {path}",
                            )
                        )
            except Exception as exc:
                failed += max(1, len(chunks))
                self._record_failure(job_id, workspace, path, str(exc))
                progress_callback(
                    IndexProgress(
                        workspace=workspace,
                        total_files=total_files,
                        total_chunks=total_chunks,
                        processed_chunks=processed,
                        failed_chunks=failed,
                        elapsed_ms=self._elapsed_ms(started),
                        message=f"索引文件失败: {path}",
                    )
                )

        # 两阶段推进：仅在向量写入流程完成后推进 commit 基准。
        self._set_last_indexed_commit(workspace, head_commit)
        elapsed_ms = self._elapsed_ms(started)
        progress_callback(
            IndexProgress(
                workspace=workspace,
                total_files=total_files,
                total_chunks=total_chunks,
                processed_chunks=processed,
                failed_chunks=failed,
                elapsed_ms=elapsed_ms,
                message="索引任务完成",
            )
        )

        return IndexBuildResult(
            workspace=workspace,
            total_files=total_files,
            total_chunks=total_chunks,
            processed_chunks=processed,
            failed_chunks=failed,
            elapsed_ms=elapsed_ms,
            head_commit=head_commit,
        )

    def semantic_search(
        self,
        *,
        workspace: str,
        query: str,
        top_k: int,
    ) -> list[dict[str, object]]:
        cfg = mcp_settings_service.get_settings()
        if not cfg.kb_enable_vector:
            raise AppError(
                code="MCP_VECTOR_DISABLED",
                message="vector search is disabled",
                status_code=400,
            )

        query_vector = self._embed_texts([query])[0]
        collection = self._collection()
        result = collection.query(
            query_embeddings=[query_vector],
            n_results=max(1, top_k),
            where={"workspace": workspace},
            include=["metadatas", "documents", "distances"],
        )

        metadatas = (result.get("metadatas") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        hits: list[dict[str, object]] = []
        for index, metadata in enumerate(metadatas):
            if not metadata:
                continue
            distance = float(distances[index]) if index < len(distances) else 1.0
            score = 1.0 / (1.0 + max(0.0, distance))
            snippet = str(documents[index]) if index < len(documents) else ""
            hits.append(
                {
                    "chunk_id": str(metadata.get("chunk_id") or ""),
                    "path": str(metadata.get("path") or ""),
                    "start_line": int(metadata.get("start_line") or 1),
                    "end_line": int(metadata.get("end_line") or 1),
                    "score": round(score, 6),
                    "snippet": snippet[:1000],
                    "commit_sha": str(metadata.get("commit_sha") or ""),
                }
            )
        return hits

    def hybrid_search(
        self,
        *,
        workspace: str,
        query: str,
        top_k: int,
    ) -> list[dict[str, object]]:
        vector_hits = self.semantic_search(
            workspace=workspace,
            query=query,
            top_k=max(8, top_k * 3),
        )
        keywords = [item.lower() for item in re.split(r"\s+", query) if item.strip()]
        merged: list[dict[str, object]] = []
        for row in vector_hits:
            snippet = str(row.get("snippet") or "")
            keyword_score = sum(1 for token in keywords if token in snippet.lower())
            vector_score = float(row.get("score") or 0.0)
            final_score = vector_score * 0.7 + min(keyword_score, 6) * 0.05
            merged.append({**row, "score": round(final_score, 6)})

        merged.sort(key=lambda item: (-float(item["score"]), str(item["path"])))
        return merged[: max(1, top_k)]

    def _collection(self):
        try:
            import chromadb
        except Exception as exc:
            raise AppError(
                code="MCP_VECTOR_DEPENDENCY_MISSING",
                message="chromadb is required for vector search",
                details={"reason": str(exc)},
                status_code=500,
            ) from exc

        chroma_dir = Path(mcp_settings_service.get_settings().kb_chroma_dir).expanduser()
        if not chroma_dir.is_absolute():
            chroma_dir = (settings.data_dir / chroma_dir).resolve()
        chroma_dir.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(path=str(chroma_dir))
        return client.get_or_create_collection(name="jework_workspace_chunks")

    def _upsert_chunks(
        self,
        *,
        workspace: str,
        commit_sha: str,
        path: str,
        chunks: list[dict[str, object]],
        vectors: list[list[float]],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        collection = self._collection()
        ids: list[str] = []
        documents: list[str] = []
        metadatas: list[dict[str, object]] = []
        for index, row in enumerate(chunks):
            chunk_id = str(row["chunk_id"])
            ids.append(f"{workspace}:{path}:{chunk_id}")
            documents.append(str(row["text"]))
            metadatas.append(
                {
                    "workspace": workspace,
                    "path": path,
                    "start_line": int(row["start_line"]),
                    "end_line": int(row["end_line"]),
                    "chunk_id": chunk_id,
                    "commit_sha": commit_sha,
                    "updated_at": now,
                    "chunk_index": index,
                }
            )
        collection.upsert(
            ids=ids,
            embeddings=vectors,
            documents=documents,
            metadatas=metadatas,
        )

    def _delete_workspace_vectors(self, workspace: str) -> None:
        collection = self._collection()
        collection.delete(where={"workspace": workspace})

    def _delete_path_vectors(self, *, workspace: str, path: str) -> None:
        collection = self._collection()
        # Chroma 的 where 在当前版本要求显式逻辑操作符组合多条件。
        collection.delete(
            where={
                "$and": [
                    {"workspace": {"$eq": workspace}},
                    {"path": {"$eq": path}},
                ]
            }
        )

    def _build_file_chunks(self, root: Path, relative_path: str) -> list[dict[str, object]]:
        path = (root / relative_path).resolve()
        if not path.exists() or not path.is_file():
            return []

        cfg = mcp_settings_service.get_settings()
        if path.stat().st_size > cfg.kb_file_max_bytes:
            raise AppError(
                code="MCP_INDEX_FILE_TOO_LARGE",
                message="file exceeds kb_file_max_bytes",
                details={
                    "path": relative_path,
                    "size": path.stat().st_size,
                    "limit": cfg.kb_file_max_bytes,
                },
                status_code=400,
            )

        text = self._read_text(path)
        lines = text.splitlines()
        suffix = path.suffix.lower()
        if suffix in {".md", ".markdown", ".txt"}:
            return self._chunk_markdown_or_text(relative_path, lines)
        if suffix in self._CODE_SUFFIXES:
            return self._chunk_code(relative_path, lines)
        return self._chunk_by_window(relative_path, lines)

    def _chunk_markdown_or_text(
        self,
        relative_path: str,
        lines: list[str],
    ) -> list[dict[str, object]]:
        chunks: list[dict[str, object]] = []
        buffer: list[str] = []
        start_line = 1
        for idx, line in enumerate(lines, start=1):
            if not buffer:
                start_line = idx
            buffer.append(line)
            should_split = (not line.strip()) or len(buffer) >= 80
            if not should_split:
                continue
            text = "\n".join(buffer).strip()
            if text:
                chunks.append(self._make_chunk(relative_path, start_line, idx, text))
            buffer = []
        if buffer:
            end_line = len(lines)
            text = "\n".join(buffer).strip()
            if text:
                chunks.append(self._make_chunk(relative_path, start_line, end_line, text))
        return chunks

    def _chunk_code(
        self,
        relative_path: str,
        lines: list[str],
    ) -> list[dict[str, object]]:
        if not lines:
            return [self._make_chunk(relative_path, 1, 1, "")]

        boundary_pattern = re.compile(
            r"^\s*(def\s+|class\s+|func\s+|fn\s+|interface\s+|type\s+|export\s+function\s+|public\s+|private\s+|protected\s+)"
        )
        boundaries = [1]
        for idx, line in enumerate(lines, start=1):
            if idx == 1:
                continue
            if boundary_pattern.match(line):
                boundaries.append(idx)
        boundaries.append(len(lines) + 1)

        chunks: list[dict[str, object]] = []
        for index in range(len(boundaries) - 1):
            start_line = boundaries[index]
            end_line = boundaries[index + 1] - 1
            segment = lines[start_line - 1 : end_line]
            if len(segment) > 180:
                chunks.extend(
                    self._chunk_by_window(
                        relative_path,
                        segment,
                        start_offset=start_line,
                    )
                )
                continue
            text = "\n".join(segment).strip()
            if not text:
                continue
            chunks.append(self._make_chunk(relative_path, start_line, end_line, text))

        if not chunks:
            chunks = self._chunk_by_window(relative_path, lines)
        return chunks

    def _chunk_by_window(
        self,
        relative_path: str,
        lines: list[str],
        *,
        start_offset: int = 1,
        window: int = 120,
        overlap: int = 20,
    ) -> list[dict[str, object]]:
        if not lines:
            return [self._make_chunk(relative_path, start_offset, start_offset, "")]

        chunks: list[dict[str, object]] = []
        step = max(1, window - overlap)
        for index in range(0, len(lines), step):
            segment = lines[index : index + window]
            if not segment:
                continue
            start_line = start_offset + index
            end_line = start_line + len(segment) - 1
            text = "\n".join(segment).strip()
            if not text:
                continue
            chunks.append(self._make_chunk(relative_path, start_line, end_line, text))
        return chunks

    def _make_chunk(
        self,
        relative_path: str,
        start_line: int,
        end_line: int,
        text: str,
    ) -> dict[str, object]:
        digest = hashlib.sha1(
            f"{relative_path}:{start_line}:{end_line}:{text[:200]}".encode("utf-8")
        ).hexdigest()
        return {
            "chunk_id": digest,
            "path": relative_path,
            "start_line": start_line,
            "end_line": end_line,
            "text": text,
        }

    def _embed_texts(self, texts: list[str]) -> list[list[float]]:
        cfg = mcp_settings_service.get_settings()
        if not cfg.embedding_base_url or not cfg.embedding_model or not cfg.embedding_api_key:
            raise AppError(
                code="MCP_EMBEDDING_CONFIG_INVALID",
                message="embedding config is incomplete",
                details={
                    "has_base_url": bool(cfg.embedding_base_url),
                    "has_model": bool(cfg.embedding_model),
                    "has_api_key": bool(cfg.embedding_api_key),
                },
                status_code=400,
            )

        base_url = cfg.embedding_base_url.rstrip("/")
        endpoint = f"{base_url}/embeddings"
        payload = json.dumps(
            {
                "model": cfg.embedding_model,
                "input": texts,
                "encoding_format": "float",
            }
        ).encode("utf-8")
        req = urllib_request.Request(
            endpoint,
            data=payload,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg.embedding_api_key}",
            },
        )

        try:
            with urllib_request.urlopen(req, timeout=60) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib_error.HTTPError as exc:
            text = exc.read().decode("utf-8", errors="ignore")
            raise AppError(
                code="MCP_EMBEDDING_HTTP_ERROR",
                message="embedding provider request failed",
                details={"status": exc.code, "body": text[:1200]},
                status_code=502,
            ) from exc
        except Exception as exc:
            raise AppError(
                code="MCP_EMBEDDING_REQUEST_FAILED",
                message="embedding provider request failed",
                details={"reason": str(exc)},
                status_code=502,
            ) from exc

        rows = body.get("data")
        if not isinstance(rows, list) or not rows:
            raise AppError(
                code="MCP_EMBEDDING_INVALID_RESPONSE",
                message="embedding provider response is invalid",
                details={"response": body},
                status_code=502,
            )

        vectors: list[list[float]] = []
        for row in rows:
            embedding = row.get("embedding") if isinstance(row, dict) else None
            if not isinstance(embedding, list):
                raise AppError(
                    code="MCP_EMBEDDING_INVALID_RESPONSE",
                    message="embedding vector missing in response",
                    details={"row": row},
                    status_code=502,
                )
            vectors.append([float(item) for item in embedding])

        if len(vectors) != len(texts):
            raise AppError(
                code="MCP_EMBEDDING_INVALID_RESPONSE",
                message="embedding vector count mismatch",
                details={"expected": len(texts), "actual": len(vectors)},
                status_code=502,
            )
        return vectors

    def _collect_incremental_changes(
        self,
        root: Path,
        last_commit: str,
        head_commit: str,
    ) -> tuple[list[str], list[str]]:
        if last_commit == head_commit:
            return [], []

        try:
            output = self._run_git(
                root,
                ["diff", "--name-status", f"{last_commit}..{head_commit}"],
            )
        except AppError:
            return self._list_tracked_files(root), []

        changed_files: set[str] = set()
        deleted_files: set[str] = set()
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status = parts[0].strip().upper()
            path = parts[-1].strip()
            if not path:
                continue
            suffix = Path(path).suffix.lower()
            if suffix not in self._TEXT_SUFFIXES:
                continue
            if status.startswith("D"):
                deleted_files.add(path)
            else:
                changed_files.add(path)
        return sorted(changed_files), sorted(deleted_files)

    def _list_tracked_files(self, root: Path) -> list[str]:
        if self._is_git_repository(root):
            output = self._run_git(root, ["ls-files"])
            result = []
            for line in output.splitlines():
                path = line.strip()
                if not path:
                    continue
                suffix = Path(path).suffix.lower()
                if suffix not in self._TEXT_SUFFIXES:
                    continue
                result.append(path)
            return sorted(result)

        result: list[str] = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if "/.git/" in path.as_posix():
                continue
            suffix = path.suffix.lower()
            if suffix not in self._TEXT_SUFFIXES:
                continue
            result.append(str(path.relative_to(root)))
        return sorted(result)

    def _read_text(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return path.read_text(encoding="utf-8-sig")

    def _resolve_head_commit(self, root: Path) -> str:
        if self._is_git_repository(root):
            return self._run_git(root, ["rev-parse", "HEAD"]).strip()
        return datetime.now(timezone.utc).strftime("snapshot-%Y%m%d%H%M%S")

    def _is_git_repository(self, root: Path) -> bool:
        return (root / ".git").exists()

    def _run_git(self, cwd: Path, args: list[str]) -> str:
        command = ["git", *args]
        try:
            proc = subprocess.run(
                command,
                cwd=str(cwd),
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except FileNotFoundError as exc:
            raise AppError(
                code="MCP_GIT_NOT_FOUND",
                message="git command not found",
                status_code=500,
            ) from exc
        except subprocess.CalledProcessError as exc:
            reason = (exc.stderr or exc.stdout or "git command failed").strip()
            raise AppError(
                code="MCP_GIT_COMMAND_FAILED",
                message="git command failed",
                details={"args": args, "reason": reason},
                status_code=400,
            ) from exc
        return (proc.stdout or "").strip()

    def _get_last_indexed_commit(self, workspace: str) -> str | None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT last_indexed_commit
                FROM mcp_index_state
                WHERE workspace = ?
                LIMIT 1
                """,
                (workspace,),
            ).fetchone()
            if row is None:
                return None
            value = str(row["last_indexed_commit"] or "").strip()
            return value or None

    def _set_last_indexed_commit(self, workspace: str, commit_sha: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO mcp_index_state (
                    workspace, last_indexed_commit, last_indexed_at, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(workspace) DO UPDATE SET
                    last_indexed_commit=excluded.last_indexed_commit,
                    last_indexed_at=excluded.last_indexed_at,
                    updated_at=excluded.updated_at
                """,
                (workspace, commit_sha, now, now),
            )
            conn.commit()

    def _record_failure(
        self,
        job_id: str,
        workspace: str,
        path: str,
        reason: str,
        retry_count: int = 0,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO mcp_index_failures (
                    job_id, workspace, path, reason, retry_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (job_id, workspace, path, reason[:2000], retry_count, now),
            )
            conn.commit()

    def list_failures(
        self,
        *,
        workspace: str | None = None,
        job_id: str | None = None,
    ) -> list[IndexFailureRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if workspace:
            clauses.append("workspace = ?")
            params.append(workspace)
        if job_id:
            clauses.append("job_id = ?")
            params.append(job_id)

        where_clause = ""
        if clauses:
            where_clause = "WHERE " + " AND ".join(clauses)

        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                SELECT job_id, workspace, path, reason, retry_count, created_at
                FROM mcp_index_failures
                {where_clause}
                ORDER BY created_at DESC
                """,
                tuple(params),
            ).fetchall()
            return [
                IndexFailureRecord(
                    job_id=str(row["job_id"] or ""),
                    workspace=str(row["workspace"] or ""),
                    path=str(row["path"] or ""),
                    reason=str(row["reason"] or ""),
                    retry_count=int(row["retry_count"] or 0),
                    created_at=str(row["created_at"] or ""),
                )
                for row in rows
            ]

    def retry_failed_paths(
        self,
        *,
        workspace: str,
        source_job_id: str,
        retry_job_id: str,
        paths: list[str],
        progress_callback: Callable[[IndexProgress], None],
    ) -> IndexBuildResult:
        started = datetime.now(timezone.utc)
        root = workspace_service.get_workspace_path(workspace)
        head_commit = self._resolve_head_commit(root)
        unique_paths = sorted({path.strip() for path in paths if path.strip()})
        total_files = len(unique_paths)
        total_chunks = 0
        file_chunks: list[tuple[str, list[dict[str, object]]]] = []
        missing_paths: list[str] = []

        for path in unique_paths:
            absolute = (root / path).resolve()
            if not absolute.exists() or not absolute.is_file():
                missing_paths.append(path)
                continue
            chunks = self._build_file_chunks(root, path)
            file_chunks.append((path, chunks))
            total_chunks += len(chunks)

        processed = 0
        failed = 0
        progress_callback(
            IndexProgress(
                workspace=workspace,
                total_files=total_files,
                total_chunks=total_chunks,
                processed_chunks=0,
                failed_chunks=0,
                elapsed_ms=0,
                message="失败文件重试开始",
            )
        )

        for path in missing_paths:
            failed += 1
            self._increment_retry_count(source_job_id, workspace, path)
            self._record_failure(
                retry_job_id,
                workspace,
                path,
                "file not found during retry",
                retry_count=1,
            )
            progress_callback(
                IndexProgress(
                    workspace=workspace,
                    total_files=total_files,
                    total_chunks=total_chunks,
                    processed_chunks=processed,
                    failed_chunks=failed,
                    elapsed_ms=self._elapsed_ms(started),
                    message=f"重试失败（文件不存在）: {path}",
                )
            )

        batch_size = max(1, mcp_settings_service.get_settings().embedding_batch_size)
        for path, chunks in file_chunks:
            try:
                self._delete_path_vectors(workspace=workspace, path=path)
                if chunks:
                    for offset in range(0, len(chunks), batch_size):
                        part = chunks[offset : offset + batch_size]
                        texts = [str(item["text"]) for item in part]
                        vectors = self._embed_texts(texts)
                        self._upsert_chunks(
                            workspace=workspace,
                            commit_sha=head_commit,
                            path=path,
                            chunks=part,
                            vectors=vectors,
                        )
                        processed += len(part)
                self._increment_retry_count(source_job_id, workspace, path)
                self._delete_failure_rows(source_job_id, workspace, path)
                progress_callback(
                    IndexProgress(
                        workspace=workspace,
                        total_files=total_files,
                        total_chunks=total_chunks,
                        processed_chunks=processed,
                        failed_chunks=failed,
                        elapsed_ms=self._elapsed_ms(started),
                        message=f"重试成功: {path}",
                    )
                )
            except Exception as exc:
                failed += max(1, len(chunks))
                self._increment_retry_count(source_job_id, workspace, path)
                self._record_failure(
                    retry_job_id,
                    workspace,
                    path,
                    f"retry failed: {exc}",
                    retry_count=1,
                )
                progress_callback(
                    IndexProgress(
                        workspace=workspace,
                        total_files=total_files,
                        total_chunks=total_chunks,
                        processed_chunks=processed,
                        failed_chunks=failed,
                        elapsed_ms=self._elapsed_ms(started),
                        message=f"重试失败: {path}",
                    )
                )

        elapsed_ms = self._elapsed_ms(started)
        progress_callback(
            IndexProgress(
                workspace=workspace,
                total_files=total_files,
                total_chunks=total_chunks,
                processed_chunks=processed,
                failed_chunks=failed,
                elapsed_ms=elapsed_ms,
                message="失败文件重试完成",
            )
        )
        return IndexBuildResult(
            workspace=workspace,
            total_files=total_files,
            total_chunks=total_chunks,
            processed_chunks=processed,
            failed_chunks=failed,
            elapsed_ms=elapsed_ms,
            head_commit=head_commit,
        )

    def _delete_failure_rows(self, job_id: str, workspace: str, path: str) -> None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                DELETE FROM mcp_index_failures
                WHERE job_id = ? AND workspace = ? AND path = ?
                """,
                (job_id, workspace, path),
            )
            conn.commit()

    def _increment_retry_count(self, job_id: str, workspace: str, path: str) -> None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                UPDATE mcp_index_failures
                SET retry_count = retry_count + 1
                WHERE job_id = ? AND workspace = ? AND path = ?
                """,
                (job_id, workspace, path),
            )
            conn.commit()

    def _elapsed_ms(self, started: datetime) -> int:
        return int((datetime.now(timezone.utc) - started).total_seconds() * 1000)


mcp_vector_service = McpVectorService(str(settings.sqlite_db_path))
