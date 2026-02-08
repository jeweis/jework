from contextlib import contextmanager
import logging
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
                    workspace,
                    submodule_warnings,
                )
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
