from fastapi import Header

from app.core.errors import AuthRequiredError
from app.services.auth_service import AuthUser, auth_service
from app.services.mcp_token_service import mcp_token_service


def get_current_mcp_user(authorization: str | None = Header(default=None)) -> AuthUser:
    if not authorization:
        raise AuthRequiredError()
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise AuthRequiredError()

    token = authorization[len(prefix) :].strip()
    if not token:
        raise AuthRequiredError()

    user_id = mcp_token_service.verify_token(token)
    return auth_service.get_user_by_id(user_id)
