import asyncio
import base64
import copy
import io
import json
import traceback
from types import SimpleNamespace

import httpx
import pytest
import respx
from PIL import Image
from pydantic import ValidationError

from assistant.clients import ServiceError
from assistant.config import Config
from assistant.images import VisionClient, normalize
from assistant.provider_config import ProviderRoles
from assistant.providers import ModelProvider, ProviderOutputError, ProviderSettings, ProviderUnavailable
from assistant.schemas import Intent, Vision


def completion(content='{"kind":"search","query":"cables"}', **message_fields):
    return {"choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content,
                                                           **message_fields}}]}


def anthropic_message(content='{"kind":"search","query":"cables"}', **fields):
    return {"type": "message", "role": "assistant", "stop_reason": "end_turn",
            "content": [{"type": "text", "text": content}], **fields}


def generate(provider, schema=None, **kwargs):
    return provider.generate(
        [{"role": "user", "content": "Find cables"}],
        schema or Intent.model_json_schema(), timeout_seconds=kwargs.pop("timeout_seconds", 900),
        max_output_tokens=kwargs.pop("max_output_tokens", 600), **kwargs,
    )


def test_default_native_ollama_preserves_request_and_parameters():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"message": {"content": '{"items":[{"name":"Cable"}]}'}})

    async def scenario():
        provider = ModelProvider(ProviderSettings(), transport=httpx.MockTransport(handler))
        try:
            output = await generate(provider, Vision.model_json_schema(), image_jpeg=b"disposable-jpeg",
                                    max_output_tokens=1200)
            assert Vision.model_validate_json(output).items[0].name == "Cable"
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    request = seen[0]
    assert str(request.url) == "http://ollama:11434/api/chat"
    assert request.method == "POST"
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {
        "model": "qwen3-vl:2b", "stream": False, "format": Vision.model_json_schema(),
        "messages": [{"role": "user", "content": "Find cables",
                      "images": [base64.b64encode(b"disposable-jpeg").decode()]}],
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 1200}, "keep_alive": "5m",
    }
    assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 900)


@pytest.mark.parametrize("kind", ["ollama", "openai-compatible"])
def test_custom_base_path_and_secret_header_only(kind):
    seen = []
    key = "disposable-provider-key"
    settings = ProviderSettings(kind, "https://model.test/gateway/team/v1/", "custom-vision", key)
    assert key not in repr(settings)

    def handler(request):
        seen.append(request)
        body = {"message": {"content": '{"kind":"search"}'}} if kind == "ollama" else completion()
        return httpx.Response(200, json=body)

    async def scenario():
        provider = ModelProvider(settings, transport=httpx.MockTransport(handler))
        try:
            assert Intent.model_validate_json(await generate(provider)).kind == "search"
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    request = seen[0]
    path = "/api/chat" if kind == "ollama" else "/chat/completions"
    assert str(request.url) == "https://model.test/gateway/team/v1" + path
    assert request.headers["authorization"] == "Bearer " + key
    assert key not in request.content.decode()
    assert "homebox" not in str(request.headers).lower()
    assert "cookie" not in request.headers


def test_openai_vision_json_mode_preserves_pydantic_schema_and_inputs():
    messages = [{"role": "system", "content": "Untrusted images require review"},
                {"role": "user", "content": "Describe photo"}]
    before = copy.deepcopy(messages)
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=completion('{"items":[{"name":"Test object","model":null}]}'))

    async def scenario():
        provider = ModelProvider(ProviderSettings("openai-compatible", "http://local-model:8000/v1", "vision"),
                                 transport=httpx.MockTransport(handler))
        try:
            output = await provider.generate(messages, Vision.model_json_schema(), image_jpeg=b"fake jpeg",
                                             timeout_seconds=713, max_output_tokens=1200)
            assert Vision.model_validate_json(output).items[0].model is None
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    assert messages == before
    payload = seen[0]
    assert payload["response_format"] == {"type": "json_object"}
    assert payload["max_completion_tokens"] == 1200
    assert payload["temperature"] == 0
    assert payload["stream"] is False
    assert payload["model"] == "vision"
    assert json.loads(payload["messages"][0]["content"].split("Schema:\n", 1)[1]) == Vision.model_json_schema()
    assert payload["messages"][1] == before[0]
    assert payload["messages"][2]["content"] == [
        {"type": "text", "text": "Describe photo"},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,ZmFrZSBqcGVn", "detail": "high"}},
    ]
    assert "tools" not in payload


