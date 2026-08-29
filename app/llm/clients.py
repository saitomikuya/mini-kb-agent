"""Provider adapters behind the single application ModelClient protocol."""

from __future__ import annotations

from base64 import b64encode
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import json
import re
import time
from typing import Any, Protocol
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from app.llm.types import AzureMode, ProtocolPreference, ProviderType, TestedProtocol
from app.models.model_config import APIProvider, ModelProfile


HttpClientFactory = Callable[[], httpx.AsyncClient]
ModelProgressCallback = Callable[[str, Mapping[str, Any]], Awaitable[None]]


class ModelClientError(RuntimeError):
    """Base adapter failure whose message is safe for an API response or log."""


class ModelProviderError(ModelClientError):
    """A remote request failed without exposing request credentials or content."""


class ModelResponseError(ModelClientError):
    """A remote response did not contain usable model output."""


@dataclass(frozen=True, slots=True)
class TextGeneration:
    text: str
    protocol: TestedProtocol
    latency_ms: int


@dataclass(frozen=True, slots=True)
class JSONGeneration:
    value: dict[str, Any]
    protocol: TestedProtocol
    latency_ms: int
    native_structured_output: bool


class ModelClient(Protocol):
    @property
    def role_prompts(self) -> Mapping[str, str]: ...

    @property
    def context_window(self) -> int | None: ...

    @property
    def max_output_tokens(self) -> int | None: ...

    async def generate_text(
        self,
        prompt: str,
        *,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> TextGeneration: ...

    async def generate_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        json_schema: Mapping[str, Any] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        verbosity: str | None = None,
    ) -> JSONGeneration: ...

    async def generate_json_stream(
        self,
        prompt: str,
        *,
        on_progress: ModelProgressCallback,
        system_prompt: str | None = None,
        json_schema: Mapping[str, Any] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        verbosity: str | None = None,
    ) -> JSONGeneration: ...

    async def generate_multimodal(
        self,
        prompt: str,
        image_bytes: bytes,
        *,
        image_media_type: str = "image/png",
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> TextGeneration: ...


_DEFAULT_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
}


