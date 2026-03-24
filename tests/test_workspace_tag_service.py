from app.services.workspace_tag_service import WorkspaceTagService


def test_replace_and_list_tags(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    service = WorkspaceTagService(str(db_path))

    item = service.replace_tags(
        workspace="workspace-1",
        tags=["后端", "高优先级", "后端", " "],
        updated_at="2026-03-24T12:00:00+00:00",
    )

    assert item.tags == ["后端", "高优先级"]
    listed = service.list_tags()
    assert listed["workspace-1"].tags == ["后端", "高优先级"]


def test_replace_tags_overwrites_previous_values(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    service = WorkspaceTagService(str(db_path))

    service.replace_tags(
        workspace="workspace-1",
        tags=["A", "B"],
        updated_at="2026-03-24T12:00:00+00:00",
    )
    item = service.replace_tags(
        workspace="workspace-1",
        tags=["C"],
        updated_at="2026-03-24T12:05:00+00:00",
    )

    assert item.tags == ["C"]
    assert service.get_tags("workspace-1") is not None
    assert service.get_tags("workspace-1").tags == ["C"]
