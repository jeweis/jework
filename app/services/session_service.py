import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import shutil
from uuid import uuid4

from app.core.config import settings
from app.core.errors import SessionNotFoundError


@dataclass
class SessionMessage:
    role: str
    text: str
    created_at: datetime


@dataclass
class SessionData:
    session_id: str
    user_id: int
    workspace: str
    workspace_path: Path
    claude_session_id: str | None
    created_at: datetime
    updated_at: datetime
    messages: list[SessionMessage]


class SessionService:
    def __init__(self) -> None:
        self._store: dict[str, SessionData] = {}
        self._session_dir = (settings.data_dir / "sessions").resolve()
        self._session_dir.mkdir(parents=True, exist_ok=True)

    def create_session(self, user_id: int, workspace: str, workspace_path: Path) -> SessionData:
        session_id = str(uuid4())
        data = SessionData(
            session_id=session_id,
            user_id=user_id,
            workspace=workspace,
            workspace_path=workspace_path,
            claude_session_id=None,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            messages=[],
        )
        self._store[session_id] = data
        self._save_session(data)
        return data

    def get_session(self, session_id: str, user_id: int) -> SessionData:
        data = self._store.get(session_id)
        if data is not None and data.user_id == user_id:
            return data

        data = self._load_session(session_id, user_id=user_id)
        if data is None or data.user_id != user_id:
            raise SessionNotFoundError(session_id)
        self._store[session_id] = data
        return data

    def append_message(self, session_id: str, user_id: int, role: str, text: str) -> None:
        session = self.get_session(session_id, user_id=user_id)
        session.messages.append(
            SessionMessage(
                role=role,
                text=text,
                created_at=datetime.now(timezone.utc),
            )
        )
        session.updated_at = datetime.now(timezone.utc)
        self._save_session(session)

    def list_messages(self, session_id: str, user_id: int) -> list[SessionMessage]:
        session = self.get_session(session_id, user_id=user_id)
        return list(session.messages)

    def set_claude_session_id(self, session_id: str, user_id: int, claude_session_id: str) -> None:
        session = self.get_session(session_id, user_id=user_id)
        if session.claude_session_id == claude_session_id:
            return
        session.claude_session_id = claude_session_id
        session.updated_at = datetime.now(timezone.utc)
        self._save_session(session)

    def list_workspace_sessions(self, user_id: int, workspace: str) -> list[SessionData]:
        sessions: list[SessionData] = []
        user_dir = self._user_session_dir(workspace, user_id, create=False)
        if not user_dir.exists():
            return sessions
        for session_file in user_dir.glob("*.json"):
            data = self._load_session_by_file(session_file)
            if data is None:
                continue
            self._store[data.session_id] = data
            if data.workspace == workspace and data.user_id == user_id:
                sessions.append(data)

        sessions.sort(key=lambda item: item.updated_at, reverse=True)
        return sessions

    def get_latest_workspace_session(self, user_id: int, workspace: str) -> SessionData | None:
        sessions = self.list_workspace_sessions(user_id=user_id, workspace=workspace)
        if not sessions:
            return None
        return sessions[0]

    def _workspace_session_dir(self, workspace: str, create: bool = True) -> Path:
        workspace_dir = (self._session_dir / workspace).resolve()
        if create:
            workspace_dir.mkdir(parents=True, exist_ok=True)
        return workspace_dir

    def _user_session_dir(self, workspace: str, user_id: int, create: bool = True) -> Path:
        user_dir = (self._workspace_session_dir(workspace, create=create) / str(user_id)).resolve()
        if create:
            user_dir.mkdir(parents=True, exist_ok=True)
        return user_dir

    def _session_file(self, workspace: str, user_id: int, session_id: str) -> Path:
        return (self._user_session_dir(workspace, user_id) / f"{session_id}.json").resolve()

    def _save_session(self, session: SessionData) -> None:
        payload = {
            "session_id": session.session_id,
            "user_id": session.user_id,
            "workspace": session.workspace,
            "workspace_path": str(session.workspace_path),
            "claude_session_id": session.claude_session_id,
            "created_at": session.created_at.isoformat(),
            "updated_at": session.updated_at.isoformat(),
            "messages": [
                {
                    **asdict(message),
                    "created_at": message.created_at.isoformat(),
                }
                for message in session.messages
            ],
        }
        self._session_file(session.workspace, session.user_id, session.session_id).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_session(self, session_id: str, user_id: int) -> SessionData | None:
        session_file = self._find_session_file(session_id=session_id, user_id=user_id)
        if session_file is None:
            return None
        return self._load_session_by_file(session_file)

    def _find_session_file(self, session_id: str, user_id: int) -> Path | None:
        pattern = f"*/{user_id}/{session_id}.json"
        matches = list(self._session_dir.glob(pattern))
        if not matches:
            return None
        return matches[0]

    def _load_session_by_file(self, session_file: Path) -> SessionData | None:
        if not session_file.exists():
            return None
        payload = json.loads(session_file.read_text(encoding="utf-8"))
        user_id = payload.get("user_id")
        if not isinstance(user_id, int):
            # Skip legacy session files that do not belong to the current user model.
            return None
        created_at = datetime.fromisoformat(payload["created_at"])
        updated_raw = payload.get("updated_at")
        updated_at = datetime.fromisoformat(updated_raw) if updated_raw else created_at
        return SessionData(
            session_id=payload["session_id"],
            user_id=user_id,
            workspace=payload["workspace"],
            workspace_path=Path(payload["workspace_path"]),
            claude_session_id=payload.get("claude_session_id"),
            created_at=created_at,
            updated_at=updated_at,
            messages=[
                SessionMessage(
                    role=item["role"],
                    text=item["text"],
                    created_at=datetime.fromisoformat(item["created_at"]),
                )
                for item in payload.get("messages", [])
            ],
        )

    def delete_workspace_sessions(self, workspace: str) -> int:
        workspace_dir = (self._session_dir / workspace).resolve()
        if not workspace_dir.exists():
            return 0
        files = list(workspace_dir.rglob("*.json"))
        shutil.rmtree(workspace_dir, ignore_errors=True)
        return len(files)


session_service = SessionService()
