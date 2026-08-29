"""Mock-HTTP tests for protocol adapters and fallback behavior."""

import asyncio
from collections.abc import Callable
import json

import httpx
import pytest

from app.llm.clients import (
    AzureOpenAIClient,
    ModelResponseError,
    OpenAICompatibleClient,
    Sub2APIClient,
)
from app.models.model_config import APIProvider, ModelProfile


API_KEY = "sk-adapter-secret-1234"


def _provider(
    *,
    provider_type: str = "openai_compatible",
    base_url: str = "https://adapter.example.test/v1",
    protocol: str = "auto",
    azure_mode: str = "v1",
    azure_api_version: str | None = None,
) -> APIProvider:
    return APIProvider(
        id=1,
        name="adapter",
        provider_type=provider_type,
        base_url=base_url,
        encrypted_api_key="not-used-by-direct-adapter-tests",
        protocol_preference=protocol,
        extra_headers_json={},
        azure_mode=azure_mode,
        azure_api_version=azure_api_version,
        enabled=True,
    )


def _profile(
    *,
    remote_model_name: str = "remote-model",
    protocol_override: str | None = None,
) -> ModelProfile:
    return ModelProfile(
        id=1,
        provider_id=1,
        name="profile",
        remote_model_name=remote_model_name,
        protocol_override=protocol_override,
        context_window=128000,
        max_output_tokens=512,
        reasoning_effort=None,
        extra_request_json={},
        enabled=True,
    )


def _factory(
    handler: Callable[[httpx.Request], httpx.Response],
) -> Callable[[], httpx.AsyncClient]:
    transport = httpx.MockTransport(handler)
    return lambda: httpx.AsyncClient(transport=transport)


def test_openai_compatible_responses_is_actually_called() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        payload = json.loads(request.content)
        assert payload["model"] == "remote-model"
        assert request.headers["Authorization"] == f"Bearer {API_KEY}"
        return httpx.Response(200, json={"output_text": "responses-ok"})

    client = OpenAICompatibleClient(
        _provider(),
        _profile(),
        API_KEY,
        http_client_factory=_factory(handler),
    )

    result = asyncio.run(client.generate_text("hello"))

    assert result.text == "responses-ok"
    assert result.protocol == "responses"
    assert seen_paths == ["/v1/responses"]
    assert all(path != "/v1/models" for path in seen_paths)


def test_responses_stream_forwards_reasoning_summary_and_json_output() -> None:
    captured_payload: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        events = [
            {
                "type": "response.reasoning_summary_text.delta",
                "delta": "先核对证据，再组织答案。",
            },
            {"type": "response.output_text.delta", "delta": '{"ok":'},
            {"type": "response.output_text.delta", "delta": "true}"},
            {"type": "response.completed", "response": {}},
        ]
        body = "".join(
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            for event in events
        )
        return httpx.Response(
            200,
            text=body,
            headers={"content-type": "text/event-stream"},
        )

    client = OpenAICompatibleClient(
        _provider(protocol="responses"),
        _profile(),
        API_KEY,
        http_client_factory=_factory(handler),
    )
    progress: list[tuple[str, dict]] = []

    async def run_generation():
        async def on_progress(progress_type: str, data: dict) -> None:
            progress.append((progress_type, data))

        return await client.generate_json_stream(
            "return ok",
            on_progress=on_progress,
            verbosity="low",
            json_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
            },
        )

    result = asyncio.run(run_generation())

    assert result.value == {"ok": True}
    assert result.native_structured_output is True
    assert captured_payload["stream"] is True
    assert captured_payload["store"] is False
    assert captured_payload["reasoning"]["summary"] == "auto"
    assert captured_payload["text"]["verbosity"] == "low"
    assert progress == [
        (
            "reasoning_summary",
            {"summary": "先核对证据，再组织答案。"},
        )
    ]


def test_auto_falls_back_to_chat_completions_after_responses_failure() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path.endswith("/responses"):
            return httpx.Response(404, json={"error": "unsupported"})
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "chat-ok"}}]},
        )

    client = OpenAICompatibleClient(
        _provider(),
        _profile(),
        API_KEY,
        http_client_factory=_factory(handler),
    )

    result = asyncio.run(client.generate_text("hello"))

    assert result.text == "chat-ok"
    assert result.protocol == "chat_completions"
    assert seen_paths == ["/v1/responses", "/v1/chat/completions"]