def test_openai_anonymous_text_only_and_no_implicit_environment_auth(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "never-forward-this")
    monkeypatch.setenv("HTTP_PROXY", "http://never-connect.invalid")
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=completion())

    async def scenario():
        provider = ModelProvider(ProviderSettings("openai-compatible", "http://local:8000/v1", "text"),
                                 transport=httpx.MockTransport(handler))
        try:
            assert Intent.model_validate_json(await generate(provider)).query == "cables"
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    assert "authorization" not in seen[0].headers
    assert json.loads(seen[0].content)["messages"][-1] == {"role": "user", "content": "Find cables"}


@pytest.mark.parametrize("url", [
    "file:///tmp/model", "ftp://model.test", "https://user:secret@model.test/v1",
    "https://model.test/v1?api_key=secret", "https://model.test/v1#secret",
    "https://model.test/v1?", "https://model.test/v1#", "http://", "//model.test",
    "https://model.test:invalid", "https://model.test:0", "https://model.test:99999",
    "https://model.test\\@evil.test", " https://model.test", "https://model.test/\n",
])
def test_invalid_endpoint_rejected_without_echoing_url(url):
    with pytest.raises(ValueError) as error:
        ProviderSettings(base_url=url)
    assert url not in str(error.value)
    assert "secret" not in str(error.value)


@pytest.mark.parametrize("overrides", [{"kind": "anthropic"}, {"model": ""}, {"model": "  "},
                                        {"api_key": ""}, {"api_key": "key\nleak"}, {"api_key": "a b"}])
def test_invalid_settings_rejected_safely(overrides):
    with pytest.raises(ValueError):
        ProviderSettings(**overrides)


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 401, 403, 404, 413, 429, 500, 503])
@pytest.mark.parametrize("kind", ["ollama", "openai-compatible", "anthropic-messages"])
def test_http_failures_redirects_do_not_leak_key_or_retry(status, kind):
    calls = []
    key = "disposable-secret-for-failure"

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(status, json={"error": key}, headers={"Location": "https://redirect.test/steal"})

    async def scenario():
        provider = ModelProvider(ProviderSettings(kind, "https://provider.test/v1", "vision", key),
                                 transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ProviderUnavailable) as error:
                await generate(provider)
            rendered = "".join(traceback.format_exception(error.value))
            assert key not in rendered and "redirect.test" not in rendered and "provider.test" not in rendered
            assert key not in repr(error.value)
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.ConnectError])
@pytest.mark.parametrize("kind", ["ollama", "anthropic-messages"])
def test_transport_failure_is_safe_and_not_retried(error_type, kind):
    calls = []

    def handler(request):
        calls.append(request)
        raise error_type("disposable-secret-and-upstream-details", request=request)

    async def scenario():
        provider = ModelProvider(ProviderSettings(kind), transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ProviderUnavailable) as error:
                await generate(provider)
            assert "disposable-secret" not in "".join(traceback.format_exception(error.value))
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    assert len(calls) == 1


@pytest.mark.parametrize("data", [
    None, [], {}, {"choices": []}, {"choices": [None]},
    {"choices": [{"finish_reason": "length", "message": {"content": '{"kind":"search"}'}}]},
    {"choices": [{"finish_reason": "content_filter", "message": {"content": '{"kind":"search"}'}}]},
    {"choices": [{"message": {"content": '{"kind":"search"}'}}]},
    completion(refusal="Sensitive refusal text"),
    completion(tool_calls=[{"function": {"name": "delete_all", "arguments": "{}"}}]),
    completion(function_call={"name": "delete_all"}),
    completion(None), completion(""), completion("not-json"), completion("[]"), completion("null"),
    completion('{"confidence":NaN}'), completion('{"value":Infinity}'),
    completion('{"kind":"search","kind":"move"}'), completion("```json\n{}\n```"),
    completion([{"type": "text", "text": "{}"}]),
])
def test_openai_malformed_refused_and_truncated_output_rejected(data):
    async def scenario():
        provider = ModelProvider(ProviderSettings("openai-compatible", "https://model.test/v1", "vision"),
                                 transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)))
        try:
            with pytest.raises(ProviderOutputError):
                await generate(provider)
        finally:
            await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("data", [
    None, {}, {"message": None}, {"message": {"content": "broken"}},
    {"done": False, "message": {"content": "{}"}},
    {"done_reason": "length", "message": {"content": "{}"}},
    {"message": {"content": "{}", "tool_calls": [{"function": {"name": "delete_all"}}]}},
])
def test_ollama_incomplete_or_malformed_output_rejected(data):
    async def scenario():
        provider = ModelProvider(ProviderSettings(),
                                 transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)))
        try:
            with pytest.raises(ProviderOutputError):
                await generate(provider)
        finally:
            await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["ollama", "openai-compatible", "anthropic-messages"])
