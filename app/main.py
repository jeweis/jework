from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.api.routes import router
from app.core.config import settings
from app.core.errors import register_exception_handlers
from app.services.auth_service import auth_service
from app.services.llm_config_service import llm_config_service
from app.services.workspace_credential_service import workspace_credential_service
from app.services.workspace_git_service import workspace_git_service

app = FastAPI(title="Jework", version="0.1.0")
register_exception_handlers(app)
app.include_router(router)

_STATIC_DIR = settings.frontend_static_dir
_INDEX_FILE = _STATIC_DIR / "index.html"


@app.on_event("startup")
def _startup() -> None:
    auth_service.init_db()
    llm_config_service.init_db()
    workspace_credential_service.init_db()
    workspace_git_service.init_db()


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
