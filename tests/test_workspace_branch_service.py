from __future__ import annotations

import subprocess
from pathlib import Path

from app.services.workspace_branch_service import WorkspaceBranchService


def _run_git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ['git', *args],
        cwd=str(cwd),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def _commit_file(
    repo_dir: Path,
    *,
    file_name: str,
    content: str,
    message: str,
) -> None:
    (repo_dir / file_name).write_text(content, encoding='utf-8')
    _run_git(repo_dir, 'add', file_name)
    _run_git(repo_dir, 'commit', '-m', message)


def test_list_branches_recovers_from_single_branch_fetch_config(tmp_path: Path) -> None:
    remote_repo = tmp_path / 'remote.git'
    _run_git(tmp_path, 'init', '--bare', str(remote_repo))

    seed_repo = tmp_path / 'seed'
    seed_repo.mkdir()
    _run_git(seed_repo, 'init')
    _run_git(seed_repo, 'config', 'user.name', 'Tester')
    _run_git(seed_repo, 'config', 'user.email', 'tester@example.com')
    _commit_file(
        seed_repo,
        file_name='README.md',
        content='# demo\n',
        message='init master',
    )
    _run_git(seed_repo, 'branch', '-M', 'master')
    _run_git(seed_repo, 'remote', 'add', 'origin', str(remote_repo))
    _run_git(seed_repo, 'push', '-u', 'origin', 'master')
    _run_git(seed_repo, 'checkout', '-b', 'dev')
    _commit_file(
        seed_repo,
        file_name='dev.txt',
        content='dev\n',
        message='init dev',
    )
    _run_git(seed_repo, 'push', '-u', 'origin', 'dev')

    workspace_repo = tmp_path / 'workspace'
    _run_git(
        tmp_path,
        'clone',
        '--single-branch',
        '--branch',
        'master',
        str(remote_repo),
        str(workspace_repo),
    )
    _run_git(
        workspace_repo,
        'config',
        'remote.origin.fetch',
        '+refs/heads/master:refs/remotes/origin/master',
    )

    before_remote_branches = _run_git(
        workspace_repo,
        'for-each-ref',
        '--format=%(refname:short)',
        'refs/remotes/origin',
    )
    assert 'origin/dev' not in before_remote_branches.splitlines()

    service = WorkspaceBranchService()
    current_branch, branches = service.list_branches(
        workspace='demo',
        workspace_path=workspace_repo,
        repo_key=WorkspaceBranchService.ROOT_REPO_KEY,
    )

    assert current_branch == 'master'
    assert 'master' in branches
    assert 'dev' in branches
