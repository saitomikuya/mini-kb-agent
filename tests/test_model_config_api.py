"""Admin model-configuration API, encryption, role, and test coverage."""

from collections.abc import Iterator
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest
import respx

from app.config import Settings
from app.db import Base
from app.llm.clients import OpenAICompatibleClient
from app.llm.registry import ModelRegistry, ModelRoleNotConfiguredError
from app.llm.types import ModelRole
from app.main import create_app
from app.models.model_config import APIProvider, ModelProfile


ADMIN_PASSWORD = "admin-model-config-tests"
RAW_API_KEY = "sk-stage03-super-secret-abcd"


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    settings = Settings(
        admin_password=ADMIN_PASSWORD,
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
        session_max_age=3600,
    )
    settings.data_dir.mkdir(parents=True)
    application = create_app(settings)
    Base.metadata.create_all(application.state.database_engine)
    with TestClient(application) as test_client:
        response = test_client.post(
            "/api/auth/admin/login",
            json={"password": ADMIN_PASSWORD},
        )
        assert response.status_code == 200
        yield test_client


def _create_provider(
    client: TestClient,
    *,
    name: str = "Provider",
    provider_type: str = "openai_compatible",
    base_url: str = "https://models.example.test/v1",
    protocol_preference: str = "auto",
    azure_mode: str = "v1",
    azure_api_version: str | None = None,
) -> dict:
    response = client.post(
        "/api/admin/providers",
        json={
            "name": name,
            "provider_type": provider_type,
            "base_url": base_url,
            "api_key": RAW_API_KEY,
            "protocol_preference": protocol_preference,
            "extra_headers_json": {"X-Client": "stage-03-tests"},
            "azure_mode": azure_mode,
            "azure_api_version": azure_api_version,
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_profile(
    client: TestClient,
    provider_id: int,
    *,
    name: str,
    remote_model_name: str | None = None,
) -> dict:
    response = client.post(
        "/api/admin/model-profiles",
        json={
            "provider_id": provider_id,
            "name": name,
            "remote_model_name": remote_model_name or f"remote-{name}",
            "context_window": 128000,
            "max_output_tokens": 1024,
            "extra_request_json": {},
            "enabled": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _mark_capable(
    client: TestClient,
    profile_ids: list[int],
    *,
    vision: bool = True,
) -> None:
    with client.app.state.session_factory() as session:
        for profile_id in profile_ids:
            profile = session.get(ModelProfile, profile_id)
            assert profile is not None
            profile.supports_text = True
            profile.supports_vision = vision
            profile.supports_structured_output = False
            profile.tested_protocol = "responses"
            profile.last_test_status = "partial"
        session.commit()


def test_admin_authentication_is_required(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        source_dir=tmp_path / "sources",
    )
    settings.data_dir.mkdir(parents=True)
    application = create_app(settings)
    Base.metadata.create_all(application.state.database_engine)
    with TestClient(application) as unauthenticated:
        assert unauthenticated.get("/api/admin/providers").status_code == 401


def test_one_provider_can_own_multiple_model_profiles(client: TestClient) -> None:
    provider = _create_provider(client)
    first = _create_profile(client, provider["id"], name="fast")
    second = _create_profile(client, provider["id"], name="vision")

    response = client.get(
        "/api/admin/model-profiles",
        params={"provider_id": provider["id"]},
    )

    assert response.status_code == 200
    assert {item["id"] for item in response.json()} == {first["id"], second["id"]}
    assert {item["provider_id"] for item in response.json()} == {provider["id"]}


def test_four_roles_can_bind_different_profiles(client: TestClient) -> None:
    provider = _create_provider(client)
    roles = [
        "document_conversion",
        "index_generation",
        "query_router",
        "answer_generation",
    ]
    profiles = [
        _create_profile(client, provider["id"], name=role) for role in roles
    ]
    _mark_capable(client, [profile["id"] for profile in profiles])

    for role, profile in zip(roles, profiles, strict=True):
        response = client.put(
            f"/api/admin/model-roles/{role}",
            json={"model_profile_id": profile["id"]},
        )
        assert response.status_code == 200, response.text

    bindings = client.get("/api/admin/model-roles").json()
    assert {item["role"]: item["model_profile_id"] for item in bindings} == {
        role: profile["id"]
        for role, profile in zip(roles, profiles, strict=True)
    }


def test_four_roles_can_share_one_profile(client: TestClient) -> None:
    provider = _create_provider(client)
    profile = _create_profile(client, provider["id"], name="shared")
    _mark_capable(client, [profile["id"]])

    for role in (
        "document_conversion",
        "index_generation",
        "query_router",
        "answer_generation",
    ):
        response = client.put(
            f"/api/admin/model-roles/{role}",
            json={"model_profile_id": profile["id"]},
        )
        assert response.status_code == 200

    assert {
        item["model_profile_id"]
        for item in client.get("/api/admin/model-roles").json()
    } == {profile["id"]}

    with client.app.state.session_factory() as session:
        model_registry = ModelRegistry(session, client.app.state.api_key_cipher)
        resolved = model_registry.get_for_role(ModelRole.ANSWER_GENERATION)
        assert isinstance(resolved, OpenAICompatibleClient)


def test_delete_referenced_profile_is_rejected(client: TestClient) -> None:
    provider = _create_provider(client)
    profile = _create_profile(client, provider["id"], name="bound")
    _mark_capable(client, [profile["id"]])
    assert client.put(
        "/api/admin/model-roles/answer_generation",
        json={"model_profile_id": profile["id"]},
    ).status_code == 200

    response = client.delete(f"/api/admin/model-profiles/{profile['id']}")

    assert response.status_code == 409
    assert client.get(f"/api/admin/model-profiles/{profile['id']}").status_code == 200


def test_conversion_binding_rejects_profile_without_tested_vision(
    client: TestClient,
) -> None:
    provider = _create_provider(client)
    profile = _create_profile(client, provider["id"], name="text-only")
    _mark_capable(client, [profile["id"]], vision=False)

    response = client.put(
        "/api/admin/model-roles/document_conversion",
        json={"model_profile_id": profile["id"]},
    )

    assert response.status_code == 422
    assert "vision" in response.json()["detail"]


def test_role_enum_is_validated_and_unconfigured_startup_is_allowed(
    client: TestClient,
) -> None:
    bindings = client.get("/api/admin/model-roles")
    assert bindings.status_code == 200
    assert len(bindings.json()) == 4
    assert all(item["model_profile_id"] is None for item in bindings.json())

    invalid = client.put(
        "/api/admin/model-roles/default_model",
        json={"model_profile_id": None},
    )
    assert invalid.status_code == 422

    with client.app.state.session_factory() as session:
        model_registry = ModelRegistry(session, client.app.state.api_key_cipher)
        with pytest.raises(ModelRoleNotConfiguredError):
            model_registry.get_for_role(ModelRole.QUERY_ROUTER)


def test_simplified_profile_creation_uses_compatibility_defaults(
    client: TestClient,
) -> None:
    provider = _create_provider(client)

    response = client.post(
        "/api/admin/model-profiles",
        json={
            "provider_id": provider["id"],
            "name": "simple",
            "remote_model_name": "remote-simple",
            "enabled": True,
        },
    )

    assert response.status_code == 201, response.text
    profile = response.json()
    assert profile["context_window"] == 32_768
    assert profile["max_output_tokens"] == 4_096
    assert profile["reasoning_effort"] is None
    assert profile["protocol_override"] is None
    assert profile["extra_request_json"] == {}


def test_supported_role_task_prompts_have_defaults_and_can_be_updated_unbound(
    client: TestClient,
) -> None:
    bindings = {
        item["role"]: item for item in client.get("/api/admin/model-roles").json()
    }
    router = bindings["query_router"]
    assert router["model_profile_id"] is None
    assert [task["task"] for task in router["prompt_tasks"]] == [
        "folder_selection",
        "document_selection",
    ]
    assert all(
        task["prompt"] == task["default_prompt"]
        for task in router["prompt_tasks"]
    )

    prompts = {
        task["task"]: f"custom {task['task']} prompt"
        for task in router["prompt_tasks"]
    }
    updated = client.put(
        "/api/admin/model-roles/query_router/prompts",
        json={"prompts": prompts},
    )

    assert updated.status_code == 200, updated.text
    payload = updated.json()
    assert payload["model_profile_id"] is None
    assert {
        task["task"]: task["prompt"] for task in payload["prompt_tasks"]
    } == prompts
    assert payload["prompts_updated_at"] is not None


def test_role_prompt_update_rejects_unsupported_or_missing_tasks(
    client: TestClient,
) -> None:
    response = client.put(
        "/api/admin/model-roles/answer_generation/prompts",
        json={"prompts": {"unsupported": "do something"}},
    )

    assert response.status_code == 422
    assert "missing tasks" in response.json()["detail"]
    assert "unsupported tasks" in response.json()["detail"]


def test_api_key_is_encrypted_and_never_returned(client: TestClient) -> None:
    provider = _create_provider(client)

    assert provider["api_key_masked"] == "sk-****abcd"
    assert RAW_API_KEY not in str(provider)
    assert "encrypted_api_key" not in provider
    assert RAW_API_KEY not in client.get("/api/admin/providers").text
    assert RAW_API_KEY not in client.get(
        f"/api/admin/providers/{provider['id']}"
    ).text

    with client.app.state.session_factory() as session:
        stored = session.get(APIProvider, provider["id"])
        assert stored is not None
        assert stored.encrypted_api_key != RAW_API_KEY
        assert RAW_API_KEY not in stored.encrypted_api_key
        assert (
            client.app.state.api_key_cipher.decrypt(stored.encrypted_api_key)
            == RAW_API_KEY
        )

    malformed = client.post(
        "/api/admin/providers",
        json={
            "name": "Malformed credential",
            "provider_type": "openai_compatible",
            "base_url": "https://models.example.test/v1",
            "api_key": {"supplied_secret": RAW_API_KEY},
        },
    )
    assert malformed.status_code == 422
    assert RAW_API_KEY not in malformed.text

    unsafe_header = client.post(
        "/api/admin/providers",
        json={
            "name": "Unsafe header",
            "provider_type": "openai_compatible",
            "base_url": "https://models.example.test/v1",
            "api_key": RAW_API_KEY,
            "extra_headers_json": {"Authorization": f"Bearer {RAW_API_KEY}"},
        },
    )
    assert unsafe_header.status_code == 422
    assert RAW_API_KEY not in unsafe_header.text


@respx.mock
def test_model_profile_connection_test_persists_all_capabilities(
    client: TestClient,
) -> None:
    provider = _create_provider(client)
    profile = _create_profile(client, provider["id"], name="tested")
    route = respx.post("https://models.example.test/v1/responses")
    route.side_effect = [
        httpx.Response(200, json={"output_text": "text works"}),
        httpx.Response(200, json={"output_text": '{"ok": true}'}),
        httpx.Response(200, json={"output_text": "KB-VISION-42"}),
    ]

    response = client.post(f"/api/admin/model-profiles/{profile['id']}/test")

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "passed"
    assert result["tested_protocol"] == "responses"
    assert result["supports_text"] is True
    assert result["supports_vision"] is True
    assert result["supports_structured_output"] is True
    assert result["json"]["passed"] is True
    assert route.call_count == 3

    persisted = client.get(f"/api/admin/model-profiles/{profile['id']}").json()
    assert persisted["supports_text"] is True
    assert persisted["supports_vision"] is True
    assert persisted["supports_structured_output"] is True
    assert persisted["last_test_status"] == "passed"
    assert persisted["last_tested_at"] is not None


@respx.mock
def test_vision_failure_keeps_text_profile_available_for_non_conversion_roles(
    client: TestClient,
) -> None:
    provider = _create_provider(client)
    profile = _create_profile(client, provider["id"], name="text-json-only")
    route = respx.post("https://models.example.test/v1/responses")
    route.side_effect = [
        httpx.Response(200, json={"output_text": "text works"}),
        httpx.Response(200, json={"output_text": '{"ok": true}'}),
        httpx.Response(200, json={"output_text": "cannot read image"}),
    ]

    tested = client.post(f"/api/admin/model-profiles/{profile['id']}/test")

    assert tested.status_code == 200
    assert tested.json()["status"] == "partial"
    assert tested.json()["supports_text"] is True
    assert tested.json()["supports_vision"] is False
    assert client.put(
        "/api/admin/model-roles/answer_generation",
        json={"model_profile_id": profile["id"]},
    ).status_code == 200
    assert client.put(
        "/api/admin/model-roles/query_router",
        json={"model_profile_id": profile["id"]},
    ).status_code == 200
    assert client.put(
        "/api/admin/model-roles/document_conversion",
        json={"model_profile_id": profile["id"]},
    ).status_code == 422


@respx.mock
def test_provider_errors_do_not_expose_api_key_in_response_or_logs(
    client: TestClient,
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider = _create_provider(client, protocol_preference="responses")
    profile = _create_profile(client, provider["id"], name="failure")
    respx.post("https://models.example.test/v1/responses").mock(
        return_value=httpx.Response(
            401,
            json={"error": f"credential rejected: {RAW_API_KEY}"},
        )
    )

    with caplog.at_level("INFO"):
        response = client.post(
            f"/api/admin/model-profiles/{profile['id']}/test"
        )

    assert response.status_code == 200
    assert response.json()["status"] == "failed"
    assert RAW_API_KEY not in response.text
    assert RAW_API_KEY not in caplog.text
