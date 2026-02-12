import hashlib
import hmac
import os
import re
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.errors import (
    AppError,
    AuthForbiddenError,
    AuthInvalidCredentialsError,
    AuthRequiredError,
    UserAlreadyExistsError,
    UserBootstrapNotAllowedError,
)

_USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")


@dataclass
class AuthUser:
    id: int
    username: str
    role: str
    created_at: str
    display_name: str | None = None
    avatar_url: str | None = None
    feishu_union_id: str | None = None
    feishu_open_id: str | None = None
    accessible_workspaces: list[str] | None = None


class AuthService:
    def __init__(self, db_path: str, token_ttl_hours: int = 24 * 7) -> None:
        self._db_path = db_path
        self._token_ttl_hours = token_ttl_hours

    def init_db(self) -> None:
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS auth_tokens (
                    token TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS user_workspace_access (
                    user_id INTEGER NOT NULL,
                    workspace TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, workspace),
                    FOREIGN KEY (user_id) REFERENCES users(id)
                )
                """
            )
            self._ensure_user_extension_columns(conn)
            conn.commit()

    def requires_bootstrap(self) -> bool:
        with closing(sqlite3.connect(self._db_path)) as conn:
            row = conn.execute("SELECT COUNT(1) AS c FROM users").fetchone()
            return (row[0] if row else 0) == 0

    def bootstrap_superadmin(self, username: str, password: str) -> AuthUser:
        if not self.requires_bootstrap():
            raise UserBootstrapNotAllowedError()

        self._validate_username(username)
        self._validate_password(password)

        now = datetime.now(timezone.utc).isoformat()
        password_hash = self._hash_password(password)

        with closing(sqlite3.connect(self._db_path)) as conn:
            cursor = conn.execute(
                """
                INSERT INTO users (username, password_hash, role, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (username, password_hash, "superadmin", now),
            )
            conn.commit()
            user_id = cursor.lastrowid

        return AuthUser(
            id=user_id,
            username=username,
            role="superadmin",
            created_at=now,
        )

    def login(self, username: str, password: str) -> tuple[str, AuthUser]:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT id, username, password_hash, role, created_at,
                       display_name, avatar_url, feishu_union_id, feishu_open_id
                FROM users
                WHERE username = ?
                """,
                (username,),
            ).fetchone()

            if row is None or not self._verify_password(password, row["password_hash"]):
                raise AuthInvalidCredentialsError()

            token = secrets.token_urlsafe(48)
            now = datetime.now(timezone.utc)
            expires = now + timedelta(hours=self._token_ttl_hours)

            conn.execute(
                """
                INSERT INTO auth_tokens (token, user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (token, row["id"], now.isoformat(), expires.isoformat()),
            )
            conn.commit()

            user = AuthUser(
                id=row["id"],
                username=row["username"],
                role=row["role"],
                created_at=row["created_at"],
                display_name=row["display_name"],
                avatar_url=row["avatar_url"],
                feishu_union_id=row["feishu_union_id"],
                feishu_open_id=row["feishu_open_id"],
            )
            return token, user

    def login_by_feishu(
        self,
        *,
        union_id: str,
        open_id: str | None,
        name: str,
        avatar_url: str | None,
        default_workspace_names: list[str] | None = None,
    ) -> tuple[str, AuthUser, bool]:
        now = datetime.now(timezone.utc)
        now_text = now.isoformat()
        first_login = False
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            self._ensure_user_extension_columns(conn)
            row = conn.execute(
                """
                SELECT id, username, role, created_at,
                       display_name, avatar_url, feishu_union_id, feishu_open_id
                FROM users
                WHERE feishu_union_id = ?
                LIMIT 1
                """,
                (union_id,),
            ).fetchone()

            if row is None:
                first_login = True
                username = self._generate_unique_feishu_username(conn, union_id)
                password_hash = self._hash_password(secrets.token_urlsafe(32))
                cursor = conn.execute(
                    """
                    INSERT INTO users (
                        username, password_hash, role, created_at,
                        display_name, avatar_url, feishu_union_id, feishu_open_id
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        username,
                        password_hash,
                        "user",
                        now_text,
                        name,
                        avatar_url,
                        union_id,
                        open_id,
                    ),
                )
                user_id = int(cursor.lastrowid)
                workspace_rows = [
                    (user_id, workspace, now_text, now_text)
                    for workspace in sorted(set(default_workspace_names or []))
                    if workspace.strip()
                ]
                if workspace_rows:
                    # 首次飞书建号时按配置授予默认工作空间权限。
                    conn.executemany(
                        """
                        INSERT INTO user_workspace_access
                        (user_id, workspace, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        workspace_rows,
                    )
                user = AuthUser(
                    id=user_id,
                    username=username,
                    role="user",
                    created_at=now_text,
                    display_name=name,
                    avatar_url=avatar_url,
                    feishu_union_id=union_id,
                    feishu_open_id=open_id,
                )
            else:
                conn.execute(
                    """
                    UPDATE users
                    SET display_name = ?, avatar_url = ?, feishu_open_id = ?
                    WHERE id = ?
                    """,
                    (name, avatar_url, open_id, row["id"]),
                )
                user = AuthUser(
                    id=row["id"],
                    username=row["username"],
                    role=row["role"],
                    created_at=row["created_at"],
                    display_name=name,
                    avatar_url=avatar_url,
                    feishu_union_id=row["feishu_union_id"],
                    feishu_open_id=open_id,
                )

            token = secrets.token_urlsafe(48)
            expires = now + timedelta(hours=self._token_ttl_hours)
            conn.execute(
                """
                INSERT INTO auth_tokens (token, user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?)
                """,
                (token, user.id, now_text, expires.isoformat()),
            )
            conn.commit()
        return token, user, first_login

    def get_user_by_token(self, token: str) -> AuthUser:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT u.id, u.username, u.role, u.created_at, t.expires_at,
                       u.display_name, u.avatar_url, u.feishu_union_id, u.feishu_open_id
                FROM auth_tokens t
                JOIN users u ON u.id = t.user_id
                WHERE t.token = ?
                """,
                (token,),
            ).fetchone()

            if row is None:
                raise AuthRequiredError()

            if datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
                conn.execute("DELETE FROM auth_tokens WHERE token = ?", (token,))
                conn.commit()
                raise AuthRequiredError()

            return AuthUser(
                id=row["id"],
                username=row["username"],
                role=row["role"],
                created_at=row["created_at"],
                display_name=row["display_name"],
                avatar_url=row["avatar_url"],
                feishu_union_id=row["feishu_union_id"],
                feishu_open_id=row["feishu_open_id"],
            )

    def create_user(
        self,
        current_user: AuthUser,
        username: str,
        password: str,
        workspace_names: list[str] | None = None,
    ) -> AuthUser:
        if current_user.role != "superadmin":
            raise AuthForbiddenError()

        self._validate_username(username)
        self._validate_password(password)

        now = datetime.now(timezone.utc).isoformat()
        password_hash = self._hash_password(password)

        try:
            with closing(sqlite3.connect(self._db_path)) as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO users (username, password_hash, role, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (username, password_hash, "user", now),
                )
                user_id = int(cursor.lastrowid)
                workspace_rows = [
                    (user_id, workspace, now, now)
                    for workspace in sorted(set(workspace_names or []))
                ]
                if workspace_rows:
                    conn.executemany(
                        """
                        INSERT INTO user_workspace_access
                        (user_id, workspace, created_at, updated_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        workspace_rows,
                    )
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise UserAlreadyExistsError(username) from exc

        return AuthUser(
            id=user_id,
            username=username,
            role="user",
            created_at=now,
            accessible_workspaces=sorted(set(workspace_names or [])),
        )

    def list_users(self, current_user: AuthUser) -> list[AuthUser]:
        if current_user.role != "superadmin":
            raise AuthForbiddenError()

        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT id, username, role, created_at, display_name
                FROM users
                ORDER BY id ASC
                """
            ).fetchall()
            access_map = self._query_user_workspace_access_map(conn)
            return [
                AuthUser(
                    id=row["id"],
                    username=row["username"],
                    display_name=row["display_name"],
                    role=row["role"],
                    created_at=row["created_at"],
                    accessible_workspaces=access_map.get(row["id"], []),
                )
                for row in rows
            ]

    def set_user_workspace_access(
        self,
        current_user: AuthUser,
        user_id: int,
        workspace_names: list[str],
    ) -> list[str]:
        if current_user.role != "superadmin":
            raise AuthForbiddenError()

        normalized = sorted(set(workspace_names))
        now = datetime.now(timezone.utc).isoformat()
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            user_row = conn.execute(
                "SELECT id, role FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
            if user_row is None:
                raise AppError(
                    code="USER_NOT_FOUND",
                    message="User not found",
                    details={"user_id": user_id},
                    status_code=404,
                )
            if user_row["role"] == "superadmin":
                return []

            conn.execute(
                "DELETE FROM user_workspace_access WHERE user_id = ?",
                (user_id,),
            )
            rows = [(user_id, workspace, now, now) for workspace in normalized]
            if rows:
                conn.executemany(
                    """
                    INSERT INTO user_workspace_access
                    (user_id, workspace, created_at, updated_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    rows,
                )
            conn.commit()
        return normalized

    def get_accessible_workspaces(self, user: AuthUser) -> list[str]:
        if user.role == "superadmin":
            return []
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT workspace
                FROM user_workspace_access
                WHERE user_id = ?
                ORDER BY workspace ASC
                """,
                (user.id,),
            ).fetchall()
            return [str(row["workspace"]) for row in rows]

    def can_access_workspace(self, user: AuthUser, workspace: str) -> bool:
        if user.role == "superadmin":
            return True
        with closing(sqlite3.connect(self._db_path)) as conn:
            row = conn.execute(
                """
                SELECT 1
                FROM user_workspace_access
                WHERE user_id = ? AND workspace = ?
                LIMIT 1
                """,
                (user.id, workspace),
            ).fetchone()
            return row is not None

    def remove_workspace_access_for_all_users(self, workspace: str) -> None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                """
                DELETE FROM user_workspace_access
                WHERE workspace = ?
                """,
                (workspace,),
            )
            conn.commit()

    def _query_user_workspace_access_map(
        self, conn: sqlite3.Connection
    ) -> dict[int, list[str]]:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT user_id, workspace
            FROM user_workspace_access
            ORDER BY workspace ASC
            """
        ).fetchall()
        result: dict[int, list[str]] = {}
        for row in rows:
            user_id = int(row["user_id"])
            result.setdefault(user_id, []).append(str(row["workspace"]))
        return result

    def _validate_username(self, username: str) -> None:
        if not _USERNAME_PATTERN.match(username):
            raise AuthInvalidCredentialsError(
                message="用户名格式不合法（3-32位，字母数字_.-）"
            )

    def _validate_password(self, password: str) -> None:
        if len(password) < 6:
            raise AuthInvalidCredentialsError(message="密码长度至少 6 位")

    def _hash_password(self, password: str) -> str:
        salt = secrets.token_bytes(16)
        iterations = 120_000
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return "pbkdf2_sha256${}${}${}".format(
            iterations, salt.hex(), digest.hex()
        )

    def _verify_password(self, password: str, encoded: str) -> bool:
        try:
            algo, iter_str, salt_hex, digest_hex = encoded.split("$")
            if algo != "pbkdf2_sha256":
                return False
            iterations = int(iter_str)
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
        except Exception:
            return False

        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return hmac.compare_digest(actual, expected)

    def _ensure_user_extension_columns(self, conn: sqlite3.Connection) -> None:
        """
        兼容历史数据库结构的无损升级。

        说明：
        - 线上已有 users 表时，不做破坏性迁移，仅补充飞书相关扩展列。
        - 该升级逻辑可重复执行，确保应用每次启动都能自修复缺失列。
        """
        rows = conn.execute("PRAGMA table_info(users)").fetchall()
        existing = {str(row[1]) for row in rows}
        required_sql: dict[str, str] = {
            "display_name": "ALTER TABLE users ADD COLUMN display_name TEXT",
            "avatar_url": "ALTER TABLE users ADD COLUMN avatar_url TEXT",
            "feishu_union_id": "ALTER TABLE users ADD COLUMN feishu_union_id TEXT",
            "feishu_open_id": "ALTER TABLE users ADD COLUMN feishu_open_id TEXT",
        }
        for column, sql in required_sql.items():
            if column not in existing:
                conn.execute(sql)

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_users_feishu_union_id
            ON users(feishu_union_id)
            WHERE feishu_union_id IS NOT NULL
            """
        )

    def _generate_unique_feishu_username(
        self, conn: sqlite3.Connection, union_id: str
    ) -> str:
        # username 规则需要兼容现有校验（3~32，字母数字_.-）
        digest = hashlib.sha256(union_id.encode("utf-8")).hexdigest()[:16]
        base = f"feishu_{digest}"
        candidate = base
        suffix = 1
        while True:
            row = conn.execute(
                "SELECT 1 FROM users WHERE username = ? LIMIT 1",
                (candidate,),
            ).fetchone()
            if row is None:
                return candidate
            suffix += 1
            candidate = f"{base}_{suffix}"


auth_service = AuthService(str(settings.sqlite_db_path))
