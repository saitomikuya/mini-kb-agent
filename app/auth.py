"""FastAPI authentication dependencies."""

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.services.auth import (
    AUTH_COOKIE_NAME,
    AuthPrincipal,
    AuthService,
    InvalidSessionError,
)


def get_auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


def get_current_principal(
    request: Request,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthPrincipal:
    token = request.cookies.get(AUTH_COOKIE_NAME)
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
        )
    try:
        return auth_service.read_session(token)
    except InvalidSessionError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired authentication session",
        ) from exc


def require_chat(
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> AuthPrincipal:
    """Allow chat sessions and administrators to access chat APIs."""
    return principal


def require_admin(
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> AuthPrincipal:
    """Allow only administrators to access management APIs."""
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return principal
