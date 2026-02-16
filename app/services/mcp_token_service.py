from __future__ import annotations

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
    - 数据库存储哈希值，不保存明文 token；明文仅在重置当次返回。
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
        prefix = self._token_hint(token)
        now = datetime.now(timezone.utc).isoformat()

        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                INSERT INTO mcp_tokens (
                    user_id, token_hash, token_prefix,
                    created_at, updated_at, last_used_at, is_active
                )
                VALUES (?, ?, ?, ?, ?, NULL, 1)
                ON CONFLICT(user_id) DO UPDATE SET
                    token_hash=excluded.token_hash,
                    token_prefix=excluded.token_prefix,
                    updated_at=excluded.updated_at,
                    last_used_at=NULL,
                    is_active=1
                """,
                (user_id, token_hash, prefix, now, now),
            )
            conn.commit()

        return McpResetTokenResult(token=token, token_hint=prefix, updated_at=now)

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


mcp_token_service = McpTokenService(str(settings.sqlite_db_path))
