"""Authentication endpoint and role-dependency tests."""

from pathlib import Path
import stat
from typing import Annotated, Iterator

from fastapi import Depends
from fastapi.testclient import TestClient
import pytest

from app.auth import require_admin, require_chat
from app.config import Settings
from app.main import create_app
from app.services.auth import AUTH_COOKIE_NAME, AuthPrincipal


CHAT_PASSWORD = "chat-password-for-tests"
ADMIN_PASSWORD = "admin-password-for-tests"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        chat_password=CHAT_PASSWORD,
        admin_password=ADMIN_PASSWORD,
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
        session_max_age=3600,
    )
    application = create_app(settings)

    # Internal test-only probes verify the reusable dependencies. They are not
    # registered by the production application or exposed in its OpenAPI schema.
    @application.get("/_internal/tests/chat", include_in_schema=False)
    def chat_probe(
        principal: Annotated[AuthPrincipal, Depends(require_chat)],
    ) -> dict[str, str]:
        return {"role": principal.role}

    @application.get("/_internal/tests/admin", include_in_schema=False)
    def admin_probe(
        principal: Annotated[AuthPrincipal, Depends(require_admin)],
    ) -> dict[str, str]:
        return {"role": principal.role}

    with TestClient(application) as test_client:
        yield test_client


def test_correct_chat_password_sets_signed_cookie(client: TestClient) -> None:
    response = client.post(
        "/api/auth/chat/login",
        json={"password": CHAT_PASSWORD},
    )

    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "role": "chat"}
    set_cookie = response.headers["set-cookie"].lower()
    assert f"{AUTH_COOKIE_NAME}=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert client.get("/api/auth/me").json() == {
        "authenticated": True,
        "role": "chat",
    }
    assert client.get("/_internal/tests/chat").status_code == 200

    secret_path = client.app.state.settings.secret_path
    assert secret_path.read_bytes()
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600


def test_wrong_chat_password_is_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/auth/chat/login",
        json={"password": "wrong-password"},
    )

    assert response.status_code == 401
    assert AUTH_COOKIE_NAME not in response.cookies
    assert client.get("/api/auth/me").status_code == 401


def test_correct_admin_password(client: TestClient) -> None:
    response = client.post(
        "/api/auth/admin/login",
        json={"password": ADMIN_PASSWORD},
    )

    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "role": "admin"}
    assert client.get("/api/auth/me").json()["role"] == "admin"


def test_chat_cookie_cannot_access_admin_dependency(client: TestClient) -> None:
    login = client.post(
        "/api/auth/chat/login",
        json={"password": CHAT_PASSWORD},
    )
    assert login.status_code == 200

    response = client.get("/_internal/tests/admin")

    assert response.status_code == 403


def test_admin_cookie_can_access_admin_dependency(client: TestClient) -> None:
    login = client.post(
        "/api/auth/admin/login",
        json={"password": ADMIN_PASSWORD},
    )
    assert login.status_code == 200

    response = client.get("/_internal/tests/admin")

    assert response.status_code == 200
    assert response.json() == {"role": "admin"}
    assert client.get("/_internal/tests/chat").status_code == 200


def test_logout_clears_session(client: TestClient) -> None:
    assert client.post(
        "/api/auth/chat/login",
        json={"password": CHAT_PASSWORD},
    ).status_code == 200

    response = client.post("/api/auth/logout")

    assert response.status_code == 200
    assert response.json() == {"authenticated": False}
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert client.get("/api/auth/me").status_code == 401


def test_tampered_cookie_is_rejected(client: TestClient) -> None:
    login = client.post(
        "/api/auth/chat/login",
        json={"password": CHAT_PASSWORD},
    )
    token = login.cookies[AUTH_COOKIE_NAME]
    tamper_at = len(token) // 2
    replacement = "x" if token[tamper_at] != "x" else "y"
    tampered = token[:tamper_at] + replacement + token[tamper_at + 1 :]
    client.cookies.clear()
    client.cookies.set(AUTH_COOKIE_NAME, tampered)

    response = client.get("/api/auth/me")

    assert response.status_code == 401
