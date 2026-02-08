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
