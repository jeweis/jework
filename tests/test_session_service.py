from pathlib import Path

from app.services.session_service import SessionService


def _build_service(tmp_path: Path) -> SessionService:
    service = SessionService()
    service._session_dir = (tmp_path / "sessions").resolve()
    service._session_dir.mkdir(parents=True, exist_ok=True)
    return service


def test_set_claude_session_id_persisted(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    service = _build_service(tmp_path)

    session = service.create_session(
        user_id=42,
        workspace="demo",
        workspace_path=workspace,
    )
    service.set_claude_session_id(
        session_id=session.session_id,
        user_id=42,
        claude_session_id="claude-session-001",
    )

    loaded = service.get_session(session.session_id, user_id=42)
    assert loaded.claude_session_id == "claude-session-001"

    # Verify it survives process restart by loading from disk.
    restarted_service = _build_service(tmp_path)
    reloaded = restarted_service.get_session(session.session_id, user_id=42)
    assert reloaded.claude_session_id == "claude-session-001"