def test_non_json_http_response_rejected(kind):
    async def scenario():
        provider = ModelProvider(ProviderSettings(kind),
                                 transport=httpx.MockTransport(lambda request: httpx.Response(200, text="secret")))
        try:
            with pytest.raises(ProviderOutputError) as error:
                await generate(provider)
            assert "secret" not in str(error.value)
        finally:
            await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("output", [
    '{"kind":"delete"}', '{"kind":"move","entityId":"malicious-id"}',
    '{"kind":"update","changes":{"id":"malicious-id"}}',
    '{"kind":"search","tools":[{"name":"delete_all"}]}',
])
def test_provider_json_never_bypasses_existing_intent_validation(output):
    async def scenario():
        provider = ModelProvider(ProviderSettings("openai-compatible"),
                                 transport=httpx.MockTransport(lambda request: httpx.Response(200, json=completion(output))))
        try:
            with pytest.raises(ValidationError):
                Intent.model_validate_json(await generate(provider))
        finally:
            await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_expired_or_invalid_deadline_does_not_send(seconds):
    async def scenario():
        provider = ModelProvider(ProviderSettings(), transport=httpx.MockTransport(
            lambda request: pytest.fail("Expired request must not reach a provider")))
        try:
            with pytest.raises(ProviderUnavailable):
                await generate(provider, timeout_seconds=seconds)
        finally:
            await provider.aclose()

    asyncio.run(scenario())


def test_client_enforces_tls_redirect_and_environment_policy_and_closes(monkeypatch):
    original = httpx.AsyncClient
    options = {}

    def client(**kwargs):
        options.update(kwargs)
        kwargs["transport"] = httpx.MockTransport(lambda request: httpx.Response(200, json=completion()))
        return original(**kwargs)

    monkeypatch.setattr("assistant.providers.httpx.AsyncClient", client)

    async def scenario():
        provider = ModelProvider(ProviderSettings("openai-compatible"))
        await generate(provider)
        await provider.aclose()
        assert provider._client.is_closed

    asyncio.run(scenario())
    assert options["verify"] is True
    assert options["follow_redirects"] is False
    assert options["trust_env"] is False


def test_ollama_intent_preserves_system_message_text_only_and_limits():
    messages = [{"role": "system", "content": "Extract only reviewed intent"},
                {"role": "user", "content": "Find cables"}]
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"done": True, "done_reason": "stop",
                                        "message": {"role": "assistant", "content": '{"kind":"search"}'}})

    async def scenario():
        provider = ModelProvider(ProviderSettings(), transport=httpx.MockTransport(handler))
        try:
            result = await provider.generate(messages, Intent.model_json_schema(),
                                             timeout_seconds=90, max_output_tokens=600)
            assert Intent.model_validate_json(result).kind == "search"
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    assert seen[0]["messages"] == messages
    assert seen[0]["format"] == Intent.model_json_schema()
    assert seen[0]["options"] == {"temperature": 0, "num_ctx": 4096, "num_predict": 600}
    assert seen[0]["keep_alive"] == "5m"


