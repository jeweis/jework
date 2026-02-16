from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlparse

from app.core.config import settings
from app.core.errors import AppError


@dataclass(frozen=True)
class McpSettings:
    mcp_enabled: bool
    mcp_base_path: str
    mcp_public_base_url: str | None
    kb_enable_vector: bool
    kb_chroma_dir: str
    kb_vector_topk_default: int
    kb_file_max_bytes: int
    kb_read_max_lines: int
    embedding_backend: str
    embedding_base_url: str | None
    embedding_model: str | None
    embedding_batch_size: int
    embedding_api_key: str | None


@dataclass(frozen=True)
class McpSettingsView:
    mcp_enabled: bool
    mcp_base_path: str
    mcp_public_base_url: str | None
    kb_enable_vector: bool
    kb_chroma_dir: str
    kb_vector_topk_default: int
    kb_file_max_bytes: int
    kb_read_max_lines: int
    embedding_backend: str
    embedding_base_url: str | None
    embedding_model: str | None
    embedding_batch_size: int
    has_embedding_api_key: bool
    editable_fields: list[str]
    updated_at: str | None


class McpSettingsService:
    _KEY_MCP_ENABLED = "MCP_ENABLED"
    _KEY_MCP_BASE_PATH = "MCP_BASE_PATH"
    _KEY_MCP_PUBLIC_BASE_URL = "MCP_PUBLIC_BASE_URL"
    _KEY_KB_ENABLE_VECTOR = "KB_ENABLE_VECTOR"
    _KEY_KB_CHROMA_DIR = "KB_CHROMA_DIR"
    _KEY_KB_VECTOR_TOPK_DEFAULT = "KB_VECTOR_TOPK_DEFAULT"
    _KEY_KB_FILE_MAX_BYTES = "KB_FILE_MAX_BYTES"
    _KEY_KB_READ_MAX_LINES = "KB_READ_MAX_LINES"
    _KEY_EMBEDDING_BACKEND = "EMBEDDING_BACKEND"
    _KEY_EMBEDDING_BASE_URL = "EMBEDDING_BASE_URL"
    _KEY_EMBEDDING_MODEL = "EMBEDDING_MODEL"
    _KEY_EMBEDDING_BATCH_SIZE = "EMBEDDING_BATCH_SIZE"
    _KEY_EMBEDDING_API_KEY_ENCRYPTED = "EMBEDDING_API_KEY_ENCRYPTED"

    _ALL_KEYS = [
        _KEY_MCP_ENABLED,
        _KEY_MCP_BASE_PATH,
        _KEY_MCP_PUBLIC_BASE_URL,
        _KEY_KB_ENABLE_VECTOR,
        _KEY_KB_CHROMA_DIR,
        _KEY_KB_VECTOR_TOPK_DEFAULT,
        _KEY_KB_FILE_MAX_BYTES,
        _KEY_KB_READ_MAX_LINES,
        _KEY_EMBEDDING_BACKEND,
        _KEY_EMBEDDING_BASE_URL,
        _KEY_EMBEDDING_MODEL,
        _KEY_EMBEDDING_BATCH_SIZE,
        _KEY_EMBEDDING_API_KEY_ENCRYPTED,
    ]

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()

    def get_settings(self) -> McpSettings:
        all_values = self._get_all_settings()

        encrypted_api_key = self._normalize(
            all_values.get(self._KEY_EMBEDDING_API_KEY_ENCRYPTED)
        )
        api_key = self._decrypt(encrypted_api_key) if encrypted_api_key else None

        return McpSettings(
            mcp_enabled=self._to_bool(all_values.get(self._KEY_MCP_ENABLED), True),
            mcp_base_path=self._normalize_mcp_path(
                self._normalize(all_values.get(self._KEY_MCP_BASE_PATH)) or "/mcp"
            ),
            mcp_public_base_url=self._normalize_base_url(
                self._normalize(all_values.get(self._KEY_MCP_PUBLIC_BASE_URL))
            ),
            kb_enable_vector=self._to_bool(
                all_values.get(self._KEY_KB_ENABLE_VECTOR),
                True,
            ),
            kb_chroma_dir=self._normalize(all_values.get(self._KEY_KB_CHROMA_DIR))
            or "./data/chroma",
            kb_vector_topk_default=self._to_int(
                all_values.get(self._KEY_KB_VECTOR_TOPK_DEFAULT),
                8,
                min_value=1,
                max_value=50,
            ),
            kb_file_max_bytes=self._to_int(
                all_values.get(self._KEY_KB_FILE_MAX_BYTES),
                1_048_576,
                min_value=1024,
                max_value=20_971_520,
            ),
            kb_read_max_lines=self._to_int(
                all_values.get(self._KEY_KB_READ_MAX_LINES),
                2000,
                min_value=10,
                max_value=20_000,
            ),
            embedding_backend=self._normalize(
                all_values.get(self._KEY_EMBEDDING_BACKEND)
            )
            or "openai_compatible",
            embedding_base_url=self._normalize_base_url(
                self._normalize(all_values.get(self._KEY_EMBEDDING_BASE_URL))
            ),
            embedding_model=self._normalize(all_values.get(self._KEY_EMBEDDING_MODEL)),
            embedding_batch_size=self._to_int(
                all_values.get(self._KEY_EMBEDDING_BATCH_SIZE),
                32,
                min_value=1,
                max_value=512,
            ),
            embedding_api_key=api_key,
        )

    def get_settings_view(self, *, is_superadmin: bool) -> McpSettingsView:
        settings_value = self.get_settings()
        updated_at = self._latest_updated_at()
        editable_fields = self._editable_fields(is_superadmin=is_superadmin)
        return McpSettingsView(
            mcp_enabled=settings_value.mcp_enabled,
            mcp_base_path=settings_value.mcp_base_path,
            mcp_public_base_url=settings_value.mcp_public_base_url,
            kb_enable_vector=settings_value.kb_enable_vector,
            kb_chroma_dir=settings_value.kb_chroma_dir,
            kb_vector_topk_default=settings_value.kb_vector_topk_default,
            kb_file_max_bytes=settings_value.kb_file_max_bytes,
            kb_read_max_lines=settings_value.kb_read_max_lines,
            embedding_backend=settings_value.embedding_backend,
            embedding_base_url=settings_value.embedding_base_url,
            embedding_model=settings_value.embedding_model,
            embedding_batch_size=settings_value.embedding_batch_size,
            has_embedding_api_key=bool(settings_value.embedding_api_key),
            editable_fields=editable_fields,
            updated_at=updated_at,
        )

    def update_settings(
        self,
        *,
        is_superadmin: bool,
        mcp_enabled: bool | None,
        mcp_base_path: str | None,
        mcp_public_base_url: str | None,
        kb_enable_vector: bool | None,
        kb_chroma_dir: str | None,
        kb_vector_topk_default: int | None,
        kb_file_max_bytes: int | None,
        kb_read_max_lines: int | None,
        embedding_backend: str | None,
        embedding_base_url: str | None,
        embedding_model: str | None,
        embedding_batch_size: int | None,
        embedding_api_key: str | None,
        clear_embedding_api_key: bool | None,
    ) -> McpSettingsView:
        if not is_superadmin:
            raise AppError(
                code="MCP_SETTINGS_FORBIDDEN",
                message="Only superadmin can update MCP global settings",
                status_code=403,
            )

        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            if mcp_enabled is not None:
                self._upsert_setting(
                    conn, self._KEY_MCP_ENABLED, "true" if mcp_enabled else "false", now
                )
            if mcp_base_path is not None:
                self._upsert_setting(
                    conn,
                    self._KEY_MCP_BASE_PATH,
                    self._normalize_mcp_path(mcp_base_path),
                    now,
                )
            if mcp_public_base_url is not None:
                normalized_public = self._normalize_base_url(mcp_public_base_url)
                self._upsert_setting(
                    conn,
                    self._KEY_MCP_PUBLIC_BASE_URL,
                    normalized_public or "",
                    now,
                )
            if kb_enable_vector is not None:
                self._upsert_setting(
                    conn,
                    self._KEY_KB_ENABLE_VECTOR,
                    "true" if kb_enable_vector else "false",
                    now,
                )
            if kb_chroma_dir is not None:
                self._upsert_setting(
                    conn,
                    self._KEY_KB_CHROMA_DIR,
                    self._normalize(kb_chroma_dir) or "./data/chroma",
                    now,
                )
            if kb_vector_topk_default is not None:
                self._upsert_setting(
                    conn,
                    self._KEY_KB_VECTOR_TOPK_DEFAULT,
                    str(max(1, min(kb_vector_topk_default, 50))),
                    now,
                )
            if kb_file_max_bytes is not None:
                self._upsert_setting(
                    conn,
                    self._KEY_KB_FILE_MAX_BYTES,
                    str(max(1024, min(kb_file_max_bytes, 20_971_520))),
                    now,
                )
            if kb_read_max_lines is not None:
                self._upsert_setting(
                    conn,
                    self._KEY_KB_READ_MAX_LINES,
                    str(max(10, min(kb_read_max_lines, 20_000))),
                    now,
                )
            if embedding_backend is not None:
                self._upsert_setting(
                    conn,
                    self._KEY_EMBEDDING_BACKEND,
                    self._normalize(embedding_backend) or "openai_compatible",
                    now,
                )
            if embedding_base_url is not None:
                normalized_embedding_url = self._normalize_base_url(embedding_base_url)
                self._upsert_setting(
                    conn,
                    self._KEY_EMBEDDING_BASE_URL,
                    normalized_embedding_url or "",
                    now,
                )
            if embedding_model is not None:
                self._upsert_setting(
                    conn,
                    self._KEY_EMBEDDING_MODEL,
                    self._normalize(embedding_model) or "",
                    now,
                )
            if embedding_batch_size is not None:
                self._upsert_setting(
                    conn,
                    self._KEY_EMBEDDING_BATCH_SIZE,
                    str(max(1, min(embedding_batch_size, 512))),
                    now,
                )

            if clear_embedding_api_key is True:
                self._upsert_setting(conn, self._KEY_EMBEDDING_API_KEY_ENCRYPTED, "", now)
            elif embedding_api_key is not None:
                normalized_key = self._normalize(embedding_api_key)
                encrypted = self._encrypt(normalized_key) if normalized_key else ""
                self._upsert_setting(
                    conn,
                    self._KEY_EMBEDDING_API_KEY_ENCRYPTED,
                    encrypted,
                    now,
                )

            conn.commit()

        return self.get_settings_view(is_superadmin=is_superadmin)

    def resolve_mcp_base_path(self) -> str:
        """运行时读取当前 MCP path，支持修改后立即生效。"""
        settings_value = self.get_settings()
        return settings_value.mcp_base_path

    def build_mcp_url(self, host_base_url: str) -> tuple[str, str]:
        cfg = self.get_settings()
        base_url = cfg.mcp_public_base_url or host_base_url.rstrip("/")
        base_path = cfg.mcp_base_path.rstrip("/") or "/mcp"
        # 对外统一返回“无尾斜杠”端点，降低客户端配置歧义。
        # 运行时由入口中间件兼容 /mcp -> /mcp/ 的内部改写。
        mcp_url = f"{base_url}{base_path}"
        template = f"{base_url}{base_path}/{{workspace}}"
        return mcp_url, template

    def _editable_fields(self, *, is_superadmin: bool) -> list[str]:
        if not is_superadmin:
            return []
        return [
            "mcp_enabled",
            "mcp_base_path",
            "mcp_public_base_url",
            "kb_enable_vector",
            "kb_chroma_dir",
            "kb_vector_topk_default",
            "kb_file_max_bytes",
            "kb_read_max_lines",
            "embedding_backend",
            "embedding_base_url",
            "embedding_model",
            "embedding_batch_size",
            "embedding_api_key",
            "clear_embedding_api_key",
        ]

    def _upsert_setting(self, conn: sqlite3.Connection, key: str, value: str, now: str) -> None:
        conn.execute(
            """
            INSERT INTO system_settings (key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value=excluded.value,
                updated_at=excluded.updated_at
            """,
            (key, value, now, now),
        )

    def _get_all_settings(self) -> dict[str, str]:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT key, value
                FROM system_settings
                WHERE key IN ({})
                """.format(
                    ",".join("?" for _ in self._ALL_KEYS)
                ),
                tuple(self._ALL_KEYS),
            ).fetchall()
            return {str(row["key"]): str(row["value"]) for row in rows}

    def _latest_updated_at(self) -> str | None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT MAX(updated_at) AS latest
                FROM system_settings
                WHERE key IN ({})
                """.format(
                    ",".join("?" for _ in self._ALL_KEYS)
                ),
                tuple(self._ALL_KEYS),
            ).fetchone()
            if row is None:
                return None
            latest = row["latest"]
            return str(latest) if latest else None

    def _normalize(self, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed if trimmed else None

    def _normalize_mcp_path(self, path: str) -> str:
        text = self._normalize(path)
        if not text:
            return "/mcp"
        normalized = text
        if not normalized.startswith("/"):
            normalized = "/" + normalized
        normalized = normalized.rstrip("/")
        if normalized == "":
            normalized = "/mcp"
        # 防止 path 含空格等非法字符导致路由异常。
        if " " in normalized:
            raise AppError(
                code="MCP_SETTINGS_INVALID",
                message="mcp_base_path contains invalid whitespace",
                details={"mcp_base_path": path},
                status_code=400,
            )
        return normalized

    def _normalize_base_url(self, base_url: str | None) -> str | None:
        text = self._normalize(base_url)
        if text is None:
            return None
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise AppError(
                code="MCP_SETTINGS_INVALID",
                message="mcp_public_base_url must be http(s) URL",
                details={"mcp_public_base_url": base_url},
                status_code=400,
            )
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip("/")

    def _to_bool(self, raw: str | None, default: bool) -> bool:
        if raw is None:
            return default
        return raw.strip().lower() == "true"

    def _to_int(
        self,
        raw: str | None,
        default: int,
        *,
        min_value: int,
        max_value: int,
    ) -> int:
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError:
            return default
        return max(min_value, min(value, max_value))

    def _resolve_crypto_key(self) -> bytes:
        env_key = os.getenv("APP_SECRET_KEY", "").strip()
        if env_key:
            return env_key.encode("utf-8")

        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT value
                FROM system_settings
                WHERE key = ?
                """,
                ("APP_SECRET_KEY",),
            ).fetchone()
            if row is not None:
                return str(row["value"]).encode("utf-8")

            generated = secrets.token_urlsafe(48)
            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                INSERT INTO system_settings (key, value, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    updated_at=excluded.updated_at
                """,
                ("APP_SECRET_KEY", generated, now, now),
            )
            conn.commit()
            return generated.encode("utf-8")

    def _encrypt(self, plain: str | None) -> str | None:
        if plain is None:
            return None
        key = self._resolve_crypto_key()
        nonce = os.urandom(16)
        raw = plain.encode("utf-8")
        encrypted = bytes(
            b ^ self._keystream_byte(key, nonce, i) for i, b in enumerate(raw)
        )
        tag = hmac.new(key, nonce + encrypted, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(nonce + tag + encrypted).decode("utf-8")

    def _decrypt(self, encoded: str) -> str:
        raw = base64.urlsafe_b64decode(encoded.encode("utf-8"))
        if len(raw) < 48:
            raise AppError(
                code="MCP_SETTINGS_INVALID",
                message="invalid encrypted embedding api key payload",
                status_code=400,
            )
        nonce = raw[:16]
        tag = raw[16:48]
        encrypted = raw[48:]
        key = self._resolve_crypto_key()
        expected = hmac.new(key, nonce + encrypted, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise AppError(
                code="MCP_SETTINGS_INVALID",
                message="embedding api key signature validation failed",
                status_code=400,
            )
        plain = bytes(
            b ^ self._keystream_byte(key, nonce, i) for i, b in enumerate(encrypted)
        )
        return plain.decode("utf-8")

    def _keystream_byte(self, key: bytes, nonce: bytes, index: int) -> int:
        block = index // 32
        offset = index % 32
        material = nonce + block.to_bytes(8, "big")
        digest = hashlib.sha256(key + material).digest()
        return digest[offset]


mcp_settings_service = McpSettingsService(str(settings.sqlite_db_path))