class _OpenAIProtocolClient:
    """Shared Responses/Chat Completions implementation."""

    def __init__(
        self,
        provider: APIProvider,
        profile: ModelProfile,
        api_key: str,
        *,
        http_client_factory: HttpClientFactory | None = None,
        role_prompts: Mapping[str, str] | None = None,
    ) -> None:
        self.provider = provider
        self.profile = profile
        self._api_key = api_key
        self._http_client_factory = http_client_factory or (
            lambda: httpx.AsyncClient(
                timeout=httpx.Timeout(120.0, connect=30.0)
            )
        )
        self.role_prompts = dict(role_prompts or {})
        self._resolved_protocol: TestedProtocol | None = None

    @property
    def resolved_protocol(self) -> TestedProtocol | None:
        return self._resolved_protocol

    @property
    def context_window(self) -> int | None:
        return self.profile.context_window

    @property
    def max_output_tokens(self) -> int | None:
        return self.profile.max_output_tokens

    async def generate_text(
        self,
        prompt: str,
        *,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> TextGeneration:
        return await self._generate(
            prompt=prompt,
            system_prompt=None,
            image_data_uri=None,
            native_json_schema=None,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            verbosity=None,
        )

    async def generate_json(
        self,
        prompt: str,
        *,
        system_prompt: str | None = None,
        json_schema: Mapping[str, Any] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        verbosity: str | None = None,
    ) -> JSONGeneration:
        schema = dict(json_schema or _DEFAULT_JSON_SCHEMA)
        try:
            native = await self._generate(
                prompt=prompt,
                system_prompt=system_prompt,
                image_data_uri=None,
                native_json_schema=schema,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                verbosity=verbosity,
            )
            return JSONGeneration(
                value=_parse_json_object(native.text),
                protocol=native.protocol,
                latency_ms=native.latency_ms,
                native_structured_output=True,
            )
        except ModelClientError:
            pass

        schema_text = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        fallback_prompt = (
            f"{prompt}\n\n"
            "The response must conform to this JSON Schema:\n"
            f"{schema_text}\n"
            "Return only one valid JSON object. Do not use Markdown fences or "
            "include explanatory text."
        )
        fallback = await self._generate(
            prompt=fallback_prompt,
            system_prompt=system_prompt,
            image_data_uri=None,
            native_json_schema=None,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            verbosity=verbosity,
        )
        try:
            value = _parse_json_object(fallback.text)
        except ModelResponseError:
            repair_prompt = (
                "Repair the malformed response below into exactly one valid JSON "
                "object conforming to the supplied schema. Preserve its intended "
                "values, add no commentary, and do not use Markdown fences.\n\n"
                f"JSON Schema:\n{schema_text}\n\n"
                "Malformed response:\n"
                f"{fallback.text}"
            )
            repaired = await self._generate(
                prompt=repair_prompt,
                system_prompt=system_prompt,
                image_data_uri=None,
                native_json_schema=None,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                verbosity=verbosity,
            )
            value = _parse_json_object(repaired.text)
            return JSONGeneration(
                value=value,
                protocol=repaired.protocol,
                latency_ms=fallback.latency_ms + repaired.latency_ms,
                native_structured_output=False,
            )
        return JSONGeneration(
            value=value,
            protocol=fallback.protocol,
            latency_ms=fallback.latency_ms,
            native_structured_output=False,
        )

    async def generate_json_stream(
        self,
        prompt: str,
        *,
        on_progress: ModelProgressCallback,
        system_prompt: str | None = None,
        json_schema: Mapping[str, Any] | None = None,
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
        verbosity: str | None = None,
    ) -> JSONGeneration:
        """Stream safe provider progress while preserving the JSON contract.

        Responses reasoning-summary events are explicitly requested and forwarded;
        raw reasoning tokens are never surfaced.  Providers that do not support
        streaming or structured streaming transparently use the existing non-stream
        fallback path.
        """
        schema = dict(json_schema or _DEFAULT_JSON_SCHEMA)
        try:
            native = await self._generate_stream(
                prompt=prompt,
                system_prompt=system_prompt,
                native_json_schema=schema,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                verbosity=verbosity,
                on_progress=on_progress,
            )
            return JSONGeneration(
                value=_parse_json_object(native.text),
                protocol=native.protocol,
                latency_ms=native.latency_ms,
                native_structured_output=True,
            )
        except ModelClientError:
            return await self.generate_json(
                prompt,
                system_prompt=system_prompt,
                json_schema=schema,
                max_output_tokens=max_output_tokens,
                reasoning_effort=reasoning_effort,
                verbosity=verbosity,
            )

    async def generate_multimodal(
        self,
        prompt: str,
        image_bytes: bytes,
        *,
        image_media_type: str = "image/png",
        max_output_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> TextGeneration:
        image_data_uri = (
            f"data:{image_media_type};base64,{b64encode(image_bytes).decode('ascii')}"
        )
        return await self._generate(
            prompt=prompt,
            system_prompt=None,
            image_data_uri=image_data_uri,
            native_json_schema=None,
            max_output_tokens=max_output_tokens,
            reasoning_effort=reasoning_effort,
            verbosity=None,
        )

    async def _generate(
        self,
        *,
        prompt: str,
        system_prompt: str | None,
        image_data_uri: str | None,
        native_json_schema: Mapping[str, Any] | None,
        max_output_tokens: int | None,
        reasoning_effort: str | None,
        verbosity: str | None,
    ) -> TextGeneration:
        last_error: ModelClientError | None = None
        for protocol in self._candidate_protocols():
            started = time.perf_counter()
            try:
                payload = self._build_payload(
                    protocol=protocol,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    image_data_uri=image_data_uri,
                    native_json_schema=native_json_schema,
                    max_output_tokens=max_output_tokens,
                    reasoning_effort=reasoning_effort,
                    verbosity=verbosity,
                )
                response_json = await self._post(protocol, payload)
                text = _extract_text(response_json, protocol)
                if not text.strip():
                    raise ModelResponseError("Model response contained no text")
            except ModelClientError as exc:
                last_error = exc
                continue

            elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
            self._resolved_protocol = protocol
            return TextGeneration(
                text=text,
                protocol=protocol,
                latency_ms=elapsed_ms,
            )

        if last_error is not None:
            raise last_error
        raise ModelProviderError("No compatible generation protocol is configured")

    async def _generate_stream(
        self,
        *,
        prompt: str,
        system_prompt: str | None,
        native_json_schema: Mapping[str, Any] | None,
        max_output_tokens: int | None,
        reasoning_effort: str | None,
        verbosity: str | None,
        on_progress: ModelProgressCallback,
    ) -> TextGeneration:
        last_error: ModelClientError | None = None
        for protocol in self._candidate_protocols():
            started = time.perf_counter()
            try:
                payload = self._build_payload(
                    protocol=protocol,
                    prompt=prompt,
                    system_prompt=system_prompt,
                    image_data_uri=None,
                    native_json_schema=native_json_schema,
                    max_output_tokens=max_output_tokens,
                    reasoning_effort=reasoning_effort,
                    verbosity=verbosity,
                )
                payload["stream"] = True
                if protocol is TestedProtocol.RESPONSES:
                    reasoning = dict(payload.get("reasoning") or {})
                    reasoning["summary"] = "auto"
                    payload["reasoning"] = reasoning
                text = await self._post_stream(protocol, payload, on_progress)
                if not text.strip():
                    raise ModelResponseError("Model response contained no text")
            except ModelClientError as exc:
                last_error = exc
                continue

            elapsed_ms = max(0, round((time.perf_counter() - started) * 1000))
            self._resolved_protocol = protocol
            return TextGeneration(
                text=text,
                protocol=protocol,
                latency_ms=elapsed_ms,
            )

        if last_error is not None:
            raise last_error
        raise ModelProviderError("No compatible generation protocol is configured")

    def _candidate_protocols(self) -> tuple[TestedProtocol, ...]:
        if self._resolved_protocol is not None:
            return (self._resolved_protocol,)
        if self._is_azure_legacy:
            return (TestedProtocol.CHAT_COMPLETIONS,)

        raw_preference = self.profile.protocol_override or self.provider.protocol_preference
        preference = ProtocolPreference(raw_preference)
        if preference is ProtocolPreference.RESPONSES:
            return (TestedProtocol.RESPONSES,)
        if preference is ProtocolPreference.CHAT_COMPLETIONS:
            return (TestedProtocol.CHAT_COMPLETIONS,)
        return (TestedProtocol.RESPONSES, TestedProtocol.CHAT_COMPLETIONS)

    @property
    def _is_azure_legacy(self) -> bool:
        return (
            self.provider.provider_type == ProviderType.AZURE_OPENAI
            and self.provider.azure_mode == AzureMode.LEGACY
        )

    def _build_payload(
        self,
        *,
        protocol: TestedProtocol,
        prompt: str,
        system_prompt: str | None,
        image_data_uri: str | None,
        native_json_schema: Mapping[str, Any] | None,
        max_output_tokens: int | None,
        reasoning_effort: str | None,
        verbosity: str | None,
    ) -> dict[str, Any]:
        output_limit = max_output_tokens or self.profile.max_output_tokens
        effort = reasoning_effort or self.profile.reasoning_effort
        extra_request = dict(self.profile.extra_request_json or {})

        if protocol is TestedProtocol.RESPONSES:
            if image_data_uri is None:
                input_value: Any = prompt
            else:
                input_value = [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": prompt},
                            {"type": "input_image", "image_url": image_data_uri},
                        ],
                    }
                ]
            core: dict[str, Any] = {
                "model": self.profile.remote_model_name,
                "input": input_value,
                "store": False,
            }
            if system_prompt is not None:
                core["instructions"] = system_prompt
            if output_limit is not None:
                core["max_output_tokens"] = output_limit
            if effort is not None:
                core["reasoning"] = {"effort": effort}
            text_options: dict[str, Any] = {}
            if verbosity is not None:
                text_options["verbosity"] = verbosity
            if native_json_schema is not None:
                text_options["format"] = {
                    "type": "json_schema",
                    "name": "model_response",
                    "strict": True,
                    "schema": dict(native_json_schema),
                }
            if text_options:
                core["text"] = text_options
            return {**extra_request, **core}

        if image_data_uri is None:
            message_content: Any = prompt
        else:
            message_content = [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": image_data_uri}},
            ]
        messages = []
        if system_prompt is not None:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": message_content})
        core = {
            "model": self.profile.remote_model_name,
            "messages": messages,
        }
        if output_limit is not None:
            core["max_tokens"] = output_limit
        if effort is not None:
            core["reasoning_effort"] = effort
        if native_json_schema is not None:
            core["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "model_response",
                    "strict": True,
                    "schema": dict(native_json_schema),
                },
            }
        return {**extra_request, **core}

    async def _post(
        self,
        protocol: TestedProtocol,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        url, params = self._request_target(protocol)
        headers = self._request_headers()
        try:
            async with self._http_client_factory() as client:
                response = await client.post(
                    url,
                    params=params,
                    headers=headers,
                    json=dict(payload),
                )
        except httpx.HTTPError as exc:
            raise ModelProviderError("Model provider request could not be completed") from exc

        if not response.is_success:
            raise ModelProviderError(
                f"Model provider request failed with HTTP {response.status_code}"
            )
        try:
            parsed = response.json()
        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
            raise ModelResponseError("Model provider returned invalid JSON") from exc
        if not isinstance(parsed, Mapping):
            raise ModelResponseError("Model provider returned an invalid response object")
        return parsed

    async def _post_stream(
        self,
        protocol: TestedProtocol,
        payload: Mapping[str, Any],
        on_progress: ModelProgressCallback,
    ) -> str:
        url, params = self._request_target(protocol)
        headers = {**self._request_headers(), "Accept": "text/event-stream"}
        output_parts: list[str] = []
        output_length = 0
        reasoning_summary = ""
        published_summary_length = 0
        next_output_report = 240
        completed_response: Mapping[str, Any] | None = None
        try:
            async with self._http_client_factory() as client:
                async with client.stream(
                    "POST",
                    url,
                    params=params,
                    headers=headers,
                    json=dict(payload),
                ) as response:
                    if not response.is_success:
                        raise ModelProviderError(
                            "Model provider request failed with HTTP "
                            f"{response.status_code}"
                        )
                    content_type = response.headers.get("content-type", "").lower()
                    if "text/event-stream" not in content_type:
                        try:
                            parsed = json.loads(await response.aread())
                        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
                            raise ModelResponseError(
                                "Model provider returned invalid JSON"
                            ) from exc
                        if not isinstance(parsed, Mapping):
                            raise ModelResponseError(
                                "Model provider returned an invalid response object"
                            )
                        return _extract_text(parsed, protocol)
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw_event = line[5:].strip()
                        if not raw_event or raw_event == "[DONE]":
                            continue
                        try:
                            event = json.loads(raw_event)
                        except (json.JSONDecodeError, UnicodeError, ValueError) as exc:
                            raise ModelResponseError(
                                "Model provider returned an invalid stream event"
                            ) from exc
                        if not isinstance(event, Mapping):
                            continue
                        event_type = event.get("type")
                        if event_type == "error":
                            raise ModelProviderError("Model provider stream failed")

                        if protocol is TestedProtocol.RESPONSES:
                            if event_type == "response.reasoning_summary_text.delta":
                                delta = event.get("delta")
                                if isinstance(delta, str):
                                    reasoning_summary += delta
                                    if _should_publish_summary(
                                        reasoning_summary,
                                        published_summary_length,
                                        delta,
                                    ):
                                        await on_progress(
                                            "reasoning_summary",
                                            {"summary": reasoning_summary.strip()},
                                        )
                                        published_summary_length = len(reasoning_summary)
                            elif event_type == "response.output_text.delta":
                                delta = event.get("delta")
                                if isinstance(delta, str):
                                    output_parts.append(delta)
                                    output_length += len(delta)
                            elif event_type == "response.completed":
                                response_value = event.get("response")
                                if isinstance(response_value, Mapping):
                                    completed_response = response_value
                        else:
                            output_length += _append_chat_completion_delta(
                                event,
                                output_parts,
                            )

                        if output_length >= next_output_report:
                            await on_progress(
                                "output_progress",
                                {"generated_characters": output_length},
                            )
                            next_output_report = output_length + 240
        except ModelClientError:
            raise
        except httpx.HTTPError as exc:
            raise ModelProviderError(
                "Model provider request could not be completed"
            ) from exc

        if reasoning_summary and published_summary_length < len(reasoning_summary):
            await on_progress(
                "reasoning_summary",
                {"summary": reasoning_summary.strip()},
            )
        text = "".join(output_parts)
        if not text and completed_response is not None:
            text = _extract_text(completed_response, protocol)
        if not text:
            raise ModelResponseError("Model provider stream contained no text")
        return text

    def _request_headers(self) -> dict[str, str]:
        extra_headers = {
            name: value
            for name, value in dict(self.provider.extra_headers_json or {}).items()
            if name.lower()
            not in {"authorization", "proxy-authorization", "api-key", "x-api-key"}
        }
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            **extra_headers,
        }
        if self.provider.provider_type == ProviderType.AZURE_OPENAI:
            headers["api-key"] = self._api_key
            headers.pop("Authorization", None)
        else:
            headers["Authorization"] = f"Bearer {self._api_key}"
        return headers

    def _request_target(
        self,
        protocol: TestedProtocol,
    ) -> tuple[str, dict[str, str] | None]:
        path = "responses" if protocol is TestedProtocol.RESPONSES else "chat/completions"
        return f"{self.provider.base_url.rstrip('/')}/{path}", None


