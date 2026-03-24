from app.services.user_workspace_preference_service import (
    UserWorkspacePreferenceService,
)


def test_get_preference_returns_default_when_missing(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    service = UserWorkspacePreferenceService(str(db_path))
    service.init_db()

    item = service.get_preference(9)

    assert item.user_id == 9
    assert item.selected_tags == []
    assert item.updated_at is None


def test_update_selected_tags_deduplicates_and_sorts(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    service = UserWorkspacePreferenceService(str(db_path))
    service.init_db()

    item = service.update_selected_tags(
        user_id=3,
        selected_tags=["后端", "紧急", "后端", " ", "测试"],
    )

    assert item.user_id == 3
    assert item.selected_tags == ["后端", "测试", "紧急"]
    assert item.updated_at is not None

    loaded = service.get_preference(3)
    assert loaded.selected_tags == ["后端", "测试", "紧急"]
