import asyncio
import fcntl
import json
import os
import time
from contextvars import ContextVar

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from assistant.ai_settings import AISettings, AISettingsUpdate
from assistant.capture_jobs import CaptureJobs, MODEL_LOCK
from assistant.clients import ServiceError
from assistant.images import VisionClient, normalize
from assistant.main import PREFIX, create_app
from assistant.state import State
from test_api import FakeClients, G1, U1
from test_ollama_minicpm import (
    HOST, ITEM, MESSAGE, clear_provider_environment, login, minicpm_config,
    structured_value, synthetic_photo,
)
from test_provider_runtime import reply


def save(store, role_name, **changes):
    public = store.public()
    body = {"revision": public["revision"]}
    for name in ("chat", "vision"):
        body[name] = {key: public[name][key] for key in ("provider", "baseUrl", "model")}
        body[name]["keyAction"] = "keep"
    body[role_name].update(changes)
    return store.update(AISettingsUpdate.model_validate(body))


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    clear_provider_environment(monkeypatch)
    return minicpm_config(monkeypatch, tmp_path)


def worker(cfg):
    cfg.uploads.mkdir(exist_ok=True)
    state, fake = State(cfg.state, cfg.instance_id), FakeClients()
    settings = AISettings(state, cfg)
    vision = VisionClient(cfg, settings_snapshot=settings.runtime_snapshot)
    manager = CaptureJobs(cfg, state, fake, vision)
    session = {"id": "synthetic-runtime-session", "actor": U1, "collection": G1, "credential": "Bearer alice"}
    state.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                  (session["id"], cfg.cipher.encrypt(b"Bearer alice").decode(), U1, G1, "csrf", time.time() + 3600))
    return manager, session, settings, fake


@pytest.mark.parametrize("change", ["chat_only", "vision_model", "vision_key", "vision_endpoint"])
@respx.mock
def test_queued_photo_retains_manual_on_any_effective_vision_change_but_not_chat(cfg, change):
    manager, session, settings, fake = worker(cfg)
    jpeg = normalize(synthetic_photo(), cfg)
    row = manager.submit(session, "synthetic-idempotency-key", jpeg, "", None)
    if change == "chat_only":
        save(settings, "chat", model="new-chat-model")
    elif change == "vision_key":
        save(settings, "vision", keyAction="replace", apiKey="synthetic-new-account-key")
    elif change == "vision_endpoint":
        save(settings, "vision", baseUrl="http://new-vision.test:11434", keyAction="clear")
    else:
        save(settings, "vision", model="new-vision-model")
    # A replay never creates a new photo or silently retargets the saved job.
    replay = manager.submit(session, "synthetic-idempotency-key", jpeg, "", None)
    assert replay["id"] == row["id"] and replay["configuration"] == row["configuration"]
    assert len(list(cfg.uploads.iterdir())) == 1
    route = respx.post(HOST + "/api/chat").mock(return_value=reply("ollama", structured_value(True)))

    async def scenario():
        try:
            assert await manager.step()
        finally:
            await manager.vision.aclose()

    asyncio.run(scenario())
    final = manager.load(session, row["id"])
    if change == "chat_only":
        assert final["status"] == "succeeded" and route.call_count == 1
    else:
        assert final["status"] == "manual" and route.call_count == 0
        assert json.loads(final["warning_codes"]) == ["analysis_configuration_changed"]
        assert "not sent" in json.loads(final["result"])["warnings"][0]
    assert next(cfg.uploads.iterdir()).read_bytes() == jpeg
    assert not any(fake.calls[name] for name in ("create_item", "update_item", "move_item", "upload"))


