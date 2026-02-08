from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3

from app.core.config import settings
from app.core.errors import AppError, AuthForbiddenError
from app.services.auth_service import AuthUser


class LlmConfigNotFoundError(AppError):
    def __init__(self, config_id: int):
        super().__init__(
            code="LLM_CONFIG_NOT_FOUND",
            message=f"LLM config not found: {config_id}",
            details={"config_id": config_id},
            status_code=404,
        )


@dataclass
class LlmConfig:
    id: int
    name: str
    base_url: str | None
    auth_token: str | None
    model: str | None
    default_sonnet_model: str | None
    default_haiku_model: str | None
    default_opus_model: str | None
    is_active: bool
    created_at: str
    updated_at: str


class LlmConfigService:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def init_db(self) -> None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    base_url TEXT,
                    auth_token TEXT,
                    model TEXT,
                    default_sonnet_model TEXT,
                    default_haiku_model TEXT,
                    default_opus_model TEXT,
                    is_active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_by INTEGER NOT NULL
                )
                """
            )
            conn.commit()

    def list_configs(self, current_user: AuthUser) -> list[LlmConfig]:
        self._ensure_superadmin(current_user)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, name, base_url, auth_token, model,
                       default_sonnet_model, default_haiku_model, default_opus_model,
                       is_active, created_at, updated_at
                FROM llm_configs
                ORDER BY is_active DESC, updated_at DESC, id DESC
                """
            ).fetchall()
            return [self._row_to_config(row) for row in rows]

    def create_config(
        self,
        current_user: AuthUser,
        *,
        name: str,
        base_url: str | None,
        auth_token: str | None,
        model: str | None,
        default_sonnet_model: str | None,
        default_haiku_model: str | None,
        default_opus_model: str | None,
    ) -> LlmConfig:
        self._ensure_superadmin(current_user)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO llm_configs (
                        name, base_url, auth_token, model,
                        default_sonnet_model, default_haiku_model, default_opus_model,
                        is_active, created_at, updated_at, created_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
                    """,
                    (
                        name,
                        self._normalize_optional(base_url),
                        self._normalize_optional(auth_token),
                        self._normalize_optional(model),
                        self._normalize_optional(default_sonnet_model),
                        self._normalize_optional(default_haiku_model),
                        self._normalize_optional(default_opus_model),
                        now,
                        now,
                        current_user.id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AppError(
                    code="LLM_CONFIG_NAME_EXISTS",
                    message=f"LLM config already exists: {name}",
                    status_code=409,
                ) from exc
            conn.commit()
            config_id = int(cursor.lastrowid)
        return self.get_config(current_user, config_id)

    def update_config(
        self,
        current_user: AuthUser,
        config_id: int,
        *,
        name: str | None,
        base_url: str | None,
        auth_token: str | None,
        model: str | None,
        default_sonnet_model: str | None,
        default_haiku_model: str | None,
        default_opus_model: str | None,
    ) -> LlmConfig:
        self._ensure_superadmin(current_user)
        existing = self.get_config(current_user, config_id)
        now = datetime.now(timezone.utc).isoformat()

        next_auth_token = existing.auth_token
        if auth_token is not None:
            token_normalized = self._normalize_optional(auth_token)
            next_auth_token = token_normalized

        with closing(sqlite3.connect(self._db_path)) as conn:
            try:
                conn.execute(
                    """
                    UPDATE llm_configs
                    SET name=?, base_url=?, auth_token=?, model=?,
                        default_sonnet_model=?, default_haiku_model=?, default_opus_model=?,
                        updated_at=?
                    WHERE id=?
                    """,
                    (
                        name or existing.name,
                        self._pick_optional(base_url, existing.base_url),
                        next_auth_token,
                        self._pick_optional(model, existing.model),
                        self._pick_optional(
                            default_sonnet_model, existing.default_sonnet_model
                        ),
                        self._pick_optional(
                            default_haiku_model, existing.default_haiku_model
                        ),
                        self._pick_optional(
                            default_opus_model, existing.default_opus_model
                        ),
                        now,
                        config_id,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AppError(
                    code="LLM_CONFIG_NAME_EXISTS",
                    message=f"LLM config already exists: {name}",
                    status_code=409,
                ) from exc
            conn.commit()
        return self.get_config(current_user, config_id)

    def activate_config(self, current_user: AuthUser, config_id: int) -> LlmConfig:
        self._ensure_superadmin(current_user)
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute("UPDATE llm_configs SET is_active=0, updated_at=?", (now,))
            cursor = conn.execute(
                "UPDATE llm_configs SET is_active=1, updated_at=? WHERE id=?",
                (now, config_id),
            )
            if cursor.rowcount == 0:
                raise LlmConfigNotFoundError(config_id)
            conn.commit()
        return self.get_config(current_user, config_id)

    def delete_config(self, current_user: AuthUser, config_id: int) -> None:
        self._ensure_superadmin(current_user)
        with closing(sqlite3.connect(self._db_path)) as conn:
            cursor = conn.execute("DELETE FROM llm_configs WHERE id=?", (config_id,))
            conn.commit()
            if cursor.rowcount == 0:
                raise LlmConfigNotFoundError(config_id)

    def get_config(self, current_user: AuthUser, config_id: int) -> LlmConfig:
        self._ensure_superadmin(current_user)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, name, base_url, auth_token, model,
                       default_sonnet_model, default_haiku_model, default_opus_model,
                       is_active, created_at, updated_at
                FROM llm_configs
                WHERE id=?
                """,
                (config_id,),
            ).fetchone()
            if row is None:
                raise LlmConfigNotFoundError(config_id)
            return self._row_to_config(row)

    def get_active_env(self) -> dict[str, str]:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT base_url, auth_token, model,
                       default_sonnet_model, default_haiku_model, default_opus_model
                FROM llm_configs
                WHERE is_active=1
                ORDER BY updated_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if row is None:
                return {}

            env: dict[str, str] = {}
            self._set_env_if_present(env, "ANTHROPIC_BASE_URL", row["base_url"])
            self._set_env_if_present(env, "ANTHROPIC_AUTH_TOKEN", row["auth_token"])
            self._set_env_if_present(env, "ANTHROPIC_MODEL", row["model"])
            self._set_env_if_present(
                env, "ANTHROPIC_DEFAULT_SONNET_MODEL", row["default_sonnet_model"]
            )
            self._set_env_if_present(
                env, "ANTHROPIC_DEFAULT_HAIKU_MODEL", row["default_haiku_model"]
            )
            self._set_env_if_present(
                env, "ANTHROPIC_DEFAULT_OPUS_MODEL", row["default_opus_model"]
            )
            return env

    def _ensure_superadmin(self, current_user: AuthUser) -> None:
        if current_user.role != "superadmin":
            raise AuthForbiddenError()

    def _set_env_if_present(self, env: dict[str, str], key: str, value: str | None) -> None:
        normalized = self._normalize_optional(value)
        if normalized is not None:
            env[key] = normalized

    def _normalize_optional(self, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if stripped == "":
            return None
        return stripped

    def _pick_optional(self, incoming: str | None, existing: str | None) -> str | None:
        if incoming is None:
            return existing
        return self._normalize_optional(incoming)

    def _row_to_config(self, row: sqlite3.Row) -> LlmConfig:
        return LlmConfig(
            id=row["id"],
            name=row["name"],
            base_url=row["base_url"],
            auth_token=row["auth_token"],
            model=row["model"],
            default_sonnet_model=row["default_sonnet_model"],
            default_haiku_model=row["default_haiku_model"],
            default_opus_model=row["default_opus_model"],
            is_active=bool(row["is_active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


llm_config_service = LlmConfigService(str(settings.sqlite_db_path))
