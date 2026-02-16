from datetime import datetime, timezone
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable
import logging
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.branch_routes import router as branch_router
from app.api.mcp_routes import router as mcp_router
from app.api.routes import router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.services.auth_service import auth_service
from app.services.feishu_settings_service import feishu_settings_service
from app.services.llm_config_service import llm_config_service
from app.services.mcp_audit_service import mcp_audit_service
from app.services.mcp_index_job_service import mcp_index_job_service
from app.services.mcp_settings_service import mcp_settings_service
from app.services.mcp_fastmcp_service import build_fastmcp_asgi_app
from app.services.mcp_token_service import mcp_token_service
from app.services.mcp_vector_service import mcp_vector_service
from app.services.workspace_credential_service import workspace_credential_service
from app.services.workspace_git_service import workspace_git_service

_fastmcp_app = build_fastmcp_asgi_app()

logger = logging.getLogger(__name__)

_STATIC_DIR = settings.frontend_static_dir
_INDEX_FILE = _STATIC_DIR / "index.html"


def _combine_lifespans(
    *lifespans: Callable[[], AsyncIterator[None]],
) -> Callable[[], AsyncIterator[None]]:
    """组合多个 lifespan，上下文按传入顺序嵌套执行。"""

    @asynccontextmanager
    async def _merged() -> AsyncIterator[None]:
        async with lifespans[0]():
            if len(lifespans) == 1:
                yield
            else:
                nested = _combine_lifespans(*lifespans[1:])
                async with nested():
                    yield

    return _merged


@asynccontextmanager
async def _core_lifespan() -> AsyncIterator[None]:
    _startup()
    yield


@asynccontextmanager
async def _fastmcp_lifespan(app: FastAPI) -> AsyncIterator[None]:
    if _fastmcp_app is not None and hasattr(_fastmcp_app, "lifespan"):
        async with _fastmcp_app.lifespan(app):
            yield
    else:
        yield


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    """组合 Jework 与 FastMCP 生命周期，避免子应用 task group 未初始化。"""
    merged = _combine_lifespans(_core_lifespan, lambda: _fastmcp_lifespan(app))
    async with merged():
        yield


app = FastAPI(
    title="Jework",
    version="0.1.0",
    lifespan=_app_lifespan,
)
register_exception_handlers(app)
app.include_router(router)
app.include_router(branch_router)
app.include_router(mcp_router)
if _fastmcp_app is not None:
    app.mount("/mcp", _fastmcp_app)


def _bind_workspace_from_mcp_path(request: Request, path: str) -> bool:
    """识别 /mcp/{workspace} 绑定入口并注入上下文。

    返回 True 表示该路径已按绑定入口处理，并已完成重写。
    """
    base = "/mcp"
    if not path.startswith(f"{base}/"):
        return False
    suffix = path[len(base) + 1 :]
    if not suffix:
        return False
    # 仅在单段路径下视为 workspace 绑定，例如 /mcp/test-ai-doc。
    if "/" in suffix:
        return False

    request.scope.setdefault("state", {})
    request.scope["state"]["mcp_bound_workspace"] = suffix
    request.scope["path"] = "/mcp/"
    return True


@app.middleware("http")
async def _rewrite_dynamic_mcp_base_path(
    request: Request,
    call_next,
):
    # mcp_base_path 在数据库更新后应立即生效。
    # 这里通过请求路径重写到固定实现路由 /mcp*，避免重启服务。
    try:
        configured_base_path = mcp_settings_service.resolve_mcp_base_path()
    except Exception:
        configured_base_path = "/mcp"

    raw_path = request.url.path or "/"
    incoming_path = raw_path.rstrip("/") or "/"

    # FastMCP 挂载根入口需要以 "/" 结尾，避免 POST /mcp 命中 405。
    if incoming_path == "/mcp":
        request.scope["path"] = "/mcp/"
        return await call_next(request)
    if _bind_workspace_from_mcp_path(request, incoming_path):
        return await call_next(request)

    if configured_base_path != "/mcp":
        base = configured_base_path.rstrip("/") or "/"
        if incoming_path == base or incoming_path.startswith(f"{base}/"):
            suffix = incoming_path[len(base) :]
            suffix = suffix if suffix.startswith("/") else f"/{suffix}" if suffix else ""
            if suffix in {"", "/"}:
                request.scope["path"] = "/mcp/"
            else:
                maybe_workspace = suffix.lstrip("/")
                if "/" not in maybe_workspace and maybe_workspace:
                    request.scope.setdefault("state", {})
                    request.scope["state"]["mcp_bound_workspace"] = maybe_workspace
                    request.scope["path"] = "/mcp/"
                else:
                    request.scope["path"] = f"/mcp{suffix}"
    return await call_next(request)


def _startup() -> None:
    auth_service.init_db()
    feishu_settings_service.init_db()
    llm_config_service.init_db()
    workspace_credential_service.init_db()
    workspace_git_service.init_db()
    mcp_token_service.init_db()
    mcp_settings_service.init_db()
    mcp_index_job_service.init_db()
    mcp_audit_service.init_db()
    mcp_vector_service.init_db()
    mcp_audit_service.cleanup_old_logs(keep_days=30)
    _start_mcp_audit_cleanup_daemon()


def _start_mcp_audit_cleanup_daemon() -> None:
    def _loop() -> None:
        while True:
            try:
                removed = mcp_audit_service.cleanup_old_logs(keep_days=30)
                logger.info(
                    "mcp audit cleanup finished removed=%s at=%s",
                    removed,
                    datetime.now(timezone.utc).isoformat(),
                )
            except Exception:
                logger.exception("mcp audit cleanup failed")
            # 每 24 小时执行一次，满足“定时清理”策略。
            time.sleep(24 * 60 * 60)

    thread = threading.Thread(target=_loop, daemon=True)
    thread.start()


@app.get("/")
def home_page() -> FileResponse:
    if _INDEX_FILE.exists():
        return FileResponse(_INDEX_FILE)
    raise HTTPException(status_code=404, detail="Frontend static files not found")


@app.get("/{full_path:path}")
def spa_fallback(full_path: str) -> FileResponse:
    candidate = (_STATIC_DIR / full_path).resolve()
    if candidate.exists() and candidate.is_file() and _STATIC_DIR in candidate.parents:
        return FileResponse(candidate)

    if _INDEX_FILE.exists():
        return FileResponse(_INDEX_FILE)

    raise HTTPException(status_code=404, detail="Frontend static files not found")