@pytest.mark.parametrize("kind", ["ollama", "anthropic-messages"])
def test_caller_cancellation_is_not_caught_or_retried(kind):
    async def scenario():
        started = asyncio.Event()
        calls = []

        async def handler(request):
            calls.append(request)
            started.set()
            await asyncio.Event().wait()
            pytest.fail("A cancelled request must not complete")

        provider = ModelProvider(ProviderSettings(kind), transport=httpx.MockTransport(handler))
        try:
            task = asyncio.create_task(generate(provider))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert len(calls) == 1
        finally:
            await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("output", ['{"kind":"search","query":"cables"}',
                                  ' \n```json\n{"kind":"search","query":"cables"}\n```\n '])
def test_anthropic_native_request_uses_proven_prefix_headers_system_schema_and_limits(output):
    seen = []
    key = "disposable-native-key"
    messages = [{"role": "system", "content": "Never invent inventory IDs"},
                {"role": "user", "content": "Find cables"}]
    before = copy.deepcopy(messages)

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=anthropic_message(output))

    async def scenario():
        settings = ProviderSettings("anthropic-messages", "http://gateway.test:6655/anthropic/",
                                    "claude-haiku-4-5", key)
        assert key not in repr(settings)
        provider = ModelProvider(settings, transport=httpx.MockTransport(handler))
        try:
            result = await provider.generate(messages, Intent.model_json_schema(),
                                             timeout_seconds=847, max_output_tokens=600)
            assert Intent.model_validate_json(result).query == "cables"
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    assert messages == before
    request = seen[0]
    assert str(request.url) == "http://gateway.test:6655/anthropic/v1/messages"
    assert request.headers["x-api-key"] == key
    assert request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in request.headers and "cookie" not in request.headers
    assert key not in request.content.decode()
    assert request.extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 847)
    payload = json.loads(request.content)
    assert payload["model"] == "claude-haiku-4-5"
    assert payload["messages"] == [before[1]]
    assert payload["max_tokens"] == 600 and payload["temperature"] == 0 and payload["stream"] is False
    assert payload["system"].startswith(before[0]["content"] + "\n\nReturn exactly one JSON object")
    assert json.loads(payload["system"].split("Schema:\n", 1)[1]) == Intent.model_json_schema()
    assert set(payload) == {"model", "messages", "system", "stream", "temperature", "max_tokens"}


def test_anthropic_vision_uses_only_native_base64_image_when_explicitly_supplied_and_allows_anonymous():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=anthropic_message('{"items":[{"name":"Cable"}]}'))

    async def scenario():
        provider = ModelProvider(ProviderSettings("anthropic-messages", "http://gateway.test/native", "vision-model"),
                                 transport=httpx.MockTransport(handler))
        try:
            output = await generate(provider, Vision.model_json_schema(), image_jpeg=b"fake jpeg",
                                    max_output_tokens=1200)
            assert Vision.model_validate_json(output).items[0].name == "Cable"
        finally:
            await provider.aclose()

    asyncio.run(scenario())
    assert "x-api-key" not in seen[0].headers and "authorization" not in seen[0].headers
    payload = json.loads(seen[0].content)
    assert payload["max_tokens"] == 1200
    assert payload["messages"] == [{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": "ZmFrZSBqcGVn"}},
        {"type": "text", "text": "Find cables"},
    ]}]
    assert json.loads(payload["system"].split("Schema:\n", 1)[1]) == Vision.model_json_schema()


@pytest.mark.parametrize("data", [
    None, {}, anthropic_message(stop_reason="max_tokens"), anthropic_message(stop_reason="tool_use"),
    anthropic_message(stop_reason="refusal"), anthropic_message(stop_reason=None),
    anthropic_message(role="user"), anthropic_message(type="error"), anthropic_message(content=[]),
    anthropic_message(content=[{"type": "tool_use", "name": "delete_all", "input": {}}]),
    anthropic_message(content=[{"type": "text", "text": "{}"}, {"type": "tool_use", "name": "delete_all"}]),
    anthropic_message(content=[{"type": "thinking", "thinking": "{}"}]),
    anthropic_message(content=[{"type": "text", "text": None}]),
    anthropic_message(refusal="Refused"), anthropic_message(tool_calls=[{"name": "delete_all"}]),
    anthropic_message('Here is the JSON:\n```json\n{"kind":"search"}\n```'),
    anthropic_message('```json\n{"kind":"search"}\n```\nExplanation'),
    anthropic_message('```json\n{"kind":"search"}\n```\n```json\n{}\n```'),
    anthropic_message('```json\n{"kind":"search"}'), anthropic_message('{"kind":"search"'),
    anthropic_message('{"kind":"search"} {"kind":"move"}'), anthropic_message('{"kind":"search","kind":"move"}'),
])
def test_anthropic_refusal_tools_truncation_and_ambiguous_output_fail_closed(data):
    async def scenario():
        provider = ModelProvider(ProviderSettings("anthropic-messages", "http://gateway.test/native", "chat-model"),
                                 transport=httpx.MockTransport(lambda request: httpx.Response(200, json=data)))
        try:
            with pytest.raises(ProviderOutputError):
                await generate(provider)
        finally:
            await provider.aclose()

    asyncio.run(scenario())


