import base64
import hashlib
import hmac
import os
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from app.core.config import settings
from app.core.errors import WorkspaceCredentialError


@dataclass
class WorkspaceCredentialSummary:
    workspace: str
    git_url: str | None
    git_username: str | None
    has_git_pat: bool


@dataclass
class WorkspaceCredentialDetail:
    workspace: str
    git_url: str | None
    git_username: str | None
    git_pat: str | None


class WorkspaceCredentialService:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path

    def init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS workspace_credentials (
                    workspace TEXT PRIMARY KEY,
                    git_url TEXT,
                    git_username TEXT,
                    git_pat_encrypted TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    created_by INTEGER NOT NULL,
                    updated_by INTEGER NOT NULL
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

    def upsert_workspace_credential(
        self,
        workspace: str,
        user_id: int,
        git_url: str | None = None,
        git_username: str | None = None,
        git_pat: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        normalized_url = self._normalize_optional(git_url)
        normalized_user = self._normalize_optional(git_username)
        encrypted_pat = self._encrypt_pat(git_pat) if git_pat is not None else None

        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            existing = conn.execute(
                """
                SELECT workspace, git_url, git_username, git_pat_encrypted,
                       created_at, created_by
                FROM workspace_credentials
                WHERE workspace = ?
                """,
                (workspace,),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO workspace_credentials (
                        workspace, git_url, git_username, git_pat_encrypted,
                        created_at, updated_at, created_by, updated_by
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace,
                        normalized_url,
                        normalized_user,
                        encrypted_pat,
                        now,
                        now,
                        user_id,
                        user_id,
                    ),
                )
            else:
                next_url = normalized_url if git_url is not None else existing["git_url"]
                next_user = (
                    normalized_user if git_username is not None else existing["git_username"]
                )
                next_pat = (
                    encrypted_pat if git_pat is not None else existing["git_pat_encrypted"]
                )
                conn.execute(
                    """
                    UPDATE workspace_credentials
                    SET git_url = ?, git_username = ?, git_pat_encrypted = ?,
                        updated_at = ?, updated_by = ?
                    WHERE workspace = ?
                    """,
                    (
                        next_url,
                        next_user,
                        next_pat,
                        now,
                        user_id,
                        workspace,
                    ),
                )
            conn.commit()

    def get_workspace_credential(self, workspace: str) -> WorkspaceCredentialSummary | None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT workspace, git_url, git_username, git_pat_encrypted
                FROM workspace_credentials
                WHERE workspace = ?
                """,
                (workspace,),
            ).fetchone()
            if row is None:
                return None
            return WorkspaceCredentialSummary(
                workspace=row["workspace"],
                git_url=row["git_url"],
                git_username=row["git_username"],
                has_git_pat=bool(row["git_pat_encrypted"]),
            )

    def get_workspace_credential_detail(
        self, workspace: str
    ) -> WorkspaceCredentialDetail | None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT workspace, git_url, git_username, git_pat_encrypted
                FROM workspace_credentials
                WHERE workspace = ?
                """,
                (workspace,),
            ).fetchone()
            if row is None:
                return None
            return WorkspaceCredentialDetail(
                workspace=row["workspace"],
                git_url=row["git_url"],
                git_username=row["git_username"],
                git_pat=self._decrypt_pat(row["git_pat_encrypted"]),
            )

    def list_workspace_credentials(self) -> dict[str, WorkspaceCredentialSummary]:
        result: dict[str, WorkspaceCredentialSummary] = {}
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT workspace, git_url, git_username, git_pat_encrypted
                FROM workspace_credentials
                """
            ).fetchall()
            for row in rows:
                item = WorkspaceCredentialSummary(
                    workspace=row["workspace"],
                    git_url=row["git_url"],
                    git_username=row["git_username"],
                    has_git_pat=bool(row["git_pat_encrypted"]),
                )
                result[item.workspace] = item
        return result

    def delete_workspace_credential(self, workspace: str) -> None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                DELETE FROM workspace_credentials
                WHERE workspace = ?
                """,
                (workspace,),
            )
            conn.commit()

    def _normalize_optional(self, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed if trimmed else None

    def _encrypt_pat(self, plain: str | None) -> str | None:
        normalized = self._normalize_optional(plain)
        if normalized is None:
            return None

        key = self._resolve_crypto_key()

        nonce = os.urandom(16)
        raw = normalized.encode("utf-8")
        encrypted = bytes(
            b ^ self._keystream_byte(key, nonce, i) for i, b in enumerate(raw)
        )
        tag = hmac.new(key, nonce + encrypted, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(nonce + tag + encrypted).decode("utf-8")

    def _decrypt_pat(self, encoded: str | None) -> str | None:
        normalized = self._normalize_optional(encoded)
        if normalized is None:
            return None
        try:
            raw = base64.urlsafe_b64decode(normalized.encode("utf-8"))
        except Exception as exc:
            raise WorkspaceCredentialError("invalid encrypted PAT payload") from exc

        if len(raw) < 48:
            raise WorkspaceCredentialError("invalid encrypted PAT payload")
        nonce = raw[:16]
        tag = raw[16:48]
        encrypted = raw[48:]
        key = self._resolve_crypto_key()
        expected = hmac.new(key, nonce + encrypted, hashlib.sha256).digest()
        if not hmac.compare_digest(tag, expected):
            raise WorkspaceCredentialError("invalid PAT signature")

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
            try:
                conn.execute(
                    """
                    INSERT INTO system_settings (key, value, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    ("APP_SECRET_KEY", generated, now, now),
                )
                conn.commit()
                return generated.encode("utf-8")
            except sqlite3.IntegrityError:
                row = conn.execute(
                    """
                    SELECT value
                    FROM system_settings
                    WHERE key = ?
                    """,
                    ("APP_SECRET_KEY",),
                ).fetchone()
                if row is None:
                    raise WorkspaceCredentialError(
                        "failed to create APP_SECRET_KEY in database"
                    )
                return str(row["value"]).encode("utf-8")


workspace_credential_service = WorkspaceCredentialService(str(settings.sqlite_db_path))
