from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import datetime, timezone

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


class WorkspaceService:
    def __init__(
        self,
        root_dir: Path,
        credential_service: WorkspaceCredentialService | None = None,
        git_service: WorkspaceGitService | None = None,
        note_service: WorkspaceNoteService | None = None,
    ):
        self._root_dir = root_dir
        self._credential_service = credential_service
        self._git_service = git_service
        self._note_service = note_service

    def list_workspaces(self, allowed_workspaces: set[str] | None = None) -> list[WorkspaceItem]:
        credential_map = (
            self._credential_service.list_workspace_credentials()
            if self._credential_service is not None
            else {}
        )
        git_meta_map = self._git_service.get_sync_meta_map() if self._git_service else {}
        note_map = self._note_service.list_notes() if self._note_service else {}
        items = []
        for item in self._root_dir.iterdir():
            if not item.is_dir():
                continue
            if allowed_workspaces is not None and item.name not in allowed_workspaces:
                continue
            meta = credential_map.get(item.name)
            git_meta = git_meta_map.get(item.name)
            note = note_map.get(item.name)
            items.append(
                WorkspaceItem(
                    name=item.name,
                    path=str(item.resolve()),
                    note=note.note if note else None,
                    git_url=meta.git_url if meta else None,
                    git_username=meta.git_username if meta else None,
                    has_git_pat=meta.has_git_pat if meta else False,
                    last_pull_at=git_meta.last_pull_at if git_meta else None,
                    last_pull_status=git_meta.last_pull_status if git_meta else None,
                )
            )
        return sorted(items, key=lambda x: x.name)

    def get_workspace_path(self, workspace: str) -> Path:
        self._validate_workspace_name(workspace)

        target = (self._root_dir / workspace).resolve()

        if self._root_dir not in target.parents and target != self._root_dir:
            raise InvalidWorkspaceError(workspace)

        if not target.exists() or not target.is_dir():
            raise WorkspaceNotFoundError(workspace)

        return target

    def create_workspace(
        self,
        workspace: str,
        git_url: str | None = None,
        git_username: str | None = None,
        git_pat: str | None = None,
        user_id: int = 0,
    ) -> WorkspaceItem:
        self._validate_workspace_name(workspace)
        target = (self._root_dir / workspace).resolve()

        if target.exists():
            raise WorkspaceAlreadyExistsError(workspace)

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

        normalized_url = git_url.strip() if git_url and git_url.strip() else None
        normalized_user = git_username.strip() if git_username and git_username.strip() else None
        normalized_pat = git_pat.strip() if git_pat and git_pat.strip() else None
        if self._credential_service is not None:
            self._credential_service.upsert_workspace_credential(
                workspace=workspace,
                user_id=user_id,
                git_url=normalized_url,
                git_username=normalized_user,
                git_pat=normalized_pat,
            )

        return WorkspaceItem(
            name=workspace,
            path=str(target),
            git_url=normalized_url,
            git_username=normalized_user,
            has_git_pat=bool(normalized_pat),
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
            "--recurse-submodules",
            "--shallow-submodules",
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

    def _validate_workspace_name(self, workspace: str) -> None:
        if not workspace or "/" in workspace or "\\" in workspace or ".." in workspace:
            raise InvalidWorkspaceError(workspace)

    def pull_workspace(self, workspace: str) -> WorkspacePullResult:
        target = self.get_workspace_path(workspace)
        git_dir = (target / ".git").resolve()
        if not git_dir.exists():
            raise WorkspaceCreateError(workspace, "workspace is not a git repository")

        detail = (
            self._credential_service.get_workspace_credential_detail(workspace)
            if self._credential_service is not None
            else None
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
            self._run_git(
                target,
                ["submodule", "update", "--init", "--recursive", "--remote"],
                git_username=username,
                git_pat=pat,
            )
            after_commit = self._run_git(
                target,
                ["rev-parse", "HEAD"],
                git_username=username,
                git_pat=pat,
            )
            changed = before_commit != after_commit
            summary = "代码已更新" if changed else "已是最新，无需更新"
            pulled_at = datetime.now(timezone.utc).isoformat()
            if self._git_service is not None:
                self._git_service.set_pull_result(
                    workspace=workspace,
                    status="success",
                    message=summary,
                    pulled_at=pulled_at,
                )
            return WorkspacePullResult(
                workspace=workspace,
                before_commit=before_commit,
                after_commit=after_commit,
                changed=changed,
                summary=summary,
                pulled_at=pulled_at,
            )
        except WorkspaceCreateError as exc:
            if self._git_service is not None:
                self._git_service.set_pull_result(
                    workspace=workspace,
                    status="failed",
                    message=str(exc.details.get("reason") if exc.details else str(exc)),
                )
            raise

    def delete_workspace(self, workspace: str) -> WorkspaceDeleteResult:
        target = self.get_workspace_path(workspace)
        if target == self._root_dir:
            raise WorkspaceDeleteError(workspace, "refuse to delete workspace root")

        try:
            shutil.rmtree(target)
        except FileNotFoundError as exc:
            raise WorkspaceNotFoundError(workspace) from exc
        except OSError as exc:
            raise WorkspaceDeleteError(workspace, str(exc)) from exc

        return WorkspaceDeleteResult(
            workspace=workspace,
            deleted_at=datetime.now(timezone.utc).isoformat(),
        )

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