@respx.mock
def test_running_photo_save_during_repair_finishes_original_then_next_job_uses_new(cfg):
    manager, session, settings, _ = worker(cfg)
    jpeg, calls = normalize(synthetic_photo(), cfg), []
    first = manager.submit(session, "synthetic-first", jpeg, "", None)
    new_url = "http://new-vision.test:11434"

    def handler(request):
        calls.append(request)
        expected = HOST if len(calls) <= 2 else new_url
        assert str(request.url) == expected + "/api/chat"
        assert request.headers.get("authorization") == (None if len(calls) <= 2 else "Bearer synthetic-new-key")
        value = structured_value(True)
        if len(calls) == 1:
            save(settings, "vision", baseUrl=new_url, keyAction="replace", apiKey="synthetic-new-key")
            value["skip_review"] = True
        return reply("ollama", value)

    respx.route().mock(side_effect=handler)

    async def scenario():
        try:
            assert await manager.step()
            assert manager.load(session, first["id"])["status"] == "succeeded"
            second = manager.submit(session, "synthetic-second", jpeg, "", None)
            assert second["configuration"] != first["configuration"]
            assert await manager.step()
            assert manager.load(session, second["id"])["status"] == "succeeded"
        finally:
            await manager.vision.aclose()

    asyncio.run(scenario())
    assert len(calls) == 3


@pytest.mark.parametrize("changed_role", ["chat", "vision"])
@respx.mock
def test_real_chat_turn_pins_saved_profile_through_repair_and_scopes_later_invalidation(cfg, changed_role):
    fake, calls = FakeClients(), []
    fake.add(ITEM)
    app = create_app(cfg, fake)
    settings = AISettings(app.state.store, cfg)
    new_url = "http://new-chat.test:11434"

    def handler(request):
        calls.append(request)
        value = structured_value(False)
        if len(calls) == 1:
            save(settings, changed_role, baseUrl=new_url, model="saved-model", keyAction="clear")
            value["skip_review"] = True
        assert str(request.url) == (new_url if changed_role == "chat" and len(calls) > 2 else HOST) + "/api/chat"
        return reply("ollama", value)

    respx.route().mock(side_effect=handler)
    with TestClient(app, base_url=cfg.origin, headers={"Origin": cfg.origin}) as client:
        login(client)
        first = client.post(PREFIX + "/chat", json={"message": MESSAGE})
        assert first.status_code == 200, first.text
        second = client.post(PREFIX + "/chat",
                             json={"message": MESSAGE, "conversationId": first.json()["conversationId"]})
        if changed_role == "chat":
            assert second.status_code == 409 and second.json()["error"]["code"] == "conversation_configuration_changed"
            assert len(calls) == 2
            assert client.post(PREFIX + "/conversations/reset", json={}).status_code == 200
            assert client.post(PREFIX + "/chat", json={"message": MESSAGE}).status_code == 200
        else:
            assert second.status_code == 200, second.text
        assert client.get(PREFIX + "/actions").json()["actions"] == []
    assert len(calls) == 3


@pytest.mark.parametrize("changed_role", ["chat", "vision"])
@respx.mock
def test_credential_rotation_invalidates_only_the_affected_chat_context(cfg, changed_role):
    fake = FakeClients()
    fake.add(ITEM)
    app = create_app(cfg, fake)
    settings = AISettings(app.state.store, cfg)
    route = respx.post(HOST + "/api/chat").mock(return_value=reply("ollama", structured_value(False)))
    with TestClient(app, base_url=cfg.origin, headers={"Origin": cfg.origin}) as client:
        login(client)
        first = client.post(PREFIX + "/chat", json={"message": MESSAGE})
        assert first.status_code == 200
        save(settings, changed_role, keyAction="replace", apiKey="synthetic-rotated-account-key")
        second = client.post(PREFIX + "/chat",
                             json={"message": MESSAGE, "conversationId": first.json()["conversationId"]})
        assert second.status_code == (409 if changed_role == "chat" else 200)
        assert route.call_count == (1 if changed_role == "chat" else 2)