class OpenAICompatibleClient(_OpenAIProtocolClient):
    """OpenAI-compatible adapter with actual Responses-to-Chat probing."""


class Sub2APIClient(OpenAICompatibleClient):
    """Distinct provider type using the OpenAI-compatible protocol core."""


class AzureOpenAIClient(_OpenAIProtocolClient):
    """Azure v1 and legacy deployment-aware adapter."""

    def _request_target(
        self,
        protocol: TestedProtocol,
    ) -> tuple[str, dict[str, str] | None]:
        if self.provider.azure_mode == AzureMode.LEGACY:
            root = _normalize_azure_legacy_root(self.provider.base_url)
            deployment = quote(self.profile.remote_model_name, safe="")
            api_version = self.provider.azure_api_version
            if not api_version:
                raise ModelProviderError(
                    "Azure legacy provider requires an API version"
                )
            return (
                f"{root}/openai/deployments/{deployment}/chat/completions",
                {"api-version": api_version},
            )

        base_url = normalize_azure_v1_url(self.provider.base_url)
        path = "responses" if protocol is TestedProtocol.RESPONSES else "chat/completions"
        return f"{base_url}{path}", None


def _should_publish_summary(
    summary: str,
    published_length: int,
    latest_delta: str,
) -> bool:
    unpublished_length = len(summary) - published_length
    return unpublished_length >= 80 or (
        unpublished_length >= 24
        and any(mark in latest_delta for mark in ("\n", "。", "！", "？", ". "))
    )


