import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
import shutil
from uuid import uuid4

from app.core.config import settings
from app.core.errors import AppError

TERMINAL_RUN_STATUS = {"done", "error", "canceled"}


class SessionRunNotFoundError(AppError):
    def __init__(self, run_id: str):
        super().__init__(
            code="SESSION_RUN_NOT_FOUND",
            message=f"Session run not found: {run_id}",
            details={"run_id": run_id},
            status_code=404,
        )


@dataclass
class SessionRunEvent:
    seq: int
    type: str
    created_at: str
    data: str = ""
    title: str | None = None
    message: str | None = None


@dataclass
class SessionRunData:
    run_id: str
    session_id: str
    user_id: int
    prompt: str
    status: str
    created_at: str
    updated_at: str
    last_seq: int
    error_message: str | None = None


class SessionRunService:
    """
    管理会话后台运行（run）与事件日志（event log）。

    设计目标：
    1) 前端断线后 run 继续执行，不依赖单个 HTTP 请求生命周期。
    2) 通过 seq 游标重放事件，支持页面刷新后的断点恢复展示。
    """

    def __init__(self) -> None:
        self._root_dir = (settings.data_dir / "session_runs").resolve()
        self._root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        # 内存订阅队列仅用于“实时推送”，历史回放依赖磁盘事件日志。
        self._subscribers: dict[str, list[asyncio.Queue[SessionRunEvent]]] = {}

    def create_run(self, *, session_id: str, user_id: int, prompt: str) -> SessionRunData:
        now = datetime.now(timezone.utc).isoformat()
        run = SessionRunData(
            run_id=str(uuid4()),
            session_id=session_id,
            user_id=user_id,
            prompt=prompt,
            status="queued",
            created_at=now,
            updated_at=now,
            last_seq=0,
        )
        with self._lock:
            self._save_run(run)
        return run

    def get_run(self, *, session_id: str, run_id: str, user_id: int) -> SessionRunData:
        with self._lock:
            run = self._load_run(session_id=session_id, user_id=user_id, run_id=run_id)
            if run is None:
                raise SessionRunNotFoundError(run_id)
            return run

    def get_latest_running_run(self, *, session_id: str, user_id: int) -> SessionRunData | None:
        """
        返回 session 下最近一个运行中的 run。
        """
        with self._lock:
            base_dir = self._session_user_dir(session_id=session_id, user_id=user_id, create=False)
            if not base_dir.exists():
                return None
            latest: SessionRunData | None = None
            for run_dir in base_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                run_file = run_dir / "run.json"
                if not run_file.exists():
                    continue
                payload = json.loads(run_file.read_text(encoding="utf-8"))
                run = self._payload_to_run(payload)
                if run.status not in {"queued", "running"}:
                    continue
                if latest is None or run.updated_at > latest.updated_at:
                    latest = run
            return latest

    def set_run_status(
        self,
        *,
        session_id: str,
        run_id: str,
        user_id: int,
        status: str,
        error_message: str | None = None,
    ) -> SessionRunData:
        with self._lock:
            run = self._load_run(session_id=session_id, user_id=user_id, run_id=run_id)
            if run is None:
                raise SessionRunNotFoundError(run_id)
            run.status = status
            run.updated_at = datetime.now(timezone.utc).isoformat()
            run.error_message = error_message
            self._save_run(run)
            return run

    def append_event(
        self,
        *,
        session_id: str,
        run_id: str,
        user_id: int,
        event_type: str,
        data: str = "",
        title: str | None = None,
        message: str | None = None,
    ) -> SessionRunEvent:
        """
        追加事件并广播给实时订阅者。

        注意：
        - 先落盘再广播，确保前端即使断线也可用 after_seq 回放。
        """
        with self._lock:
            run = self._load_run(session_id=session_id, user_id=user_id, run_id=run_id)
            if run is None:
                raise SessionRunNotFoundError(run_id)
            next_seq = run.last_seq + 1
            event = SessionRunEvent(
                seq=next_seq,
                type=event_type,
                created_at=datetime.now(timezone.utc).isoformat(),
                data=data,
                title=title,
                message=message,
            )
            self._append_event_to_file(
                session_id=session_id,
                user_id=user_id,
                run_id=run_id,
                event=event,
            )
            run.last_seq = next_seq
            run.updated_at = event.created_at
            self._save_run(run)

            subscribers = list(self._subscribers.get(run_id, []))

        # 广播不持锁，避免慢消费者阻塞写盘。
        for queue in subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # 队列满时丢弃实时通知；客户端可通过 after_seq 回放补齐。
                continue
        return event

    def list_events_after(
        self,
        *,
        session_id: str,
        run_id: str,
        user_id: int,
        after_seq: int,
    ) -> list[SessionRunEvent]:
        with self._lock:
            run = self._load_run(session_id=session_id, user_id=user_id, run_id=run_id)
            if run is None:
                raise SessionRunNotFoundError(run_id)
            event_file = self._event_file(session_id=session_id, user_id=user_id, run_id=run_id)
            if not event_file.exists():
                return []

            events: list[SessionRunEvent] = []
            for raw in event_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line:
                    continue
                payload = json.loads(line)
                seq = int(payload.get("seq", 0))
                if seq <= after_seq:
                    continue
                events.append(self._payload_to_event(payload))
            return events

    def subscribe(self, run_id: str) -> asyncio.Queue[SessionRunEvent]:
        queue: asyncio.Queue[SessionRunEvent] = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subscribers.setdefault(run_id, []).append(queue)
        return queue

    def unsubscribe(self, run_id: str, queue: asyncio.Queue[SessionRunEvent]) -> None:
        with self._lock:
            queues = self._subscribers.get(run_id, [])
            if queue in queues:
                queues.remove(queue)
            if not queues and run_id in self._subscribers:
                self._subscribers.pop(run_id, None)

    def has_running_run(self, *, session_id: str, user_id: int) -> bool:
        """
        判断会话是否存在进行中的 run（queued/running）。
        """
        return self.get_latest_running_run(session_id=session_id, user_id=user_id) is not None

    def delete_session_runs(self, *, session_id: str, user_id: int) -> int:
        """
        删除会话的全部 run 数据，返回删除的 run 数量。
        """
        with self._lock:
            session_user_dir = self._session_user_dir(
                session_id=session_id,
                user_id=user_id,
                create=False,
            )
            if not session_user_dir.exists():
                return 0

            run_count = 0
            run_ids: list[str] = []
            for run_dir in session_user_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                run_count += 1
                run_ids.append(run_dir.name)

            shutil.rmtree(session_user_dir, ignore_errors=True)
            for run_id in run_ids:
                self._subscribers.pop(run_id, None)
            return run_count

    def _session_user_dir(self, *, session_id: str, user_id: int, create: bool) -> Path:
        root = (self._root_dir / session_id / str(user_id)).resolve()
        if create:
            root.mkdir(parents=True, exist_ok=True)
        return root

    def _run_dir(self, *, session_id: str, user_id: int, run_id: str, create: bool) -> Path:
        run_dir = (self._session_user_dir(session_id=session_id, user_id=user_id, create=create) / run_id).resolve()
        if create:
            run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    def _run_file(self, *, session_id: str, user_id: int, run_id: str) -> Path:
        return self._run_dir(session_id=session_id, user_id=user_id, run_id=run_id, create=True) / "run.json"

    def _event_file(self, *, session_id: str, user_id: int, run_id: str) -> Path:
        return self._run_dir(session_id=session_id, user_id=user_id, run_id=run_id, create=True) / "events.jsonl"

    def _save_run(self, run: SessionRunData) -> None:
        run_file = self._run_file(session_id=run.session_id, user_id=run.user_id, run_id=run.run_id)
        run_file.write_text(
            json.dumps(asdict(run), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_run(self, *, session_id: str, user_id: int, run_id: str) -> SessionRunData | None:
        run_file = (
            self._root_dir / session_id / str(user_id) / run_id / "run.json"
        ).resolve()
        if not run_file.exists():
            return None
        payload = json.loads(run_file.read_text(encoding="utf-8"))
        return self._payload_to_run(payload)

    def _append_event_to_file(
        self,
        *,
        session_id: str,
        user_id: int,
        run_id: str,
        event: SessionRunEvent,
    ) -> None:
        event_file = self._event_file(session_id=session_id, user_id=user_id, run_id=run_id)
        serialized = json.dumps(asdict(event), ensure_ascii=False)
        with event_file.open("a", encoding="utf-8") as fp:
            fp.write(serialized + "\n")

    def _payload_to_run(self, payload: dict) -> SessionRunData:
        return SessionRunData(
            run_id=str(payload["run_id"]),
            session_id=str(payload["session_id"]),
            user_id=int(payload["user_id"]),
            prompt=str(payload.get("prompt", "")),
            status=str(payload["status"]),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
            last_seq=int(payload.get("last_seq", 0)),
            error_message=payload.get("error_message"),
        )

    def _payload_to_event(self, payload: dict) -> SessionRunEvent:
        return SessionRunEvent(
            seq=int(payload["seq"]),
            type=str(payload["type"]),
            created_at=str(payload["created_at"]),
            data=str(payload.get("data", "")),
            title=payload.get("title"),
            message=payload.get("message"),
        )


session_run_service = SessionRunService()
