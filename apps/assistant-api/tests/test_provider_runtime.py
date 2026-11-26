import asyncio
import json
import pickle
from dataclasses import replace

import httpx
import pytest
import respx

from assistant.ai_settings import AISnapshot
from assistant.clients import ServiceError
from assistant.images import VisionClient, normalize, resource_identity
from assistant.provider_config import ProviderRoles
from assistant.provider_runtime import ProviderSnapshot, chat_configuration, snapshot_for, vision_configuration
from assistant.providers import ModelProvider, ProviderSettings
from test_ollama_minicpm import (
    ITEM, MESSAGE, clear_provider_environment, minicpm_config, structured_value, synthetic_photo,
)


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    clear_provider_environment(monkeypatch)
    return minicpm_config(monkeypatch, tmp_path)


def snapshot(chat, vision=None, chat_token="chat-synthetic-version", vision_token="vision-synthetic-version"):
    return AISnapshot(ProviderRoles(chat, vision or chat), chat_token, vision_token)


def reply(kind, value):
    content = json.dumps(value)
    if kind == "ollama":
        data = {"done": True, "done_reason": "stop", "message": {"content": content}}
    elif kind == "openai-compatible":
        data = {"choices": [{"finish_reason": "stop", "message": {"content": content}}]}
    elif kind == "anthropic-messages":
        data = {"type": "message", "role": "assistant", "stop_reason": "end_turn",
                "content": [{"type": "text", "text": content}]}
    else:
        data = {"candidates": [{"finishReason": "STOP",
                               "content": {"role": "model", "parts": [{"text": content}]}}]}
    return httpx.Response(200, json=data)


def endpoint(role):
    return role.base_url + {
        "ollama": "/api/chat", "openai-compatible": "/chat/completions",
        "anthropic-messages": "/v1/messages",
        "gemini": "/models/" + role.model + ":generateContent",
    }[role.kind]


def assert_key(request, role):
    headers = ("authorization", "x-api-key", "x-goog-api-key")
    expected = ("x-api-key" if role.kind == "anthropic-messages" else
                "x-goog-api-key" if role.kind == "gemini" else "authorization")
    for name in headers:
        assert request.headers.get(name) == (
            ("Bearer " if name == "authorization" else "") + role.api_key
            if role.api_key is not None and name == expected else None)
    assert "cookie" not in request.headers


def test_canonical_snapshot_and_unchanged_default_fingerprints_and_picklable_config(cfg):
    assert ProviderSnapshot is AISnapshot
    before = pickle.dumps(cfg)
    value = snapshot_for(None, cfg)
    assert value.chat_token is value.vision_token is None
    assert chat_configuration(value) == resource_identity(cfg.chat_provider_settings) + ":" + cfg.chat_provider_settings.model
    assert json.loads(vision_configuration(value)) == {
        "provider": cfg.vision_provider_settings.kind, "model": cfg.vision_provider_settings.model,
        "baseUrlFingerprint": resource_identity(cfg.vision_provider_settings)}
    assert pickle.dumps(cfg) == before
    assert "ai_settings" not in vars(cfg)


def test_nested_scope_reads_once_and_new_operation_observes_save(cfg):
    first = snapshot(cfg.chat_provider_settings)
    second = replace(first, chat_token="second-chat-version")
    current, reads = [first], []

    def load():
        reads.append(True)
        return current[0]

    client = VisionClient(cfg, settings_snapshot=load)

    async def scenario():
        try:
            with client.operation():
                assert client.current_snapshot() is first
                current[0] = second
                with client.operation():
                    assert client.current_snapshot() is first
                assert client.current_snapshot() is first
            with client.operation():
                assert client.current_snapshot() is second
            assert len(reads) == 2
        finally:
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("role_name", ["chat", "vision"])
@pytest.mark.parametrize("kind", ["ollama", "openai-compatible", "anthropic-messages", "gemini"])
@respx.mock
def test_effective_role_transport_and_secret_never_inherit_startup_cloud(cfg, role_name, kind):
    startup = ProviderSettings("anthropic-messages", "https://startup-cloud.test/native",
                               "claude-haiku-4-5", "synthetic-startup-cloud-key")
    cfg = replace(cfg, ai_roles=ProviderRoles(startup, startup))
    selected = ProviderSettings(kind, ("http" if kind == "ollama" else "https") + "://selected-provider.test/native",
                                "selected-model", None if kind == "ollama" else "synthetic-selected-key")
    roles = {"chat": startup, "vision": startup, role_name: selected}
    value = AISnapshot(ProviderRoles(**roles), "chat-synthetic-token", "vision-synthetic-token")
    client, calls = VisionClient(cfg, settings_snapshot=lambda: value), []

    def handler(request):
        role = roles["chat" if not calls else "vision"]
        assert str(request.url) == endpoint(role)
        assert_key(request, role)
        if role is selected:
            assert startup.api_key not in str(request.headers)
        assert startup.api_key not in request.content.decode()
        calls.append(request)
        return reply(role.kind, structured_value(len(calls) == 2))

    respx.route().mock(side_effect=handler)

    async def scenario():
        try:
            assert (await client.intent(MESSAGE)).query == ITEM
            assert (await client.analyze(normalize(synthetic_photo(), cfg), "")).items[0].name == "Synthetic local box"
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert len(calls) == 2 and cfg.ai_roles == ProviderRoles(startup, startup)