def test_anthropic_fenced_json_does_not_bypass_strict_intent_validation():
    output = '```json\n{"kind":"move","entityId":"malicious-id"}\n```'

    async def scenario():
        provider = ModelProvider(ProviderSettings("anthropic-messages", "http://gateway.test/native", "chat-model"),
                                 transport=httpx.MockTransport(lambda request: httpx.Response(
                                     200, json=anthropic_message(output))))
        try:
            with pytest.raises(ValidationError):
                Intent.model_validate_json(await generate(provider))
        finally:
            await provider.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["bare", "whole_fence", "strict_repair"])
@respx.mock
def test_native_vision_real_client_schema_repair_shared_provider_and_close_once(tmp_path, monkeypatch, mode):
    cfg = Config.load()
    cfg.state = tmp_path / "synthetic-state.db"
    cfg.uploads = tmp_path / "synthetic-uploads"
    settings = ProviderSettings("anthropic-messages", "http://synthetic-gateway.test/anthropic",
                                "claude-haiku-4-5", "synthetic-key")
    cfg.ai_roles = ProviderRoles(chat=settings, vision=settings)
    client = VisionClient(cfg)
    assert client.chat_provider is client.provider
    closed = []
    original_close = client.provider.aclose

    async def close():
        closed.append(True)
        await original_close()

    monkeypatch.setattr(client.provider, "aclose", close)
    raw = io.BytesIO()
    Image.new("RGB", (1400, 700), "blue").save(raw, format="PNG")
    jpeg = normalize(raw.getvalue(), cfg)
    with Image.open(io.BytesIO(jpeg)) as image:
        assert image.format == "JPEG" and image.size == (1024, 512)
    valid = '{"items":[{"name":"Synthetic blue box","manufacturer":null,"confidence":0.9}],"image_notes":"Synthetic"}'
    contents = {
        "bare": [valid],
        "whole_fence": ["```json\n" + valid + "\n```"],
        "strict_repair": ['{"items":[{"name":"Synthetic blue box"}],"skip_review":true}', valid],
    }[mode]
    route = respx.post(settings.base_url + "/v1/messages").mock(side_effect=[
        httpx.Response(200, json=anthropic_message(content)) for content in contents
    ])

    async def scenario():
        try:
            result = await client.analyze(jpeg, "Synthetic image test only")
            assert result.items[0].name == "Synthetic blue box"
            assert result.items[0].manufacturer is None
            assert result.items[0].needs_user_review is True
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert closed == [True]
    assert route.call_count == len(contents)
    payloads = [json.loads(call.request.content) for call in route.calls]
    for payload in payloads:
        assert payload["model"] == "claude-haiku-4-5"
        assert payload["max_tokens"] == 1200 and payload["temperature"] == 0
        assert payload["stream"] is False
        assert json.loads(payload["system"].split("Schema:\n", 1)[1]) == Vision.model_json_schema()
        assert payload["messages"][0]["content"][0] == {
            "type": "image", "source": {
                "type": "base64", "media_type": "image/jpeg", "data": base64.b64encode(jpeg).decode(),
            },
        }
        assert payload["messages"][0]["content"][1]["type"] == "text"
        assert "tools" not in payload and "tool_choice" not in payload
    if mode == "strict_repair":
        assert payloads[0]["messages"] == payloads[1]["messages"]
        assert payloads[0]["system"] != payloads[1]["system"]
        assert "Return only one JSON object that exactly matches the requested schema." in payloads[1]["system"]