@respx.mock
def test_unreadable_saved_settings_leave_queued_photo_manual_without_fallback(cfg):
    manager, session, settings, _ = worker(cfg)
    jpeg = normalize(synthetic_photo(), cfg)
    row = manager.submit(session, "synthetic-corrupt-settings", jpeg, "", None)
    save(settings, "vision", model="new-model")
    manager.state.execute("UPDATE ai_settings SET ciphertext='synthetic-invalid-ciphertext'")
    route = respx.route().mock(return_value=reply("ollama", structured_value(True)))
    with pytest.raises(ServiceError) as error:
        manager.submit(session, "synthetic-must-not-persist", jpeg, "", None)
    assert error.value.code == "AI_SETTINGS_UNAVAILABLE"

    async def scenario():
        try:
            assert await manager.step()
        finally:
            await manager.vision.aclose()

    asyncio.run(scenario())
    final = manager.load(session, row["id"])
    assert final["status"] == "manual" and route.call_count == 0
    assert json.loads(final["warning_codes"]) == ["AI_SETTINGS_UNAVAILABLE"]
    assert len(list(cfg.uploads.iterdir())) == 1


@respx.mock
def test_legacy_photo_pins_target_before_outer_model_queue_wait(cfg, monkeypatch):
    app, calls = create_app(cfg, FakeClients()), []
    settings = AISettings(app.state.store, cfg)
    real_lock = app.state.store.collection_lock
    new_url = "http://saved-legacy-vision.test:11434"

    def handler(request):
        calls.append(str(request.url))
        return reply("ollama", structured_value(True))

    respx.route(host="inventory.test").pass_through()
    respx.route().mock(side_effect=handler)
    with TestClient(app, base_url=cfg.origin, headers={"Origin": cfg.origin}) as client:
        login(client)

        async def scenario():
            waiting = asyncio.Event()
            request_scope = ContextVar("legacy_queue_request", default=False)
            real_flock = fcntl.flock
            task, identity, blocked = None, None, []

            def witness(fd, mode):
                try:
                    return real_flock(fd, mode)
                except BlockingIOError:
                    stat = os.fstat(fd)
                    if request_scope.get() and (stat.st_dev, stat.st_ino) == identity:
                        blocked.append(True)
                        waiting.set()
                    raise

            monkeypatch.setattr(fcntl, "flock", witness)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=cfg.origin,
                                         cookies=dict(client.cookies), headers=dict(client.headers)) as api:
                async def capture():
                    token = request_scope.set(True)
                    try:
                        return await api.post(
                            PREFIX + "/captures",
                            files={"image": ("synthetic.png", synthetic_photo(), "image/png")},
                        )
                    finally:
                        request_scope.reset(token)

                try:
                    async with real_lock(MODEL_LOCK):
                        stat = (app.state.store.locks / MODEL_LOCK).stat()
                        identity = (stat.st_dev, stat.st_ino)
                        task = asyncio.create_task(capture())
                        reached = asyncio.create_task(waiting.wait())
                        try:
                            done, _ = await asyncio.wait((task, reached), timeout=2,
                                                         return_when=asyncio.FIRST_COMPLETED)
                            if task in done:
                                early = await task
                                raise AssertionError(f"Capture completed before the held lock: {early.status_code} {early.text}")
                            assert reached in done, "Capture did not reach the model lock"
                        finally:
                            reached.cancel()
                            await asyncio.gather(reached, return_exceptions=True)
                        save(settings, "vision", baseUrl=new_url, keyAction="clear")
                        assert blocked, "No actual kernel contention was observed for this request"
                    result = await asyncio.wait_for(task, 3)
                    assert result.status_code == 200
                    assert result.json()["draft"]["name"] == "Synthetic local box"
                    assert calls == [HOST + "/api/chat"]
                    next_result = await api.post(
                        PREFIX + "/captures",
                        files={"image": ("synthetic.png", synthetic_photo(), "image/png")},
                    )
                    assert next_result.status_code == 200
                    assert calls == [HOST + "/api/chat", new_url + "/api/chat"]
                finally:
                    if task is not None:
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

        client.portal.call(scenario)
