"""Password verification and signed authentication session handling."""

from dataclasses import dataclass
import hashlib
import hmac
import os
from pathlib import Path
import secrets
from threading import Lock
from typing import Literal, cast

from itsdangerous import BadData, URLSafeTimedSerializer

from app.config import Settings


AuthRole = Literal["chat", "admin"]
AUTH_COOKIE_NAME = "mini_kb_auth"
_SESSION_SALT = "mini-kb-agent.auth-session.v1"
_SESSION_VERSION = 1


class InvalidSessionError(ValueError):
    """Raised when an authentication cookie is invalid or expired."""


@dataclass(frozen=True, slots=True)
class AuthPrincipal:
    role: AuthRole


class AuthService:
    """Own authentication secrets and signed role sessions for one app."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._serializer: URLSafeTimedSerializer | None = None
        self._serializer_lock = Lock()

    def ensure_secret(self) -> None:
        """Create and load the signing secret if this is the first run."""
        self._get_serializer()

    def password_matches(self, role: AuthRole, candidate: str) -> bool:
        expected = (
            self.settings.chat_password
            if role == "chat"
            else self.settings.admin_password
        )
        return bool(expected) and hmac.compare_digest(candidate, expected)

    def issue_session(self, role: AuthRole) -> str:
        return self._get_serializer().dumps(
            {"version": _SESSION_VERSION, "role": role}
        )

    def read_session(self, token: str) -> AuthPrincipal:
        try:
            payload = self._get_serializer().loads(
                token,
                max_age=self.settings.session_max_age,
            )
        except BadData as exc:
            raise InvalidSessionError("Invalid or expired authentication session") from exc

        if not isinstance(payload, dict) or payload.get("version") != _SESSION_VERSION:
            raise InvalidSessionError("Invalid authentication session payload")
        role = payload.get("role")
        if role not in ("chat", "admin"):
            raise InvalidSessionError("Invalid authentication role")
        return AuthPrincipal(role=cast(AuthRole, role))

    def _get_serializer(self) -> URLSafeTimedSerializer:
        if self._serializer is None:
            with self._serializer_lock:
                if self._serializer is None:
                    secret = load_or_create_root_secret(self.settings.secret_path)
                    self._serializer = URLSafeTimedSerializer(
                        secret_key=secret,
                        salt=_SESSION_SALT,
                        signer_kwargs={"digest_method": hashlib.sha256},
                    )
        return self._serializer


def load_or_create_root_secret(secret_path: Path) -> bytes:
    """Load the stable application root secret, creating it when absent."""
    secret_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    try:
        return _read_secret(secret_path)
    except FileNotFoundError:
        pass

    generated_secret = secrets.token_bytes(32)
    temporary_path = secret_path.with_name(
        f".{secret_path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    file_descriptor = os.open(temporary_path, flags, 0o600)
    try:
        with os.fdopen(file_descriptor, "wb") as secret_file:
            file_descriptor = -1
            secret_file.write(generated_secret)
            secret_file.flush()
            os.fsync(secret_file.fileno())
        try:
            os.link(temporary_path, secret_path)
        except FileExistsError:
            pass
        except OSError:
            _write_secret_exclusively(secret_path, generated_secret)
    finally:
        if file_descriptor >= 0:
            os.close(file_descriptor)
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass

    return _read_secret(secret_path)


def _write_secret_exclusively(secret_path: Path, secret: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        file_descriptor = os.open(secret_path, flags, 0o600)
    except FileExistsError:
        return

    with os.fdopen(file_descriptor, "wb") as secret_file:
        secret_file.write(secret)
        secret_file.flush()
        os.fsync(secret_file.fileno())


def _read_secret(secret_path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    file_descriptor = os.open(secret_path, flags)
    with os.fdopen(file_descriptor, "rb") as secret_file:
        try:
            os.fchmod(secret_file.fileno(), 0o600)
        except OSError:
            # Some mounted filesystems do not allow chmod; signing can still work.
            pass
        secret = secret_file.read()

    if len(secret) < 32:
        raise RuntimeError(f"Signing secret is invalid: {secret_path}")
    return secret
