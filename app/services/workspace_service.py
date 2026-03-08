from contextlib import closing, contextmanager
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from app.core.config import settings
from app.core.errors import (
    WorkspaceAlreadyExistsError,
    WorkspaceCredentialError,
    WorkspaceCreateError,
    WorkspaceDeleteError,
    InvalidWorkspaceError,
    WorkspaceNotFoundError,
)
from app.models.schemas import WorkspaceItem
from app.services.workspace_credential_service import (
    WorkspaceCredentialService,
    workspace_credential_service,
)
from app.services.workspace_git_service import (
    WorkspaceGitService,
    workspace_git_service,
)
from app.services.workspace_note_service import (
    WorkspaceNoteService,
    workspace_note_service,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkspacePullResult:
    workspace: str
    before_commit: str | None
    after_commit: str | None
    changed: bool
    summary: str
    pulled_at: str


@dataclass(frozen=True)
class WorkspaceDeleteResult:
    workspace: str
    deleted_at: str


@dataclass(frozen=True)
class WorkspaceMeta:
    workspace_id: str
    workspace_name: str
    mode: str
    owner_user_id: int | None
    relative_path: str
    created_at: str


class WorkspaceService:
    def __init__(
        self,
        root_dir: Path,
        credential_service: WorkspaceCredentialService | None = None,
        git_service: WorkspaceGitService | None = None,
        note_service: WorkspaceNoteService | None = None,
    ):
        self._root_dir = root_dir
        self._db_path = str(settings.sqlite_db_path)
        self._credential_service = credential_service
        self._git_service = git_service
        self._note_service = note_service
        self._personal_root_relative = "personal"
        # 主个人 Agent 的目录名，固定为 workspace。
        self._personal_main_agent_root_relative = "workspace"
        # 其他个人 Agent 的目录名前缀，例如 workspace-reviewer。
        self._personal_agent_root_prefix = "workspace-"
        self._personal_project_root_relative = "project"
        # 预留目录名：用于系统内部路径隔离，禁止作为业务工作空间名。
        self._reserved_workspace_names = {
            self._personal_root_relative,
            "personal-agent",
        }

    def init_db(self) -> None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            self._ensure_registry_schema(conn)
            self._backfill_legacy_workspaces(conn)
            conn.commit()

    def list_workspaces(self, allowed_workspaces: set[str] | None = None) -> list[WorkspaceItem]:
        meta_map = self._workspace_meta_map()
        credential_map = (
            self._credential_service.list_workspace_credentials()
            if self._credential_service is not None
            else {}
        )
        git_meta_map = self._git_service.get_sync_meta_map() if self._git_service else {}
        note_map = self._note_service.list_notes() if self._note_service else {}

        items: list[WorkspaceItem] = []
        for meta in meta_map.values():
            if allowed_workspaces is not None:
                if (
                    meta.workspace_id not in allowed_workspaces
                    and meta.workspace_name not in allowed_workspaces
                ):
                    continue
            target = self._resolve_workspace_path(meta)
            if not target.exists() or not target.is_dir():
                continue
            credential = credential_map.get(meta.workspace_id) or credential_map.get(
                meta.workspace_name
            )
            git_meta = git_meta_map.get(meta.workspace_id) or git_meta_map.get(
                meta.workspace_name
            )
            note = note_map.get(meta.workspace_id) or note_map.get(meta.workspace_name)
            items.append(
                WorkspaceItem(
                    workspace_id=meta.workspace_id,
                    name=meta.workspace_name,
                    path=str(target),
                    mode=meta.mode,
                    owner_user_id=meta.owner_user_id,
                    note=note.note if note else None,
                    git_url=credential.git_url if credential else None,
                    git_username=credential.git_username if credential else None,
                    has_git_pat=credential.has_git_pat if credential else False,
                    last_pull_at=git_meta.last_pull_at if git_meta else None,
                    last_pull_status=git_meta.last_pull_status if git_meta else None,
                )
            )
        return sorted(items, key=lambda x: x.name)

    def get_workspace_path(self, workspace_ref: str) -> Path:
        meta = self.get_workspace_meta(workspace_ref)
        target = self._resolve_workspace_path(meta)

        if not target.exists() or not target.is_dir():
            raise WorkspaceNotFoundError(workspace_ref)

        return target

    def get_workspace_meta(self, workspace_ref: str) -> WorkspaceMeta:
        self._validate_workspace_ref(workspace_ref)

        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            self._ensure_registry_schema(conn)
            meta = self._query_workspace_meta(conn, workspace_ref)
            if meta is None:
                self._register_legacy_workspace_if_exists(conn, workspace_ref)
                meta = self._query_workspace_meta(conn, workspace_ref)
            if meta is None:
                raise WorkspaceNotFoundError(workspace_ref)
            return meta

    def resolve_workspace_reference(
        self,
        workspace_ref: str,
        *,
        allowed_workspace_ids: set[str] | None = None,
    ) -> WorkspaceMeta:
        """
        将前端/接口传入的 workspace 标识解析为唯一 workspace。

        兼容输入：
        - workspace_id（推荐）
        - workspace_name（兼容历史；若重名则必须可唯一确定）
        """
        self._validate_workspace_ref(workspace_ref)
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            self._ensure_registry_schema(conn)
            by_id = conn.execute(
                """
                SELECT workspace_id, workspace_name, mode, owner_user_id, relative_path, created_at
                FROM workspace_registry
                WHERE workspace_id = ?
                LIMIT 1
                """,
                (workspace_ref,),
            ).fetchone()
            if by_id is not None:
                meta = WorkspaceMeta(
                    workspace_id=str(by_id["workspace_id"]),
                    workspace_name=str(by_id["workspace_name"]),
                    mode=str(by_id["mode"]),
                    owner_user_id=(
                        int(by_id["owner_user_id"])
                        if by_id["owner_user_id"] is not None
                        else None
                    ),
                    relative_path=str(by_id["relative_path"]),
                    created_at=str(by_id["created_at"]),
                )
                if (
                    allowed_workspace_ids is not None
                    and meta.workspace_id not in allowed_workspace_ids
                ):
                    raise WorkspaceNotFoundError(workspace_ref)
                return meta

            rows = conn.execute(
                """
                SELECT workspace_id, workspace_name, mode, owner_user_id, relative_path, created_at
                FROM workspace_registry
                WHERE workspace_name = ?
                ORDER BY created_at ASC
                """,
                (workspace_ref,),
            ).fetchall()
            candidates: list[WorkspaceMeta] = []
            for row in rows:
                meta = WorkspaceMeta(
                    workspace_id=str(row["workspace_id"]),
                    workspace_name=str(row["workspace_name"]),
                    mode=str(row["mode"]),
                    owner_user_id=(
                        int(row["owner_user_id"])
                        if row["owner_user_id"] is not None
                        else None
                    ),
                    relative_path=str(row["relative_path"]),
                    created_at=str(row["created_at"]),
                )
                if (
                    allowed_workspace_ids is None
                    or meta.workspace_id in allowed_workspace_ids
                ):
                    candidates.append(meta)
            if len(candidates) == 1:
                return candidates[0]
            if len(candidates) > 1:
                raise InvalidWorkspaceError(workspace_ref)
            raise WorkspaceNotFoundError(workspace_ref)

    def create_workspace(
        self,
        workspace: str,
        mode: str = "team",
        git_url: str | None = None,
        git_username: str | None = None,
        git_pat: str | None = None,
        creator_user_id: int = 0,
        owner_user_id: int | None = None,
    ) -> WorkspaceItem:
        normalized_mode = self._normalize_workspace_mode(mode)
        self._validate_workspace_ref(workspace)
        self._validate_reserved_workspace_name(workspace)

        resolved_owner_user_id = owner_user_id
        if normalized_mode == "personal":
            if resolved_owner_user_id is None:
                resolved_owner_user_id = creator_user_id
            if resolved_owner_user_id <= 0:
                raise WorkspaceCreateError(workspace, "invalid personal workspace owner")
        else:
            resolved_owner_user_id = None

        target = self._resolve_workspace_target(
            workspace_name=workspace,
            mode=normalized_mode,
            owner_user_id=resolved_owner_user_id,
        )
        with closing(sqlite3.connect(self._db_path)) as conn:
            self._ensure_registry_schema(conn)
            existing_meta = self._find_existing_workspace_for_create(
                conn=conn,
                workspace_name=workspace,
                mode=normalized_mode,
                owner_user_id=resolved_owner_user_id,
            )
            if existing_meta is not None:
                # 自愈场景：DB 里已存在，但目录被手动删除。
                existing_target = self._resolve_workspace_path(existing_meta)
                if not existing_target.exists():
                    existing_target.mkdir(parents=True, exist_ok=True)
                elif not existing_target.is_dir():
                    raise WorkspaceCreateError(
                        workspace,
                        "workspace path exists but is not a directory",
                    )
                return self._build_workspace_item_from_meta(existing_meta)

            if target.exists():
                raise WorkspaceAlreadyExistsError(workspace)
            workspace_id = str(uuid4())
            try:
                if git_url and git_url.strip():
                    self._clone_repo(
                        git_url=git_url.strip(),
                        target=target,
                        git_username=git_username,
                        git_pat=git_pat,
                    )
                else:
                    target.mkdir(parents=True, exist_ok=False)
            except WorkspaceCreateError:
                raise
            except Exception as exc:
                raise WorkspaceCreateError(workspace, str(exc)) from exc

            self._insert_workspace_registry(
                conn=conn,
                workspace_id=workspace_id,
                workspace_name=workspace,
                mode=normalized_mode,
                owner_user_id=resolved_owner_user_id,
                relative_path=target.relative_to(self._root_dir).as_posix(),
            )
            conn.commit()

        normalized_url = git_url.strip() if git_url and git_url.strip() else None
        normalized_user = git_username.strip() if git_username and git_username.strip() else None
        normalized_pat = git_pat.strip() if git_pat and git_pat.strip() else None

        if self._credential_service is not None:
            self._credential_service.upsert_workspace_credential(
                workspace=workspace_id,
                user_id=creator_user_id,
                git_url=normalized_url,
                git_username=normalized_user,
                git_pat=normalized_pat,
            )

        return WorkspaceItem(
            workspace_id=workspace_id,
            name=workspace,
            path=str(target),
            mode=normalized_mode,
            owner_user_id=resolved_owner_user_id,
            git_url=normalized_url,
            git_username=normalized_user,
            has_git_pat=bool(normalized_pat),
        )

    def _build_workspace_item_from_meta(self, meta: WorkspaceMeta) -> WorkspaceItem:
        target = self._resolve_workspace_path(meta)
        credential = None
        if self._credential_service is not None:
            credential_map = self._credential_service.list_workspace_credentials()
            credential = credential_map.get(meta.workspace_id) or credential_map.get(
                meta.workspace_name
            )
        return WorkspaceItem(
            workspace_id=meta.workspace_id,
            name=meta.workspace_name,
            path=str(target),
            mode=meta.mode,
            owner_user_id=meta.owner_user_id,
            git_url=credential.git_url if credential else None,
            git_username=credential.git_username if credential else None,
            has_git_pat=credential.has_git_pat if credential else False,
        )

    def _clone_repo(
        self,
        git_url: str,
        target: Path,
        git_username: str | None = None,
        git_pat: str | None = None,
    ) -> None:
        normalized_pat = self._normalize_optional(git_pat)
        normalized_user = self._normalize_optional(git_username) or "oauth2"
        self._validate_pat_support(git_url=git_url, git_pat=normalized_pat)
        command = [
            "git",
            "-c",
            "credential.helper=",
            "clone",
            "--depth",
            "1",
        ]
        command.extend([git_url, str(target)])
        try:
            with self._git_process_env(
                git_username=normalized_user,
                git_pat=normalized_pat,
            ) as env:
                subprocess.run(
                    command,
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
        except FileNotFoundError as exc:
            raise WorkspaceCreateError(
                target.name,
                "git command not found",
            ) from exc
        except subprocess.CalledProcessError as exc:
            reason = (exc.stderr or exc.stdout or "git clone failed").strip()
            raise WorkspaceCreateError(target.name, reason) from exc

        # 决策背景（2026-02 热修）：
        # 线上出现“父仓记录的子模块 commit 在远端不可达（unadvertised object）”问题，
        # 该问题会导致工作空间创建整体失败。为保证主流程可用，先采用兼容策略：
        # 1) 主仓 clone 成功即可保留工作空间；
        # 2) 子模块不再强依赖父仓锁定 commit，改为按远端默认分支拉取；
        # 3) 单个子模块失败仅记录告警，不阻断其他子模块和主仓使用。
        # 取舍：该策略牺牲了子模块版本的严格可复现性，后续可再引入“严格模式”开关。
        self._run_git(
            target,
            ["submodule", "sync", "--recursive"],
            git_username=normalized_user,
            git_pat=normalized_pat,
        )
        _, _, warnings = self._update_submodules_best_effort(
            repo_dir=target,
            git_username=normalized_user,
            git_pat=normalized_pat,
        )
        if warnings:
            logger.warning(
                "Workspace clone submodules partial failed: workspace=%s warnings=%s",
                target.name,
                warnings,
            )

    def _validate_workspace_ref(self, workspace: str) -> None:
        if not workspace or "/" in workspace or "\\" in workspace or ".." in workspace:
            raise InvalidWorkspaceError(workspace)

    def _validate_reserved_workspace_name(self, workspace: str) -> None:
        """
        保留名校验（大小写不敏感）。

        约束目的：
        - 避免团队/个人工作空间名与系统预留目录冲突。
        - 降低路径语义歧义与后续迁移风险。
        """
        normalized = workspace.strip().lower()
        if normalized in self._reserved_workspace_names:
            raise WorkspaceCreateError(workspace, "workspace name is reserved")

    def _normalize_workspace_mode(self, mode: str | None) -> str:
        normalized = (mode or "team").strip().lower()
        if normalized not in {"team", "personal"}:
            raise WorkspaceCreateError("workspace", f"unsupported workspace mode: {mode}")
        return normalized

    def _resolve_workspace_target(
        self,
        *,
        workspace_name: str,
        mode: str,
        owner_user_id: int | None,
    ) -> Path:
        if mode == "team":
            target = (self._root_dir / workspace_name).resolve()
        else:
            if owner_user_id is None:
                raise WorkspaceCreateError(
                    workspace_name,
                    "missing owner for personal workspace",
                )
            target = (
                self.get_personal_main_agent_workspace_root(owner_user_id)
                / self._personal_project_root_relative
                / workspace_name
            ).resolve()

        if self._root_dir not in target.parents and target != self._root_dir:
            raise InvalidWorkspaceError(workspace_name)
        return target

    def get_personal_user_root(self, user_id: int) -> Path:
        """
        返回用户个人空间根目录：workspaces/personal/<user_id>。

        该目录是“个人空间沙箱边界”的上限，后续多 Agent 目录都挂在此处。
        """
        if user_id <= 0:
            raise WorkspaceCreateError("personal", "invalid personal user id")
        target = (self._root_dir / self._personal_root_relative / str(user_id)).resolve()
        if self._root_dir not in target.parents and target != self._root_dir:
            raise InvalidWorkspaceError(str(user_id))
        return target

    def get_personal_agent_workspace_root(self, user_id: int, agent_name: str | None) -> Path:
        """
        返回某个个人 Agent 的工作根目录。

        规则：
        - 主 Agent（agent_name 为空）使用固定目录 `workspace`
        - 其他 Agent 使用目录 `workspace-<agent_name>`
        """
        user_root = self.get_personal_user_root(user_id)
        slug = self._to_personal_agent_dir_slug(agent_name)
        target = (user_root / slug).resolve()
        if user_root not in target.parents and target != user_root:
            raise InvalidWorkspaceError(slug)
        return target

    def get_personal_main_agent_workspace_root(self, user_id: int) -> Path:
        """
        返回主个人 Agent 根目录：workspaces/personal/<user_id>/workspace。
        """
        return self.get_personal_agent_workspace_root(user_id, None)

    def _to_personal_agent_dir_slug(self, agent_name: str | None) -> str:
        normalized = (agent_name or "").strip().lower()
        if not normalized:
            return self._personal_main_agent_root_relative
        if normalized == self._personal_main_agent_root_relative:
            return self._personal_main_agent_root_relative
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,31}", normalized):
            raise WorkspaceCreateError(
                "agent",
                "invalid agent name, only [a-z0-9-], length 1..32",
            )
        return f"{self._personal_agent_root_prefix}{normalized}"

    def _resolve_workspace_path(self, meta: WorkspaceMeta) -> Path:
        target = (self._root_dir / meta.relative_path).resolve()
        if self._root_dir not in target.parents and target != self._root_dir:
            raise InvalidWorkspaceError(meta.workspace_name)
        return target

    def _ensure_registry_schema(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type='table' AND name='workspace_registry'
            LIMIT 1
            """
        ).fetchone()

        if row is None:
            self._create_registry_table(conn)
            return

        columns = {
            str(item[1])
            for item in conn.execute("PRAGMA table_info(workspace_registry)").fetchall()
        }
        if "workspace_id" in columns and "workspace_name" in columns:
            self._ensure_registry_indexes(conn)
            return

        # 历史 schema 迁移：workspace(主键) -> workspace_id/workspace_name
        legacy_rows = conn.execute(
            """
            SELECT workspace, mode, owner_user_id, relative_path, created_at
            FROM workspace_registry
            """
        ).fetchall()

        conn.execute("DROP TABLE workspace_registry")
        self._create_registry_table(conn)

        for legacy in legacy_rows:
            workspace_name = str(legacy[0])
            mode = str(legacy[1] or "team")
            owner_user_id = int(legacy[2]) if legacy[2] is not None else None
            relative_path = str(legacy[3])
            created_at = str(legacy[4] or datetime.now(timezone.utc).isoformat())
            conn.execute(
                """
                INSERT INTO workspace_registry
                (workspace_id, workspace_name, mode, owner_user_id, relative_path, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    workspace_name,
                    mode,
                    owner_user_id,
                    relative_path,
                    created_at,
                ),
            )

        self._ensure_registry_indexes(conn)

    def _create_registry_table(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS workspace_registry (
                workspace_id TEXT PRIMARY KEY,
                workspace_name TEXT NOT NULL,
                mode TEXT NOT NULL,
                owner_user_id INTEGER,
                relative_path TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self._ensure_registry_indexes(conn)

    def _ensure_registry_indexes(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_registry_team_name
            ON workspace_registry(workspace_name)
            WHERE mode='team'
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_workspace_registry_personal_owner_name
            ON workspace_registry(owner_user_id, workspace_name)
            WHERE mode='personal'
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_workspace_registry_name
            ON workspace_registry(workspace_name)
            """
        )

    def _ensure_workspace_uniqueness(
        self,
        *,
        conn: sqlite3.Connection,
        workspace_name: str,
        mode: str,
        owner_user_id: int | None,
    ) -> None:
        conn.row_factory = sqlite3.Row
        if mode == "team":
            row = conn.execute(
                """
                SELECT workspace_id
                FROM workspace_registry
                WHERE mode='team' AND workspace_name=?
                LIMIT 1
                """,
                (workspace_name,),
            ).fetchone()
            if row is not None:
                raise WorkspaceAlreadyExistsError(workspace_name)
            return

        if owner_user_id is None:
            raise WorkspaceCreateError(workspace_name, "missing owner for personal workspace")

        row = conn.execute(
            """
            SELECT workspace_id
            FROM workspace_registry
            WHERE mode='personal' AND owner_user_id=? AND workspace_name=?
            LIMIT 1
            """,
            (owner_user_id, workspace_name),
        ).fetchone()
        if row is not None:
            raise WorkspaceAlreadyExistsError(workspace_name)

    def _find_existing_workspace_for_create(
        self,
        *,
        conn: sqlite3.Connection,
        workspace_name: str,
        mode: str,
        owner_user_id: int | None,
    ) -> WorkspaceMeta | None:
        conn.row_factory = sqlite3.Row
        if mode == "team":
            row = conn.execute(
                """
                SELECT workspace_id, workspace_name, mode, owner_user_id, relative_path, created_at
                FROM workspace_registry
                WHERE mode='team' AND workspace_name=?
                LIMIT 1
                """,
                (workspace_name,),
            ).fetchone()
        else:
            if owner_user_id is None:
                return None
            row = conn.execute(
                """
                SELECT workspace_id, workspace_name, mode, owner_user_id, relative_path, created_at
                FROM workspace_registry
                WHERE mode='personal' AND owner_user_id=? AND workspace_name=?
                LIMIT 1
                """,
                (owner_user_id, workspace_name),
            ).fetchone()
        if row is None:
            return None
        return WorkspaceMeta(
            workspace_id=str(row["workspace_id"]),
            workspace_name=str(row["workspace_name"]),
            mode=str(row["mode"]),
            owner_user_id=(
                int(row["owner_user_id"]) if row["owner_user_id"] is not None else None
            ),
            relative_path=str(row["relative_path"]),
            created_at=str(row["created_at"]),
        )

    def _insert_workspace_registry(
        self,
        *,
        conn: sqlite3.Connection,
        workspace_id: str,
        workspace_name: str,
        mode: str,
        owner_user_id: int | None,
        relative_path: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO workspace_registry
            (workspace_id, workspace_name, mode, owner_user_id, relative_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (workspace_id, workspace_name, mode, owner_user_id, relative_path, now),
        )

    def _delete_workspace_registry(self, workspace_id: str) -> None:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.execute(
                "DELETE FROM workspace_registry WHERE workspace_id = ?",
                (workspace_id,),
            )
            conn.commit()

    def _workspace_meta_map(self) -> dict[str, WorkspaceMeta]:
        with closing(sqlite3.connect(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            self._ensure_registry_schema(conn)
            rows = conn.execute(
                """
                SELECT workspace_id, workspace_name, mode, owner_user_id, relative_path, created_at
                FROM workspace_registry
                ORDER BY workspace_name ASC
                """
            ).fetchall()
            result: dict[str, WorkspaceMeta] = {}
            for row in rows:
                meta = WorkspaceMeta(
                    workspace_id=str(row["workspace_id"]),
                    workspace_name=str(row["workspace_name"]),
                    mode=str(row["mode"]),
                    owner_user_id=(
                        int(row["owner_user_id"])
                        if row["owner_user_id"] is not None
                        else None
                    ),
                    relative_path=str(row["relative_path"]),
                    created_at=str(row["created_at"]),
                )
                result[meta.workspace_id] = meta
            return result

    def _query_workspace_meta(
        self,
        conn: sqlite3.Connection,
        workspace_ref: str,
    ) -> WorkspaceMeta | None:
        conn.row_factory = sqlite3.Row
        by_id = conn.execute(
            """
            SELECT workspace_id, workspace_name, mode, owner_user_id, relative_path, created_at
            FROM workspace_registry
            WHERE workspace_id = ?
            LIMIT 1
            """,
            (workspace_ref,),
        ).fetchone()
        if by_id is not None:
            return WorkspaceMeta(
                workspace_id=str(by_id["workspace_id"]),
                workspace_name=str(by_id["workspace_name"]),
                mode=str(by_id["mode"]),
                owner_user_id=(
                    int(by_id["owner_user_id"]) if by_id["owner_user_id"] is not None else None
                ),
                relative_path=str(by_id["relative_path"]),
                created_at=str(by_id["created_at"]),
            )

        rows = conn.execute(
            """
            SELECT workspace_id, workspace_name, mode, owner_user_id, relative_path, created_at
            FROM workspace_registry
            WHERE workspace_name = ?
            ORDER BY created_at ASC
            """,
            (workspace_ref,),
        ).fetchall()
        if len(rows) == 1:
            row = rows[0]
            return WorkspaceMeta(
                workspace_id=str(row["workspace_id"]),
                workspace_name=str(row["workspace_name"]),
                mode=str(row["mode"]),
                owner_user_id=(
                    int(row["owner_user_id"]) if row["owner_user_id"] is not None else None
                ),
                relative_path=str(row["relative_path"]),
                created_at=str(row["created_at"]),
            )
        if len(rows) > 1:
            # name 出现歧义时必须使用 workspace_id。
            raise InvalidWorkspaceError(workspace_ref)
        return None

    def _backfill_legacy_workspaces(self, conn: sqlite3.Connection) -> None:
        existing = {
            str(row[0])
            for row in conn.execute(
                "SELECT workspace_name FROM workspace_registry"
            ).fetchall()
        }
        now = datetime.now(timezone.utc).isoformat()
        for item in self._root_dir.iterdir():
            if not item.is_dir():
                continue
            if item.name == self._personal_root_relative:
                continue
            if item.name in existing:
                continue
            conn.execute(
                """
                INSERT INTO workspace_registry
                (workspace_id, workspace_name, mode, owner_user_id, relative_path, created_at)
                VALUES (?, ?, 'team', NULL, ?, ?)
                """,
                (
                    str(uuid4()),
                    item.name,
                    item.relative_to(self._root_dir).as_posix(),
                    now,
                ),
            )

    def _register_legacy_workspace_if_exists(
        self,
        conn: sqlite3.Connection,
        workspace_ref: str,
    ) -> None:
        legacy_path = (self._root_dir / workspace_ref).resolve()
        if not legacy_path.exists() or not legacy_path.is_dir():
            return
        if self._root_dir not in legacy_path.parents and legacy_path != self._root_dir:
            return
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO workspace_registry
            (workspace_id, workspace_name, mode, owner_user_id, relative_path, created_at)
            VALUES (?, ?, 'team', NULL, ?, ?)
            """,
            (
                str(uuid4()),
                workspace_ref,
                legacy_path.relative_to(self._root_dir).as_posix(),
                now,
            ),
        )
        conn.commit()

    def pull_workspace(self, workspace: str) -> WorkspacePullResult:
        meta = self.get_workspace_meta(workspace)
        target = self._resolve_workspace_path(meta)
        git_dir = (target / ".git").resolve()
        if not git_dir.exists():
            raise WorkspaceCreateError(workspace, "workspace is not a git repository")

        detail = None
        if self._credential_service is not None:
            detail = self._credential_service.get_workspace_credential_detail(meta.workspace_id)
            if detail is None:
                detail = self._credential_service.get_workspace_credential_detail(
                    meta.workspace_name
                )
        username = detail.git_username if detail else None
        pat = detail.git_pat if detail else None
        if detail and detail.git_url:
            self._validate_pat_support(git_url=detail.git_url, git_pat=pat)

        try:
            before_commit = self._run_git(
                target,
                ["rev-parse", "HEAD"],
                git_username=username,
                git_pat=pat,
            )
            self._run_git(
                target,
                ["pull", "--ff-only"],
                git_username=username,
                git_pat=pat,
            )
            self._run_git(
                target,
                ["submodule", "sync", "--recursive"],
                git_username=username,
                git_pat=pat,
            )
            # 与创建流程保持一致：
            # pull 时对子模块采用“尽力而为”策略，避免单个子模块异常导致整仓 pull 失败。
            submodule_success_count, submodule_fail_count, submodule_warnings = (
                self._update_submodules_best_effort(
                    repo_dir=target,
                    git_username=username,
                    git_pat=pat,
                )
            )
            after_commit = self._run_git(
                target,
                ["rev-parse", "HEAD"],
                git_username=username,
                git_pat=pat,
            )
            changed = before_commit != after_commit
            summary = "代码已更新" if changed else "已是最新，无需更新"
            if submodule_success_count or submodule_fail_count:
                summary = (
                    f"{summary}（子模块成功 {submodule_success_count}，"
                    f"失败 {submodule_fail_count}）"
                )
            if submodule_warnings:
                logger.warning(
                    "Workspace pull submodules partial failed: workspace=%s warnings=%s",
                    meta.workspace_name,
                    submodule_warnings,
                )
            pulled_at = datetime.now(timezone.utc).isoformat()
            if self._git_service is not None:
                self._git_service.set_pull_result(
                    workspace=meta.workspace_id,
                    status="success",
                    message=summary,
                    pulled_at=pulled_at,
                )
            return WorkspacePullResult(
                workspace=meta.workspace_name,
                before_commit=before_commit,
                after_commit=after_commit,
                changed=changed,
                summary=summary,
                pulled_at=pulled_at,
            )
        except WorkspaceCreateError as exc:
            if self._git_service is not None:
                self._git_service.set_pull_result(
                    workspace=meta.workspace_id,
                    status="failed",
                    message=str(exc.details.get("reason") if exc.details else str(exc)),
                )
            raise

    def delete_workspace(self, workspace: str) -> WorkspaceDeleteResult:
        meta = self.get_workspace_meta(workspace)
        target = self._resolve_workspace_path(meta)
        if target == self._root_dir:
            raise WorkspaceDeleteError(workspace, "refuse to delete workspace root")

        try:
            shutil.rmtree(target)
        except FileNotFoundError as exc:
            raise WorkspaceNotFoundError(workspace) from exc
        except OSError as exc:
            raise WorkspaceDeleteError(workspace, str(exc)) from exc

        self._delete_workspace_registry(meta.workspace_id)
        return WorkspaceDeleteResult(
            workspace=meta.workspace_name,
            deleted_at=datetime.now(timezone.utc).isoformat(),
        )

    def is_personal_workspace_owned_by(self, workspace: str, user_id: int) -> bool:
        meta = self.get_workspace_meta(workspace)
        return meta.mode == "personal" and meta.owner_user_id == user_id

    def is_team_workspace(self, workspace: str) -> bool:
        meta = self.get_workspace_meta(workspace)
        return meta.mode == "team"

    def _run_git(
        self,
        cwd: Path,
        args: list[str],
        git_username: str | None = None,
        git_pat: str | None = None,
    ) -> str:
        command = ["git", "-c", "credential.helper="]
        normalized_pat = self._normalize_optional(git_pat)
        normalized_user = self._normalize_optional(git_username) or "oauth2"
        command.extend(args)
        try:
            with self._git_process_env(
                git_username=normalized_user,
                git_pat=normalized_pat,
            ) as env:
                completed = subprocess.run(
                    command,
                    cwd=str(cwd),
                    check=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                )
            return (completed.stdout or "").strip()
        except FileNotFoundError as exc:
            raise WorkspaceCreateError(cwd.name, "git command not found") from exc
        except subprocess.CalledProcessError as exc:
            reason = (exc.stderr or exc.stdout or "git command failed").strip()
            raise WorkspaceCreateError(cwd.name, reason) from exc

    def _normalize_optional(self, value: str | None) -> str | None:
        if value is None:
            return None
        trimmed = value.strip()
        return trimmed if trimmed else None

    def _validate_pat_support(self, git_url: str, git_pat: str | None) -> None:
        if not git_pat:
            return
        parsed = urlparse(git_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise WorkspaceCredentialError(
                "PAT authentication only supports https repository URL"
            )

    def _update_submodules_best_effort(
        self,
        repo_dir: Path,
        git_username: str | None,
        git_pat: str | None,
    ) -> tuple[int, int, list[str]]:
        # 设计意图：
        # 逐个子模块更新并汇总结果，失败继续，最后返回成功/失败统计与告警明细。
        # 这样上层可以把“部分成功”反馈给用户，而不是直接中断整个请求。
        paths = self._list_submodule_paths(repo_dir=repo_dir)
        success_count = 0
        fail_count = 0
        warnings: list[str] = []

        for path in paths:
            try:
                self._run_git(
                    repo_dir,
                    [
                        "submodule",
                        "update",
                        "--init",
                        "--depth",
                        "1",
                        "--remote",
                        path,
                    ],
                    git_username=git_username,
                    git_pat=git_pat,
                )
                success_count += 1
            except WorkspaceCreateError as exc:
                fail_count += 1
                reason = (
                    str(exc.details.get("reason"))
                    if isinstance(exc.details, dict)
                    and exc.details.get("reason") is not None
                    else str(exc)
                )
                warnings.append(f"{path}: {reason}")
                continue

            nested_repo = (repo_dir / path).resolve()
            # 递归处理嵌套子模块，确保多级 submodule 也遵循相同容错策略。
            (
                nested_success_count,
                nested_fail_count,
                nested_warnings,
            ) = self._update_submodules_best_effort(
                repo_dir=nested_repo,
                git_username=git_username,
                git_pat=git_pat,
            )
            success_count += nested_success_count
            fail_count += nested_fail_count
            warnings.extend([f"{path}/{item}" for item in nested_warnings])

        return success_count, fail_count, warnings

    def _list_submodule_paths(self, repo_dir: Path) -> list[str]:
        gitmodules_file = (repo_dir / ".gitmodules").resolve()
        if not gitmodules_file.exists():
            return []
        output = self._run_git(
            repo_dir,
            ["config", "-f", ".gitmodules", "--get-regexp", "path"],
        )
        paths: list[str] = []
        for line in output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split(maxsplit=1)
            if len(parts) != 2:
                continue
            path = parts[1].strip()
            if path:
                paths.append(path)
        return paths

    @contextmanager
    def _git_process_env(
        self,
        git_username: str,
        git_pat: str | None,
    ):
        """
        构建一次 git 命令执行环境：
        1) 禁止终端交互，避免卡住 API 请求。
        2) 通过 GIT_ASKPASS 注入工作空间凭据（若存在 PAT）。
        3) 不依赖系统凭据管理器，确保行为可控。
        """
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GCM_INTERACTIVE"] = "Never"

        if not git_pat:
            yield env
            return

        script_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
                suffix=".sh",
            ) as script:
                script.write("#!/bin/sh\n")
                script.write('prompt="$1"\n')
                script.write('case "$prompt" in\n')
                script.write('  *sername*|*Username*) echo "$JEWEI_GIT_USERNAME" ;;\n')
                script.write('  *assword*|*Password*) echo "$JEWEI_GIT_PAT" ;;\n')
                script.write('  *) echo "$JEWEI_GIT_PAT" ;;\n')
                script.write("esac\n")
                script_path = script.name

            os.chmod(script_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

            env["GIT_ASKPASS"] = script_path
            env["SSH_ASKPASS"] = script_path
            env["JEWEI_GIT_USERNAME"] = git_username
            env["JEWEI_GIT_PAT"] = git_pat

            yield env
        finally:
            if script_path and os.path.exists(script_path):
                try:
                    os.remove(script_path)
                except OSError:
                    pass


workspace_service = WorkspaceService(
    settings.workspace_root_dir,
    credential_service=workspace_credential_service,
    git_service=workspace_git_service,
    note_service=workspace_note_service,
)