def _append_chat_completion_delta(
    event: Mapping[str, Any],
    output_parts: list[str],
) -> int:
    choices = event.get("choices")
    if not isinstance(choices, list):
        return 0
    added = 0
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            continue
        content = delta.get("content")
        if isinstance(content, str):
            output_parts.append(content)
            added += len(content)
        elif isinstance(content, list):
            text = _collect_text_items(content)
            if text:
                output_parts.append(text)
                added += len(text)
    return added


def build_model_client(
    provider: APIProvider,
    profile: ModelProfile,
    api_key: str,
    *,
    http_client_factory: HttpClientFactory | None = None,
    role_prompts: Mapping[str, str] | None = None,
) -> ModelClient:
    provider_type = ProviderType(provider.provider_type)
    client_class: type[_OpenAIProtocolClient]
    if provider_type is ProviderType.OPENAI_COMPATIBLE:
        client_class = OpenAICompatibleClient
    elif provider_type is ProviderType.AZURE_OPENAI:
        client_class = AzureOpenAIClient
    elif provider_type is ProviderType.SUB2API:
        client_class = Sub2APIClient
    else:  # pragma: no cover - database and enum validation make this unreachable.
        raise ModelProviderError("Unsupported model provider type")
    return client_class(
        provider,
        profile,
        api_key,
        http_client_factory=http_client_factory,
        role_prompts=role_prompts,
    )


