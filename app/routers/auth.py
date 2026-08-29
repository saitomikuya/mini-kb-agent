"""Authentication HTTP endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.auth import get_auth_service, get_current_principal
from app.schemas.auth import AuthStatus, LoginRequest, LogoutStatus
from app.services.auth import (
    AUTH_COOKIE_NAME,
    AuthPrincipal,
    AuthRole,
    AuthService,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])


def _login(
    role: AuthRole,
    credentials: LoginRequest,
    response: Response,
    auth_service: AuthService,
) -> AuthStatus:
    if not auth_service.password_matches(role, credentials.password.get_secret_value()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid password",
        )

    response.set_cookie(
        key=AUTH_COOKIE_NAME,
        value=auth_service.issue_session(role),
        max_age=auth_service.settings.session_max_age,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return AuthStatus(role=role)


@router.post("/chat/login", response_model=AuthStatus)
def chat_login(
    credentials: LoginRequest,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthStatus:
    return _login("chat", credentials, response, auth_service)


@router.post("/admin/login", response_model=AuthStatus)
def admin_login(
    credentials: LoginRequest,
    response: Response,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
) -> AuthStatus:
    return _login("admin", credentials, response, auth_service)


@router.post("/logout", response_model=LogoutStatus)
def logout(response: Response) -> LogoutStatus:
    response.delete_cookie(
        key=AUTH_COOKIE_NAME,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return LogoutStatus()


@router.get("/me", response_model=AuthStatus)
def me(
    principal: Annotated[AuthPrincipal, Depends(get_current_principal)],
) -> AuthStatus:
    return AuthStatus(role=principal.role)
