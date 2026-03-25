from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from app.api import mcp_routes
from app.api.mcp_routes import execute_mcp_tool
from app.core.errors import AppError
from app.services.auth_service import AuthUser
from app.services.workspace_credential_service import WorkspaceCredentialService
from app.services.workspace_git_service import WorkspaceGitService
from app.services.workspace_service import WorkspaceService
from app.services.workspace_tag_service import WorkspaceTagService


def _run_git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return completed.stdout.strip()


def _run_git_allow_file(
    cwd: Path,
    *args: str,
    env: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args],
        cwd=str(cwd),
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return completed.stdout.strip()


def _commit_file(
    repo_dir: Path,
    *,
    file_name: str,
    content: str,
    message: str,
    author_name: str,
    author_email: str,
    authored_at: str,
) -> str:
    target = repo_dir / file_name
    target.write_text(content, encoding="utf-8")
    _run_git(repo_dir, "add", file_name)
    env = {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_AUTHOR_DATE": authored_at,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
        "GIT_COMMITTER_DATE": authored_at,
    }
    _run_git(repo_dir, "commit", "-m", message, env=env)
    return _run_git(repo_dir, "rev-parse", "HEAD")


def _create_submodule_repo(parent_dir: Path) -> tuple[Path, str]:
    submodule_source = parent_dir / "submodule-source"
    submodule_source.mkdir(parents=True, exist_ok=True)
    _run_git(submodule_source, "init")
    _run_git(submodule_source, "config", "user.name", "Submodule User")
    _run_git(submodule_source, "config", "user.email", "submodule@example.com")
    commit_id = _commit_file(
        submodule_source,
        file_name="module.py",
        content="print('hello')\n",
        message="submodule init",
        author_name="Carol",
        author_email="carol@example.com",
        authored_at="2026-02-20T12:00:00+00:00",
    )
    return submodule_source, commit_id


def _commit_all(
    repo_dir: Path,
    *,
    message: str,
    author_name: str,
    author_email: str,
    authored_at: str,
) -> str:
    _run_git(repo_dir, "add", ".")
    env = {
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_AUTHOR_DATE": authored_at,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
        "GIT_COMMITTER_DATE": authored_at,
    }
    _run_git(repo_dir, "commit", "-m", message, env=env)
    return _run_git(repo_dir, "rev-parse", "HEAD")


def test_search_git_commits_and_detail(tmp_path: Path) -> None:
    service = WorkspaceService(tmp_path)
    service.init_db()
    item = service.create_workspace("git-demo")
    repo_dir = Path(item.path)

    _run_git(repo_dir, "init")
    _run_git(repo_dir, "config", "user.name", "Default User")
    _run_git(repo_dir, "config", "user.email", "default@example.com")

    first_commit = _commit_file(
        repo_dir,
        file_name="README.md",
        content="# demo\n",
        message="init docs",
        author_name="Alice",
        author_email="alice@example.com",
        authored_at="2026-01-10T08:00:00+00:00",
    )
    second_commit = _commit_file(
        repo_dir,
        file_name="README.md",
        content="# demo\n\nupdated\n",
        message="update docs",
        author_name="Bob",
        author_email="bob@example.com",
        authored_at="2026-02-15T09:30:00+00:00",
    )

    result = service.search_git_commits(
        "git-demo",
        start_time="2026-01-01",
        end_time="2026-03-01",
        page=1,
        page_size=1,
    )

    assert result.workspace == "git-demo"
    assert result.page == 1
    assert result.page_size == 1
    assert result.has_more is True
    assert len(result.items) == 1
    assert result.items[0].commit_id == second_commit
    assert result.items[0].subject == "update docs"
    assert result.items[0].repo.repo_path == "."
    assert result.items[0].repo.current_branch is not None

    filtered = service.search_git_commits(
        "git-demo",
        start_time="2026-01-01",
        end_time="2026-03-01",
        page=1,
        page_size=10,
        author="Alice",
    )
    assert [item.commit_id for item in filtered.items] == [first_commit]

    detail = service.get_git_commit_detail("git-demo", commit_id=second_commit)
    assert detail.commit_id == second_commit
    assert detail.subject == "update docs"
    assert detail.repo.repo_path == "."
    assert "README.md" in detail.changed_files
    assert "updated" in detail.patch
    assert detail.truncated is False


