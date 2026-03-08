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


def test_create_personal_agent_session_and_list(tmp_path: Path) -> None:
    personal_root = tmp_path / "personal" / "7" / "workspace"
    personal_root.mkdir(parents=True)
    service = _build_service(tmp_path)

    created = service.create_personal_agent_session(
        user_id=7,
        workspace_path=personal_root,
    )

    assert created.scope == "personal_agent"
    assert created.workspace == "personal-agent"
    listed = service.list_personal_agent_sessions(user_id=7)
    assert len(listed) == 1
    assert listed[0].session_id == created.session_id
    assert listed[0].workspace_path == personal_root


def test_delete_session_removes_file_and_store(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    service = _build_service(tmp_path)

    session = service.create_session(
        user_id=9,
        workspace="demo",
        workspace_path=workspace,
    )
    session_file = service._session_file(  # noqa: SLF001 - 单测校验持久化行为
        workspace=session.workspace,
        user_id=session.user_id,
        session_id=session.session_id,
    )
    assert session_file.exists()

    deleted = service.delete_session(session_id=session.session_id, user_id=9)

    assert deleted.session_id == session.session_id
    assert not session_file.exists()
    assert session.session_id not in service._store  # noqa: SLF001 - 校验内存态一致性