def test_json_prompt_and_parse_fallback_when_native_output_is_unavailable() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if "text" in payload:
            return httpx.Response(400, json={"error": "format unsupported"})
        return httpx.Response(
            200,
            json={"output_text": '```json\n{"ok": true}\n```'},
        )

    client = OpenAICompatibleClient(
        _provider(protocol="responses"),
        _profile(),
        API_KEY,
        http_client_factory=_factory(handler),
    )

    result = asyncio.run(client.generate_json("return ok"))

    assert result.value == {"ok": True}
    assert result.native_structured_output is False
    assert len(payloads) == 2
    assert "text" in payloads[0]
    assert "Return only one valid JSON object" in payloads[1]["input"]


def test_json_fallback_repairs_one_malformed_response() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if "text" in payload:
            return httpx.Response(400, json={"error": "format unsupported"})
        if len(payloads) == 2:
            return httpx.Response(200, json={"output_text": '{"ok": tru'})
        return httpx.Response(200, json={"output_text": '{"ok": true}'})

    client = OpenAICompatibleClient(
        _provider(protocol="responses"),
        _profile(),
        API_KEY,
        http_client_factory=_factory(handler),
    )

    result = asyncio.run(client.generate_json("return ok"))

    assert result.value == {"ok": True}
    assert result.native_structured_output is False
    assert len(payloads) == 3
    assert "Repair the malformed response" in payloads[2]["input"]


def test_json_fallback_repair_is_attempted_only_once() -> None:
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        if "text" in payload:
            return httpx.Response(400, json={"error": "format unsupported"})
        return httpx.Response(200, json={"output_text": '{"ok": tru'})

    client = OpenAICompatibleClient(
        _provider(protocol="responses"),
        _profile(),
        API_KEY,
        http_client_factory=_factory(handler),
    )

    with pytest.raises(ModelResponseError, match="valid JSON object"):
        asyncio.run(client.generate_json("return ok"))

    assert len(payloads) == 3


def test_azure_v1_normalizes_url_and_uses_deployment_as_model() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        captured["api_key"] = request.headers["api-key"]
        return httpx.Response(200, json={"output_text": "azure-ok"})

    client = AzureOpenAIClient(
        _provider(
            provider_type="azure_openai",
            base_url="https://resource.openai.azure.com",
        ),
        _profile(remote_model_name="production-deployment"),
        API_KEY,
        http_client_factory=_factory(handler),
    )

    result = asyncio.run(client.generate_text("hello"))

    assert result.protocol == "responses"
    assert captured["url"] == (
        "https://resource.openai.azure.com/openai/v1/responses"
    )
    assert captured["payload"]["model"] == "production-deployment"
    assert captured["api_key"] == API_KEY


def test_azure_legacy_uses_deployment_url_and_api_version() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "legacy-ok"}}]},
        )

    client = AzureOpenAIClient(
        _provider(
            provider_type="azure_openai",
            base_url="https://resource.openai.azure.com/openai/v1/",
            azure_mode="legacy",
            azure_api_version="2024-10-21",
        ),
        _profile(remote_model_name="legacy deployment"),
        API_KEY,
        http_client_factory=_factory(handler),
    )

    result = asyncio.run(client.generate_text("hello"))

    assert result.protocol == "chat_completions"
    assert captured["url"] == (
        "https://resource.openai.azure.com/openai/deployments/"
        "legacy%20deployment/chat/completions?api-version=2024-10-21"
    )
    assert captured["payload"]["model"] == "legacy deployment"


def test_sub2api_keeps_distinct_client_type_and_reuses_compatible_protocol() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/responses"
        return httpx.Response(200, json={"output_text": "sub2api-ok"})

    client = Sub2APIClient(
        _provider(provider_type="sub2api"),
        _profile(),
        API_KEY,
        http_client_factory=_factory(handler),
    )

    result = asyncio.run(client.generate_text("hello"))

    assert type(client) is Sub2APIClient
    assert result.text == "sub2api-ok"
    assert result.protocol == "responses"