@respx.mock
def test_save_during_repair_keeps_endpoint_key_and_closes_override_clients(cfg, monkeypatch):
    a = ProviderSettings("openai-compatible", "https://old-provider.test/v1", "old-model", "synthetic-old-key")
    b = ProviderSettings("gemini", "https://new-provider.test/v1beta", "new-model", "synthetic-new-key")
    current, created, calls = [snapshot(a)], [], []
    original = ModelProvider.__init__

    def record(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(ModelProvider, "__init__", record)
    client = VisionClient(cfg, settings_snapshot=lambda: current[0])

    def handler(request):
        role = a if len(calls) < 2 else b
        assert str(request.url) == endpoint(role)
        assert_key(request, role)
        calls.append(request)
        value = structured_value(False)
        if len(calls) == 1:
            current[0] = snapshot(b)
            value["skip_review"] = True
        return reply(role.kind, value)

    respx.route().mock(side_effect=handler)

    async def scenario():
        try:
            assert (await client.intent(MESSAGE)).query == ITEM
            assert created[-1].settings is a and created[-1]._client.is_closed
            assert (await client.intent(MESSAGE)).query == ITEM
            assert created[-1].settings is b and created[-1]._client.is_closed
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert len(calls) == 3 and all(value._client.is_closed for value in created)


@respx.mock
def test_snapshot_survives_model_queue_wait_without_leaking_to_concurrent_turn(cfg):
    a = ProviderSettings("ollama", "http://old-provider.test:11434", "old-model")
    b = ProviderSettings("ollama", "http://new-provider.test:11434", "new-model")
    current, calls = [snapshot(a)], []

    async def scenario():
        pinned = asyncio.Event()

        def load():
            value = current[0]
            if value.roles.chat is a:
                pinned.set()
            return value

        client = VisionClient(cfg, settings_snapshot=load)
        blocked = asyncio.Semaphore(0)
        client.resource_locks[resource_identity(a)] = blocked

        def handler(request):
            calls.append(str(request.url))
            return reply("ollama", structured_value(False))

        respx.route().mock(side_effect=handler)
        task = asyncio.create_task(client.intent(MESSAGE))
        try:
            await asyncio.wait_for(pinned.wait(), 2)
            current[0] = snapshot(b)
            assert (await client.intent(MESSAGE)).query == ITEM
            assert calls == [endpoint(b)]
            blocked.release()
            assert (await task).query == ITEM
            assert calls == [endpoint(b), endpoint(a)]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["exception", "wrong_type", "service_error"])
@respx.mock
def test_bad_saved_snapshot_fails_closed_without_startup_fallback(cfg, failure):
    def load():
        if failure == "exception":
            raise ValueError("synthetic-sensitive-detail-must-not-escape")
        if failure == "service_error":
            raise ServiceError("AI_SETTINGS_UNAVAILABLE", "Saved settings cannot be read.", 503)
        return cfg.ai_roles

    client = VisionClient(cfg, settings_snapshot=load)
    route = respx.route().mock(return_value=reply("ollama", structured_value(False)))

    async def scenario():
        try:
            with pytest.raises(ServiceError) as error:
                await client.intent(MESSAGE)
            assert error.value.status == 503
            assert "synthetic-sensitive" not in error.value.message
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert route.call_count == 0


@respx.mock
def test_cancelled_override_closes_transport_and_releases_model_lock(cfg, monkeypatch):
    role = ProviderSettings("ollama", "http://cancel-provider.test:11434", "cancel-model")
    created, started = [], []
    original = ModelProvider.__init__

    def record(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(ModelProvider, "__init__", record)
    client = VisionClient(cfg, settings_snapshot=lambda: snapshot(role))

    async def scenario():
        received = asyncio.Event()

        async def handler(request):
            started.append(request)
            received.set()
            await asyncio.Event().wait()

        respx.route().mock(side_effect=handler)
        task = asyncio.create_task(client.intent(MESSAGE))
        try:
            await asyncio.wait_for(received.wait(), 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert created[-1]._client.is_closed
            assert client.resource_locks[resource_identity(role)]._value == 1
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await client.aclose()

    asyncio.run(scenario())
    assert len(started) == 1 and all(value._client.is_closed for value in created)
