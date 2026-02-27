from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import settings
from app.core.errors import AuthRequiredError


@dataclass(frozen=True)
class McpTokenInfo:
    has_token: bool
    token_hint: str | None
    updated_at: str | None


@dataclass(frozen=True)
class McpResetTokenResult:
    token: str
    token_hint: str
    updated_at: str


class McpTokenService:
    """管理 MCP 独立令牌。

    设计说明：
    - 令牌与业务登录态（auth_tokens）隔离，避免第三方工具依赖短会话。
    - 同时保存哈希（鉴权）与可回读密文（前端复制/配置填充）。
    - 明文不会直接落库，数据库泄露时仍需 APP_SECRET_KEY 才能还原。
    """

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mcp_tokens (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL UNIQUE,
                    token_hash TEXT NOT NULL,
                    token_prefix TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_used_at TEXT,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
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
            self._ensure_mcp_token_extension_columns(conn)
            conn.commit()

    def get_info(self, user_id: int) -> McpTokenInfo:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT token_prefix, updated_at, is_active
                FROM mcp_tokens
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return McpTokenInfo(has_token=False, token_hint=None, updated_at=None)
            if int(row["is_active"]) != 1:
                return McpTokenInfo(has_token=False, token_hint=None, updated_at=row["updated_at"])
            return McpTokenInfo(
                has_token=True,
                token_hint=str(row["token_prefix"]),
                updated_at=str(row["updated_at"]),
            )

    def reset_token(self, user_id: int) -> McpResetTokenResult:
        # 使用更长随机串，降低泄露后被穷举的风险。
        token = "mcp_" + secrets.token_urlsafe(48)
        token_hash = self._hash_token(token)
        token_encrypted = self._encrypt_token(token)
        prefix = self._token_hint(token)
        now = datetime.now(timezone.utc).isoformat()

        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO mcp_tokens (
                    user_id, token_hash, token_encrypted, token_prefix,
                    created_at, updated_at, last_used_at, is_active
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    token_hash=excluded.token_hash,
                    token_encrypted=excluded.token_encrypted,
                    token_prefix=excluded.token_prefix,
                    updated_at=excluded.updated_at,
                    last_used_at=NULL,
                    is_active=1
                """,
                (user_id, token_hash, token_encrypted, prefix, now, now),
            )
            conn.commit()

        return McpResetTokenResult(token=token, token_hint=prefix, updated_at=now)

    def get_token(self, user_id: int) -> str | None:
        """
        返回当前用户可复用的 MCP token 明文。

        兼容说明：
        - 历史版本仅存 hash，不可反解；此时返回 None。
        - 新版本会在重置时写入可回读密文，支持页面直接复制/填充配置。
        """
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT token_encrypted, is_active
                FROM mcp_tokens
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            if int(row["is_active"]) != 1:
                return None
            token_encrypted = str(row["token_encrypted"] or "").strip()
            if not token_encrypted:
                return None
            return self._decrypt_token(token_encrypted)

    def verify_token(self, token: str) -> int:
        normalized = token.strip()
        if not normalized:
            raise AuthRequiredError()

        token_hash = self._hash_token(normalized)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT user_id, is_active
                FROM mcp_tokens
                WHERE token_hash = ?
                LIMIT 1
                """,
                (token_hash,),
            ).fetchone()
            if row is None or int(row["is_active"]) != 1:
                raise AuthRequiredError()

            now = datetime.now(timezone.utc).isoformat()
            conn.execute(
                """
                UPDATE mcp_tokens
                SET last_used_at = ?
                WHERE user_id = ?
                """,
                (now, int(row["user_id"])),
            )
            conn.commit()
            return int(row["user_id"])

    def _token_hint(self, token: str) -> str:
        if len(token) <= 16:
            return token
        return f"{token[:10]}...{token[-6:]}"

    def _hash_token(self, token: str) -> str:
        key = self._resolve_crypto_key()
        return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()

    def _resolve_crypto_key(self) -> bytes:
        env_key = os.getenv("APP_SECRET_KEY", "").strip()
        if env_key:
            return env_key.encode("utf-8")

        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT value FROM system_settings
                WHERE key = ?
                LIMIT 1
                """,
                ("APP_SECRET_KEY",),
            ).fetchone()
            if row is not None:
                return str(row["value"]).encode("utf-8")

            # 与既有逻辑保持一致：若未显式配置，首次自动生成并持久化。
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

    def _ensure_mcp_token_extension_columns(self, conn: sqlite3.Connection) -> None:
        """
        兼容历史库结构：为 mcp_tokens 补充可回读密文字段。
        """
        rows = conn.execute("PRAGMA table_info(mcp_tokens)").fetchall()
        existing = {str(row[1]) for row in rows}
        if "token_encrypted" not in existing:
            conn.execute("ALTER TABLE mcp_tokens ADD COLUMN token_encrypted TEXT")

    def _encrypt_token(self, token: str) -> str:
        key = self._resolve_crypto_key()
        plaintext = token.encode("utf-8")
        nonce = secrets.token_bytes(16)
        keystream = self._keystream(key=key, nonce=nonce, size=len(plaintext))
        ciphertext = bytes(a ^ b for a, b in zip(plaintext, keystream))
        payload = nonce + ciphertext
        return base64.urlsafe_b64encode(payload).decode("utf-8")

    def _decrypt_token(self, token_encrypted: str) -> str | None:
        key = self._resolve_crypto_key()
        try:
            payload = base64.urlsafe_b64decode(token_encrypted.encode("utf-8"))
        except (binascii.Error, ValueError):
            return None
        if len(payload) <= 16:
            return None
        nonce = payload[:16]
        ciphertext = payload[16:]
        keystream = self._keystream(key=key, nonce=nonce, size=len(ciphertext))
        plaintext = bytes(a ^ b for a, b in zip(ciphertext, keystream))
        try:
            return plaintext.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def _keystream(self, *, key: bytes, nonce: bytes, size: int) -> bytes:
        buffer = bytearray()
        counter = 0
        while len(buffer) < size:
            block = hmac.new(
                key,
                nonce + counter.to_bytes(4, byteorder="big"),
                hashlib.sha256,
            ).digest()
            buffer.extend(block)
            counter += 1
        return bytes(buffer[:size])


mcp_token_service = McpTokenService(str(settings.sqlite_db_path))
