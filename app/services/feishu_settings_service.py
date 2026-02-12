from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import settings
from app.core.errors import AppError


@dataclass(frozen=True)
class FeishuSettings:
    enabled: bool
    app_id: str | None
    app_secret: str | None
    base_url: str
    default_workspace_names: list[str]


@dataclass(frozen=True)
class FeishuSettingsView:
    enabled: bool
    app_id: str | None
    has_app_secret: bool
    base_url: str
    default_workspace_names: list[str]


class FeishuSettingsService:
    """
    飞书配置服务（数据库持久化）。

    设计目标：
    1) 将第三方配置与登录主链路解耦，关闭飞书时不影响原有账号密码体系。
    2) 敏感配置（AppSecret）以密文保存，统一复用 APP_SECRET_KEY。
    3) 提供“运行时视图”和“完整配置”两种读取方式，避免前端拿到敏感字段。
    """

    _KEY_ENABLED = "FEISHU_ENABLED"
    _KEY_APP_ID = "FEISHU_APP_ID"
    _KEY_APP_SECRET_ENCRYPTED = "FEISHU_APP_SECRET_ENCRYPTED"
    _KEY_BASE_URL = "FEISHU_BASE_URL"
    _KEY_DEFAULT_WORKSPACES = "FEISHU_DEFAULT_WORKSPACES"

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

    def get_public_status(self) -> FeishuSettingsView:
        config = self.get_active_config()
        enabled = (
            config.enabled
            and bool(config.app_id)
            and bool(config.app_secret)
        )
        return FeishuSettingsView(
            enabled=enabled,
            app_id=config.app_id if enabled else None,
            has_app_secret=bool(config.app_secret),
            base_url=config.base_url,
            default_workspace_names=config.default_workspace_names,
        )

    def get_settings_view(self) -> FeishuSettingsView:
        config = self.get_active_config()
        return FeishuSettingsView(
            enabled=config.enabled,
            app_id=config.app_id,
            has_app_secret=bool(config.app_secret),
            base_url=config.base_url,
            default_workspace_names=config.default_workspace_names,
        )

    def get_active_config(self) -> FeishuSettings:
        enabled_raw = self._get_setting(self._KEY_ENABLED)
        app_id = self._normalize(self._get_setting(self._KEY_APP_ID))
        base_url = self._normalize(self._get_setting(self._KEY_BASE_URL))
        encrypted_secret = self._normalize(self._get_setting(self._KEY_APP_SECRET_ENCRYPTED))
        default_workspaces_raw = self._normalize(
            self._get_setting(self._KEY_DEFAULT_WORKSPACES)
        )

        app_secret: str | None = None
        if encrypted_secret:
            app_secret = self._decrypt(encrypted_secret)

        default_workspace_names = self._parse_workspace_names(default_workspaces_raw)

        return FeishuSettings(
            enabled=(enabled_raw or "false").lower() == "true",
            app_id=app_id,
            app_secret=app_secret,
            base_url=base_url or "https://open.feishu.cn",
            default_workspace_names=default_workspace_names,
        )

    def update_settings(
        self,
        *,
        enabled: bool | None,
        app_id: str | None,
        app_secret: str | None,
        base_url: str | None,
        default_workspace_names: list[str] | None,
    ) -> FeishuSettingsView:
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            if enabled is not None:
                self._upsert_setting(conn, self._KEY_ENABLED, "true" if enabled else "false", now)
            if app_id is not None:
                self._upsert_setting(conn, self._KEY_APP_ID, self._normalize(app_id) or "", now)
            if base_url is not None:
                normalized_base_url = (self._normalize(base_url) or "https://open.feishu.cn").rstrip("/")
                self._upsert_setting(conn, self._KEY_BASE_URL, normalized_base_url, now)
            if app_secret is not None:
                normalized_secret = self._normalize(app_secret)
                encrypted = self._encrypt(normalized_secret) if normalized_secret else ""
                self._upsert_setting(conn, self._KEY_APP_SECRET_ENCRYPTED, encrypted, now)
            if default_workspace_names is not None:
                normalized = self._normalize_workspace_names(default_workspace_names)
                self._upsert_setting(
                    conn,
                    self._KEY_DEFAULT_WORKSPACES,
                    json.dumps(normalized, ensure_ascii=False),
                    now,
                )
            conn.commit()
        return self.get_settings_view()

    def assert_login_enabled(self) -> FeishuSettings:
        config = self.get_active_config()
        if not config.enabled:
            raise AppError(
                code="FEISHU_NOT_ENABLED",
                message="Feishu login is disabled in settings",
                status_code=400,
            )
        if not config.app_id or not config.app_secret:
            raise AppError(
                code="FEISHU_ENV_INVALID",
                message="Feishu AppID/AppSecret is not configured",
                status_code=400,
            )
        return config

    def _upsert_setting(
        self,
        conn: sqlite3.Connection,
        key: str,
        value: str,
        now: str,
    ) -> None:
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

    def _get_setting(self, key: str) -> str | None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT value
                FROM system_settings
                WHERE key = ?
                """,
                (key,),
            ).fetchone()
            if row is None:
                return None
            return str(row["value"])

    def _normalize(self, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed if trimmed else None

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
        try:
            raw = base64.urlsafe_b64decode(encoded.encode("utf-8"))
        except Exception as exc:
            raise AppError(
                code="FEISHU_ENV_INVALID",
                message="Invalid encrypted Feishu secret payload",
                details={"reason": str(exc)},
                status_code=400,
            ) from exc

        if len(raw) < 48:
            raise AppError(
                code="FEISHU_ENV_INVALID",
                message="Invalid encrypted Feishu secret payload",
                status_code=400,
            )
        nonce = raw[:16]
        tag = raw[16:48]
        encrypted = raw[48:]
        key = self._resolve_crypto_key()
        expected = hmac.new(key, nonce + encrypted, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise AppError(
                code="FEISHU_ENV_INVALID",
                message="Feishu secret signature validation failed",
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

    def _normalize_workspace_names(self, names: list[str]) -> list[str]:
        # 统一清洗工作空间名称，避免写入重复值与空值。
        normalized = {name.strip() for name in names if name and name.strip()}
        return sorted(normalized)

    def _parse_workspace_names(self, raw: str | None) -> list[str]:
        if not raw:
            return []
        try:
            payload = json.loads(raw)
        except Exception:
            return []
        if not isinstance(payload, list):
            return []
        return self._normalize_workspace_names(
            [item for item in payload if isinstance(item, str)]
        )


feishu_settings_service = FeishuSettingsService(str(settings.sqlite_db_path))
