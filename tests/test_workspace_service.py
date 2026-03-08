from pathlib import Path

import pytest

from app.core.errors import WorkspaceAlreadyExistsError
from app.services.workspace_service import WorkspaceService


def test_list_workspaces_only_directories(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")

    service = WorkspaceService(tmp_path)
    names = [item.name for item in service.list_workspaces()]

    assert names == ["a", "b"]


def test_create_workspace_without_git(tmp_path: Path) -> None:
    service = WorkspaceService(tmp_path)

    item = service.create_workspace("demo")

    assert item.name == "demo"
    assert (tmp_path / "demo").exists()


def test_create_workspace_duplicate(tmp_path: Path) -> None:
    service = WorkspaceService(tmp_path)
    service.create_workspace("demo")

    with pytest.raises(WorkspaceAlreadyExistsError):
        service.create_workspace("demo")


def test_create_personal_workspace_under_project_dir(tmp_path: Path) -> None:
    service = WorkspaceService(tmp_path)

    item = service.create_workspace("my", mode="personal", creator_user_id=7)

    assert item.mode == "personal"
    assert item.owner_user_id == 7
    assert (tmp_path / "personal" / "7" / "workspace" / "project" / "my").exists()


def test_create_personal_workspace_recover_missing_directory(tmp_path: Path) -> None:
    service = WorkspaceService(tmp_path)

    first = service.create_workspace("my", mode="personal", creator_user_id=7)
    workspace_dir = tmp_path / "personal" / "7" / "workspace" / "project" / "my"
    assert workspace_dir.exists()

    # 模拟用户手动删目录，仅保留 DB 记录。
    workspace_dir.rmdir()
    assert not workspace_dir.exists()

    recovered = service.create_workspace("my", mode="personal", creator_user_id=7)

    assert recovered.workspace_id == first.workspace_id
    assert recovered.name == "my"
    assert recovered.mode == "personal"
    assert workspace_dir.exists()


def test_resolve_personal_agent_workspace_roots(tmp_path: Path) -> None:
    service = WorkspaceService(tmp_path)

    main_root = service.get_personal_main_agent_workspace_root(9)
    qa_root = service.get_personal_agent_workspace_root(9, "qa")

    assert main_root == (tmp_path / "personal" / "9" / "workspace")
    assert qa_root == (tmp_path / "personal" / "9" / "workspace-qa")
