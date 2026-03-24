from datetime import datetime, timedelta, timezone
import sqlite3

from app.services.workspace_auto_pull_service import WorkspaceAutoPullService


def test_auto_pull_settings_default_row_created(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    service = WorkspaceAutoPullService(str(db_path))

    service.init_db()
    settings = service.get_settings()

    assert settings.enabled is False
    assert settings.interval_minutes == 60
    assert settings.last_run_at is None
    assert settings.next_run_at is None
    assert settings.updated_by is None
    assert settings.updated_at is not None


def test_update_settings_persists_enabled_and_next_run(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    service = WorkspaceAutoPullService(str(db_path))
    service.init_db()

    settings = service.update_settings(
        enabled=True,
        interval_minutes=30,
        updated_by=7,
    )

    assert settings.enabled is True
    assert settings.interval_minutes == 30
    assert settings.updated_by == 7
    assert settings.next_run_at is not None


def test_should_run_now_uses_next_run_time(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    service = WorkspaceAutoPullService(str(db_path))
    service.init_db()
    service.update_settings(enabled=True, interval_minutes=15, updated_by=1)

    settings = service.get_settings()
    assert settings.next_run_at is not None

    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            UPDATE workspace_auto_pull_settings
            SET next_run_at = ?
            WHERE id = 1
            """,
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
        )
        conn.commit()

    assert service.should_run_now() is True


def test_mark_run_finished_updates_last_and_next_run(tmp_path) -> None:
    db_path = tmp_path / "app.db"
    service = WorkspaceAutoPullService(str(db_path))
    service.init_db()
    service.update_settings(enabled=True, interval_minutes=180, updated_by=3)

    updated = service.mark_run_finished()

    assert updated.last_run_at is not None
    assert updated.next_run_at is not None
    last_run = datetime.fromisoformat(updated.last_run_at)
    next_run = datetime.fromisoformat(updated.next_run_at)
    assert next_run > last_run
