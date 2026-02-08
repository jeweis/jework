from fastapi import Header

from app.core.errors import AuthRequiredError
from app.services.auth_service import AuthUser, auth_service


def get_current_user(authorization: str | None = Header(default=None)) -> AuthUser:
    if not authorization:
        raise AuthRequiredError()

    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise AuthRequiredError()

    token = authorization[len(prefix) :].strip()
    if not token:
        raise AuthRequiredError()

    return auth_service.get_user_by_token(token)