def test_execute_mcp_tool_rejects_non_git_workspace_and_large_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = WorkspaceService(tmp_path)
    service.init_db()
    service.create_workspace("plain-workspace")
    git_item = service.create_workspace("git-workspace")
    git_repo_dir = Path(git_item.path)
    _run_git(git_repo_dir, "init")
    _run_git(git_repo_dir, "config", "user.name", "Default User")
    _run_git(git_repo_dir, "config", "user.email", "default@example.com")
    _commit_file(
        git_repo_dir,
        file_name="README.md",
        content="# demo\n",
        message="init docs",
        author_name="Alice",
        author_email="alice@example.com",
        authored_at="2026-01-10T08:00:00+00:00",
    )
    monkeypatch.setattr(mcp_routes, "workspace_service", service)

    current_user = AuthUser(
        id=1,
        username="root",
        role="superadmin",
        created_at="2026-03-24T00:00:00+00:00",
    )

    with pytest.raises(AppError) as non_git_error:
        execute_mcp_tool(
            current_user=current_user,
            tool="search_git_commits",
            arguments={
                "workspace": "plain-workspace",
                "start_time": "2026-01-01",
                "end_time": "2026-01-31",
                "page": 1,
                "page_size": 20,
            },
        )
    assert non_git_error.value.code == "MCP_GIT_UNAVAILABLE"

    with pytest.raises(AppError) as range_error:
        execute_mcp_tool(
            current_user=current_user,
            tool="search_git_commits",
            arguments={
                "start_time": "2026-01-01",
                "end_time": "2026-05-02",
                "page": 1,
                "page_size": 20,
                "workspace": "git-workspace",
            },
        )
    assert range_error.value.code == "MCP_GIT_TIME_RANGE_TOO_LARGE"


def test_list_workspaces_includes_last_pull_at_for_git_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "app.db"
    credential_service = WorkspaceCredentialService(str(db_path))
    credential_service.init_db()
    git_service = WorkspaceGitService(str(db_path))
    git_service.init_db()
    service = WorkspaceService(
        tmp_path,
        credential_service=credential_service,
        git_service=git_service,
    )
    service.init_db()

    git_item = service.create_workspace("git-workspace")
    service.create_workspace("plain-workspace")
    credential_service.upsert_workspace_credential(
        workspace=git_item.workspace_id,
        user_id=1,
        git_url="https://example.com/org/repo.git",
    )
    git_service.set_pull_result(
        git_item.workspace_id,
        status="success",
        message="ok",
        pulled_at="2026-03-24T09:30:00+08:00",
    )
    monkeypatch.setattr(mcp_routes, "workspace_service", service)

    current_user = AuthUser(
        id=1,
        username="root",
        role="superadmin",
        created_at="2026-03-24T00:00:00+00:00",
    )

    result = execute_mcp_tool(
        current_user=current_user,
        tool="list_workspaces",
        arguments={},
    )

    git_summary = next(item for item in result["items"] if item["name"] == "git-workspace")
    plain_summary = next(
        item for item in result["items"] if item["name"] == "plain-workspace"
    )
    assert git_summary["last_pull_at"] == "2026-03-24T09:30:00+08:00"
    assert "last_pull_at" not in plain_summary


def test_list_workspaces_includes_tags_for_mcp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "app.db"
    tag_service = WorkspaceTagService(str(db_path))
    service = WorkspaceService(
        tmp_path,
        tag_service=tag_service,
    )
    service.init_db()

    item = service.create_workspace("tagged-workspace")
    tag_service.replace_tags(
        workspace=item.workspace_id,
        tags=["后端", "高优先级"],
        updated_at="2026-03-25T10:00:00+08:00",
    )
    monkeypatch.setattr(mcp_routes, "workspace_service", service)

    current_user = AuthUser(
        id=1,
        username="root",
        role="superadmin",
        created_at="2026-03-24T00:00:00+00:00",
    )

    result = execute_mcp_tool(
        current_user=current_user,
        tool="list_workspaces",
        arguments={},
    )

    items = result["items"]
    tagged = next(item for item in items if item["name"] == "tagged-workspace")
    assert tagged["tags"] == ["后端", "高优先级"]


def test_search_git_commits_includes_submodule_results(tmp_path: Path) -> None:
    service = WorkspaceService(tmp_path)
    service.init_db()
    item = service.create_workspace("git-submodule-demo")
    repo_dir = Path(item.path)

    _run_git(repo_dir, "init")
    _run_git(repo_dir, "config", "user.name", "Default User")
    _run_git(repo_dir, "config", "user.email", "default@example.com")
    _commit_file(
        repo_dir,
        file_name="README.md",
        content="# demo\n",
        message="root init",
        author_name="Alice",
        author_email="alice@example.com",
        authored_at="2026-02-10T08:00:00+00:00",
    )

    submodule_source, submodule_commit = _create_submodule_repo(tmp_path)
    _run_git_allow_file(
        repo_dir,
        "submodule",
        "add",
        str(submodule_source),
        "third_party/demo-lib",
    )
    _commit_all(
        repo_dir,
        message="add submodule",
        author_name="Bob",
        author_email="bob@example.com",
        authored_at="2026-02-21T09:00:00+00:00",
    )

    result = service.search_git_commits(
        "git-submodule-demo",
        start_time="2026-02-01",
        end_time="2026-03-01",
        page=1,
        page_size=20,
    )

    submodule_hits = [
        item for item in result.items if item.repo.repo_path == "third_party/demo-lib"
    ]
    assert submodule_hits
    assert submodule_hits[0].commit_id == submodule_commit
    assert submodule_hits[0].repo.current_branch is not None

    detail = service.get_git_commit_detail(
        "git-submodule-demo",
        commit_id=submodule_commit,
        repo_path="third_party/demo-lib",
    )
    assert detail.repo.repo_path == "third_party/demo-lib"
    assert detail.commit_id == submodule_commit
    assert "module.py" in detail.changed_files
