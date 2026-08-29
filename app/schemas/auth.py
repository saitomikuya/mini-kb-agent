"""Authentication API schemas."""

from typing import Literal

from pydantic import BaseModel, SecretStr


class LoginRequest(BaseModel):
    password: SecretStr


class AuthStatus(BaseModel):
    authenticated: Literal[True] = True
    role: Literal["chat", "admin"]


class LogoutStatus(BaseModel):
    authenticated: Literal[False] = False
