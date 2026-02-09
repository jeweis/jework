from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from app.core.errors import AppError, WorkspaceCreateError
from app.services.workspace_credential_service import (
    WorkspaceCredentialService,
    workspace_credential_service,
)


@dataclass(frozen=True)
class BranchRepoStatus:
    repo_key: str
    display_path: str
    current_branch: str
    is_dirty: bool
    dirty_file_count: int


@dataclass(frozen=True)
class BranchCheckoutResult:
    repo_key: str
    before_branch: str
    after_branch: str
    discarded_changes: bool
    summary: str


class WorkspaceBranchService:
    """工作空间分支管理服务。

    设计目标：
    1) 仅负责“列仓库/查分支/切分支”领域逻辑。
    2) 保持默认安全策略：有未提交改动时阻止切换。
    3) 允许显式传入 discard_changes=True 时放弃改动后切换。
    """
    ROOT_REPO_KEY = '__root__'

    def __init__(
        self,
        credential_service: WorkspaceCredentialService | None = None,
    ) -> None:
        self._credential_service = credential_service

    def list_repos(self, workspace: str, workspace_path: Path) -> list[BranchRepoStatus]:
        repo_items = self._collect_repos(workspace_path)
        username, pat = self._workspace_git_credential(workspace)

        result: list[BranchRepoStatus] = []
        for repo_key, repo_path in repo_items:
            current_branch = self._current_branch(
                repo_dir=repo_path,
                git_username=username,
                git_pat=pat,
            )
            dirty_files = self._dirty_files(
                repo_dir=repo_path,
                git_username=username,
                git_pat=pat,
            )
            result.append(
                BranchRepoStatus(
                    repo_key=repo_key,
                    display_path='主仓(.)'
                    if repo_key == self.ROOT_REPO_KEY
                    else repo_key,
                    current_branch=current_branch,
                    is_dirty=bool(dirty_files),
                    dirty_file_count=len(dirty_files),
                )
            )
        return result

    def list_branches(
        self,
        workspace: str,
        workspace_path: Path,
        repo_key: str,
    ) -> tuple[str, list[str]]:
        repo_dir = self._resolve_repo_dir(workspace_path=workspace_path, repo_key=repo_key)
        username, pat = self._workspace_git_credential(workspace)

        # 先 fetch，保证用户可看到最新远端分支。
        self._run_git(
            repo_dir,
            ['fetch', '--all', '--prune'],
            git_username=username,
            git_pat=pat,
        )

        current_branch = self._current_branch(
            repo_dir=repo_dir,
            git_username=username,
            git_pat=pat,
        )
        local_refs = self._run_git(
            repo_dir,
            ['for-each-ref', '--format=%(refname:short)', 'refs/heads'],
            git_username=username,
            git_pat=pat,
        )
        remote_refs = self._run_git(
            repo_dir,
            ['for-each-ref', '--format=%(refname:short)', 'refs/remotes/origin'],
            git_username=username,
            git_pat=pat,
        )

        branches = {
            line.strip()
            for line in local_refs.splitlines()
            if line.strip()
        }
        for line in remote_refs.splitlines():
            value = line.strip()
            if not value or value == 'origin/HEAD' or not value.startswith('origin/'):
                continue
            branches.add(value.replace('origin/', '', 1))

        return current_branch, sorted(branches)

    def checkout_branch(
        self,
        workspace: str,
        workspace_path: Path,
        repo_key: str,
        branch: str,
        discard_changes: bool,
    ) -> BranchCheckoutResult:
        repo_dir = self._resolve_repo_dir(workspace_path=workspace_path, repo_key=repo_key)
        username, pat = self._workspace_git_credential(workspace)

        before_branch = self._current_branch(
            repo_dir=repo_dir,
            git_username=username,
            git_pat=pat,
        )
        dirty_files = self._dirty_files(
            repo_dir=repo_dir,
            git_username=username,
            git_pat=pat,
        )
        has_dirty = bool(dirty_files)

        if has_dirty and not discard_changes:
            raise AppError(
                code='WORKTREE_DIRTY',
                message='仓库存在未提交改动，默认不允许切换分支',
                details={
                    'repo_key': repo_key,
                    'dirty_file_count': len(dirty_files),
                    'dirty_files': dirty_files[:20],
                },
                status_code=409,
            )

        if has_dirty and discard_changes:
            # 用户显式确认后才执行破坏性清理。
            self._run_git(
                repo_dir,
                ['reset', '--hard', 'HEAD'],
                git_username=username,
                git_pat=pat,
            )
            self._run_git(
                repo_dir,
                ['clean', '-fd'],
                git_username=username,
                git_pat=pat,
            )

        normalized_branch = branch.strip()
        if not normalized_branch:
            raise AppError(
                code='INVALID_BRANCH',
                message='目标分支不能为空',
                status_code=400,
            )

        self._run_git(
            repo_dir,
            ['fetch', '--all', '--prune'],
            git_username=username,
            git_pat=pat,
        )

        local_exists = self._has_ref(
            repo_dir=repo_dir,
            ref=f'refs/heads/{normalized_branch}',
            git_username=username,
            git_pat=pat,
        )
        remote_exists = self._has_ref(
            repo_dir=repo_dir,
            ref=f'refs/remotes/origin/{normalized_branch}',
            git_username=username,
            git_pat=pat,
        )

        if local_exists:
            self._run_git(
                repo_dir,
                ['checkout', normalized_branch],
                git_username=username,
                git_pat=pat,
            )
        elif remote_exists:
            self._run_git(
                repo_dir,
                ['checkout', '-b', normalized_branch, '--track', f'origin/{normalized_branch}'],
                git_username=username,
                git_pat=pat,
            )
        else:
            raise AppError(
                code='BRANCH_NOT_FOUND',
                message='目标分支不存在',
                details={'repo_key': repo_key, 'branch': normalized_branch},
                status_code=404,
            )

        self._run_git(
            repo_dir,
            ['pull', '--ff-only'],
            git_username=username,
            git_pat=pat,
        )

        after_branch = self._current_branch(
            repo_dir=repo_dir,
            git_username=username,
            git_pat=pat,
        )

        return BranchCheckoutResult(
            repo_key=repo_key,
            before_branch=before_branch,
            after_branch=after_branch,
            discarded_changes=has_dirty and discard_changes,
            summary=f'已切换到分支 {after_branch}',
        )

    def _collect_repos(self, workspace_path: Path) -> list[tuple[str, Path]]:
        # repo_key 统一使用工作空间相对路径，主仓使用固定键避免 URL 标准化误伤。
        repo_items: list[tuple[str, Path]] = [(self.ROOT_REPO_KEY, workspace_path)]
        queue: list[tuple[str, Path]] = [(self.ROOT_REPO_KEY, workspace_path)]

        while queue:
            parent_key, parent_path = queue.pop(0)
            for child in self._list_submodule_paths(parent_path):
                repo_key = (
                    child
                    if parent_key == self.ROOT_REPO_KEY
                    else f'{parent_key}/{child}'
                )
                repo_dir = (parent_path / child).resolve()
                if not (repo_dir / '.git').exists():
                    continue
                repo_items.append((repo_key, repo_dir))
                queue.append((repo_key, repo_dir))

        return repo_items

    def _list_submodule_paths(self, repo_dir: Path) -> list[str]:
        gitmodules = (repo_dir / '.gitmodules').resolve()
        if not gitmodules.exists():
            return []

        output = self._run_git(
            repo_dir,
            ['config', '-f', '.gitmodules', '--get-regexp', 'path'],
        )

        paths: list[str] = []
        for line in output.splitlines():
            value = line.strip()
            if not value:
                continue
            parts = value.split(maxsplit=1)
            if len(parts) != 2:
                continue
            path = parts[1].strip()
            if path:
                paths.append(path)
        return paths

    def _resolve_repo_dir(self, workspace_path: Path, repo_key: str) -> Path:
        if repo_key == self.ROOT_REPO_KEY:
            return workspace_path

        relative = repo_key.strip().replace('\\', '/')
        if not relative or relative.startswith('/') or '..' in relative.split('/'):
            raise AppError(
                code='INVALID_REPO_KEY',
                message='仓库路径非法',
                details={'repo_key': repo_key},
                status_code=400,
            )

        repo_dir = (workspace_path / relative).resolve()
        if workspace_path not in repo_dir.parents:
            raise AppError(
                code='INVALID_REPO_KEY',
                message='仓库路径越界',
                details={'repo_key': repo_key},
                status_code=400,
            )
        if not (repo_dir / '.git').exists():
            raise AppError(
                code='REPO_NOT_FOUND',
                message='仓库不存在',
                details={'repo_key': repo_key},
                status_code=404,
            )

        return repo_dir

    def _current_branch(
        self,
        repo_dir: Path,
        git_username: str | None,
        git_pat: str | None,
    ) -> str:
        branch = self._run_git(
            repo_dir,
            ['rev-parse', '--abbrev-ref', 'HEAD'],
            git_username=git_username,
            git_pat=git_pat,
        ).strip()
        return branch or 'HEAD'

    def _dirty_files(
        self,
        repo_dir: Path,
        git_username: str | None,
        git_pat: str | None,
    ) -> list[str]:
        output = self._run_git(
            repo_dir,
            ['status', '--porcelain'],
            git_username=git_username,
            git_pat=git_pat,
        )
        files: list[str] = []
        for line in output.splitlines():
            text = line.rstrip()
            if not text:
                continue
            # porcelain 格式前两位是状态，后面是路径。
            if len(text) > 3:
                files.append(text[3:].strip())
            else:
                files.append(text)
        return files

    def _has_ref(
        self,
        repo_dir: Path,
        ref: str,
        git_username: str | None,
        git_pat: str | None,
    ) -> bool:
        try:
            self._run_git(
                repo_dir,
                ['show-ref', '--verify', '--quiet', ref],
                git_username=git_username,
                git_pat=git_pat,
            )
            return True
        except WorkspaceCreateError:
            return False

    def _workspace_git_credential(self, workspace: str) -> tuple[str | None, str | None]:
        if self._credential_service is None:
            return None, None
        detail = self._credential_service.get_workspace_credential_detail(workspace)
        if detail is None:
            return None, None
        return detail.git_username, detail.git_pat

    def _run_git(
        self,
        cwd: Path,
        args: list[str],
        git_username: str | None = None,
        git_pat: str | None = None,
    ) -> str:
        command = ['git', '-c', 'credential.helper=']
        command.extend(args)

        normalized_user = (git_username or '').strip() or 'oauth2'
        normalized_pat = (git_pat or '').strip() or None

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
            return (completed.stdout or '').strip()
        except FileNotFoundError as exc:
            raise WorkspaceCreateError(cwd.name, 'git command not found') from exc
        except subprocess.CalledProcessError as exc:
            reason = (exc.stderr or exc.stdout or 'git command failed').strip()
            raise WorkspaceCreateError(cwd.name, reason) from exc

    @contextmanager
    def _git_process_env(self, git_username: str, git_pat: str | None):
        env = os.environ.copy()
        env['GIT_TERMINAL_PROMPT'] = '0'
        env['GCM_INTERACTIVE'] = 'Never'

        if not git_pat:
            yield env
            return

        script_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w',
                encoding='utf-8',
                delete=False,
                suffix='.sh',
            ) as script:
                script.write('#!/bin/sh\n')
                script.write('prompt="$1"\n')
                script.write('case "$prompt" in\n')
                script.write('  *sername*|*Username*) echo "$JEWEI_GIT_USERNAME" ;;\n')
                script.write('  *assword*|*Password*) echo "$JEWEI_GIT_PAT" ;;\n')
                script.write('  *) echo "$JEWEI_GIT_PAT" ;;\n')
                script.write('esac\n')
                script_path = script.name

            os.chmod(script_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

            env['GIT_ASKPASS'] = script_path
            env['SSH_ASKPASS'] = script_path
            env['JEWEI_GIT_USERNAME'] = git_username
            env['JEWEI_GIT_PAT'] = git_pat

            yield env
        finally:
            if script_path and os.path.exists(script_path):
                try:
                    os.remove(script_path)
                except OSError:
                    pass


workspace_branch_service = WorkspaceBranchService(
    credential_service=workspace_credential_service,
)
