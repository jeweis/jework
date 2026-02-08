import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


class AppError(Exception):
    def __init__(self, code: str, message: str, details: Any = None, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.status_code = status_code


class WorkspaceNotFoundError(AppError):
    def __init__(self, workspace: str):
        super().__init__(
            code="WORKSPACE_NOT_FOUND",
            message=f"Workspace not found: {workspace}",
            details={"workspace": workspace},
            status_code=404,
        )


class WorkspaceAlreadyExistsError(AppError):
    def __init__(self, workspace: str):
        super().__init__(
            code="WORKSPACE_ALREADY_EXISTS",
            message=f"Workspace already exists: {workspace}",
            details={"workspace": workspace},
            status_code=409,
        )


class WorkspaceCreateError(AppError):
    def __init__(self, workspace: str, reason: str):
        super().__init__(
            code="WORKSPACE_CREATE_ERROR",
            message="Failed to create workspace",
            details={"workspace": workspace, "reason": reason},
            status_code=400,
        )


class WorkspaceDeleteError(AppError):
    def __init__(self, workspace: str, reason: str):
        super().__init__(
            code="WORKSPACE_DELETE_ERROR",
            message="Failed to delete workspace",
            details={"workspace": workspace, "reason": reason},
            status_code=400,
        )


class SessionNotFoundError(AppError):
    def __init__(self, session_id: str):
        super().__init__(
            code="SESSION_NOT_FOUND",
            message=f"Session not found: {session_id}",
            details={"session_id": session_id},
            status_code=404,
        )


class AgentInvocationError(AppError):
    def __init__(self, reason: str):
        super().__init__(
            code="AGENT_INVOCATION_ERROR",
            message="Failed to invoke claude agent",
            details={"reason": reason},
            status_code=500,
        )


class InvalidWorkspaceError(AppError):
    def __init__(self, workspace: str):
        super().__init__(
            code="INVALID_WORKSPACE",
            message="Workspace path is invalid",
            details={"workspace": workspace},
            status_code=400,
        )


class WorkspaceCredentialError(AppError):
    def __init__(self, reason: str):
        super().__init__(
            code="WORKSPACE_CREDENTIAL_ERROR",
            message="Workspace credential operation failed",
            details={"reason": reason},
            status_code=400,
        )


class AuthRequiredError(AppError):
    def __init__(self):
        super().__init__(
            code="AUTH_REQUIRED",
            message="Authentication required",
            status_code=401,
        )


class AuthInvalidCredentialsError(AppError):
    def __init__(self, message: str = "Invalid username or password"):
        super().__init__(
            code="AUTH_INVALID_CREDENTIALS",
            message=message,
            status_code=401,
        )


class AuthForbiddenError(AppError):
    def __init__(self):
        super().__init__(
            code="AUTH_FORBIDDEN",
            message="Permission denied",
            status_code=403,
        )


class UserAlreadyExistsError(AppError):
    def __init__(self, username: str):
        super().__init__(
            code="USER_ALREADY_EXISTS",
            message=f"User already exists: {username}",
            details={"username": username},
            status_code=409,
        )


class UserBootstrapNotAllowedError(AppError):
    def __init__(self):
        super().__init__(
            code="BOOTSTRAP_NOT_ALLOWED",
            message="Superadmin has already been initialized",
            status_code=409,
        )


def _error_payload(code: str, message: str, details: Any = None) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": details,
        }
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        logger.warning(
            "AppError on %s %s: code=%s message=%s details=%s",
            request.method,
            request.url.path,
            exc.code,
            exc.message,
            exc.details,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(Exception)
    async def _internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled exception on %s %s",
            request.method,
            request.url.path,
        )
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                code="INTERNAL_SERVER_ERROR",
                message="Unexpected server error",
                details={"reason": str(exc)},
            ),
        )