def intent_config(tmp_path, timeout):
    cfg = Config.load()
    cfg.state = tmp_path / "synthetic-intent.db"
    cfg.chat_timeout = timeout
    settings = ProviderSettings("ollama", "http://synthetic-ollama.test:11434", "minicpm-v4.6")
    cfg.ai_roles = ProviderRoles(chat=settings, vision=settings)
    return cfg


@pytest.mark.parametrize("budget", [60, 120])
@respx.mock
def test_intent_uses_configured_chat_budget(tmp_path, monkeypatch, budget):
    monkeypatch.setattr("assistant.images.time", SimpleNamespace(monotonic=lambda: 0))
    client = VisionClient(intent_config(tmp_path, budget))
    route = respx.post(client.chat_provider.settings.base_url + "/api/chat").respond(
        200, json={"message": {"content": '{"kind":"search","query":"cables"}'}, "done": True},
    )

    async def scenario():
        try:
            assert (await client.intent("Find my cables")).kind == "search"
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert route.calls[0].request.extensions["timeout"] == dict.fromkeys(
        ("connect", "read", "write", "pool"), budget,
    )


@pytest.mark.parametrize("first,repair,succeeds", [(30, 31, True), (50, 50, False)])
@respx.mock
def test_intent_queue_and_repair_share_configured_budget(tmp_path, monkeypatch, first, repair, succeeds):
    clock, remaining = [0], []
    monkeypatch.setattr("assistant.images.time", SimpleNamespace(monotonic=lambda: clock[0]))
    client = VisionClient(intent_config(tmp_path, 120))
    assert client.chat_provider is client.provider

    def response(request):
        timeout = request.extensions["timeout"]["read"]
        remaining.append(timeout)
        duration = first if len(remaining) == 1 else repair
        if duration > timeout:
            raise httpx.ReadTimeout("Synthetic remaining-budget failure", request=request)
        clock[0] += duration
        content = '{"kind":"search","unexpected":true}' if len(remaining) == 1 else '{"kind":"search","query":"cables"}'
        return httpx.Response(200, json={"message": {"content": content}, "done": True})

    route = respx.post(client.chat_provider.settings.base_url + "/api/chat").mock(side_effect=response)

    async def scenario():
        await client.lock.acquire()
        task = asyncio.create_task(client.intent("Find my cables"))
        try:
            await asyncio.sleep(0)
            assert not task.done() and not route.called
            clock[0] += 30
            client.lock.release()
            if succeeds:
                assert (await task).kind == "search"
            else:
                with pytest.raises(ServiceError) as error:
                    await task
                assert error.value.code == "model_unavailable"
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose()

    asyncio.run(scenario())
    assert remaining == [90, 90 - first]
    assert route.call_count == 2
    assert clock[0] == (30 + first + repair if succeeds else 30 + first)


@respx.mock
def test_intent_configured_deadline_includes_resource_wait(tmp_path):
    client = VisionClient(intent_config(tmp_path, .02))
    route = respx.post(client.chat_provider.settings.base_url + "/api/chat").respond(
        200, json={"message": {"content": '{"kind":"search"}'}, "done": True},
    )

    async def scenario():
        await client.lock.acquire()
        try:
            with pytest.raises(ServiceError) as error:
                await asyncio.wait_for(client.intent("Find my cables"), .2)
            assert error.value.code == "model_unavailable"
            assert not route.called
        finally:
            client.lock.release()
            await client.aclose()

    asyncio.run(scenario())


@respx.mock
def test_intent_obeys_elapsed_outer_deadline_and_releases_resource(tmp_path):
    client = VisionClient(intent_config(tmp_path, 120))
    cancelled, attempted = [], []

    async def response(request):
        attempted.append(request)
        if not cancelled:
            try:
                await asyncio.sleep(1)
            finally:
                cancelled.append(True)
        return httpx.Response(200, json={"message": {"content": '{"kind":"search","query":"cables"}'}, "done": True})

    route = respx.post(client.chat_provider.settings.base_url + "/api/chat").mock(side_effect=response)

    async def scenario():
        try:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(.03):
                    await asyncio.sleep(.015)
                    await client.intent("Find my cables")
            assert cancelled == [True]
            assert (await asyncio.wait_for(client.intent("Find my cables"), .2)).kind == "search"
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert len(attempted) == 2