def normalize_azure_v1_url(base_url: str) -> str:
    """Return the canonical Azure OpenAI v1 endpoint with a trailing slash."""
    split = urlsplit(base_url.strip())
    path = split.path.rstrip("/")
    marker = "/openai/v1"
    marker_at = path.lower().find(marker)
    if marker_at >= 0:
        path = path[: marker_at + len(marker)]
    else:
        path = f"{path}{marker}"
    return urlunsplit((split.scheme, split.netloc, f"{path}/", "", ""))


def _normalize_azure_legacy_root(base_url: str) -> str:
    split = urlsplit(base_url.strip())
    path = split.path.rstrip("/")
    for suffix in ("/openai/v1", "/openai"):
        if path.lower().endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((split.scheme, split.netloc, path, "", "")).rstrip("/")


def _extract_text(
    response: Mapping[str, Any],
    protocol: TestedProtocol,
) -> str:
    if protocol is TestedProtocol.CHAT_COMPLETIONS:
        choices = response.get("choices")
        if isinstance(choices, list) and choices:
            choice = choices[0]
            if isinstance(choice, Mapping):
                message = choice.get("message")
                if isinstance(message, Mapping):
                    content = message.get("content")
                    if isinstance(content, str):
                        return content
                    if isinstance(content, list):
                        collected = _collect_text_items(content)
                        if collected:
                            return collected
        raise ModelResponseError("Chat Completions response contained no text")

    output_text = response.get("output_text")
    if isinstance(output_text, str):
        return output_text
    output = response.get("output")
    if isinstance(output, list):
        collected: list[str] = []
        for item in output:
            if not isinstance(item, Mapping):
                continue
            content = item.get("content")
            if isinstance(content, list):
                text = _collect_text_items(content)
                if text:
                    collected.append(text)
        if collected:
            return "\n".join(collected)
    raise ModelResponseError("Responses API response contained no text")


def _collect_text_items(items: list[Any]) -> str:
    collected: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        for key in ("text", "output_text"):
            value = item.get(key)
            if isinstance(value, str):
                collected.append(value)
                break
    return "\n".join(collected)


def _parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```",
        stripped,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        stripped = fenced.group(1).strip()

    candidates = [stripped]
    first_brace = stripped.find("{")
    last_brace = stripped.rfind("}")
    if first_brace >= 0 and last_brace > first_brace:
        candidates.append(stripped[first_brace : last_brace + 1])

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, UnicodeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    raise ModelResponseError("Model did not return a valid JSON object")
