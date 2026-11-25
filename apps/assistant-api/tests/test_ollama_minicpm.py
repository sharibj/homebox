import asyncio
import base64
import io
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from assistant.config import Config
from assistant.images import VisionClient, normalize
from assistant.main import PREFIX, create_app
from assistant.providers import ProviderSettings
from assistant.schemas import Intent, Vision
from test_api import FakeClients
from test_capture_jobs import terminal

MODEL = "minicpm-v4.6"
HOST = "http://host.docker.internal:11434"
ITEM = "minicpm-fixture-42"
MESSAGE = f"Please check my inventory for {ITEM} and tell me its location."
MUTATIONS = ("create_item", "update_item", "move_item", "set_tags", "create_location", "upload")
KEY_FIELDS = ("AI_API_KEY_FILE", "AI_CHAT_API_KEY_FILE", "AI_VISION_API_KEY_FILE")


def clear_provider_environment(monkeypatch):
    for prefix in ("AI", "AI_CHAT", "AI_VISION"):
        for suffix in ("PROVIDER", "BASE_URL", "MODEL", "API_KEY_FILE"):
            monkeypatch.delenv(prefix + "_" + suffix, raising=False)
    for name in ("OLLAMA_URL", "OLLAMA_MODEL", "ASSISTANT_CHAT_TURN_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def isolated_provider_environment(monkeypatch):
    clear_provider_environment(monkeypatch)


def minicpm_config(monkeypatch, tmp_path, base_url=HOST):
    for role in ("CHAT", "VISION"):
        monkeypatch.setenv("AI_" + role + "_PROVIDER", "ollama")
        monkeypatch.setenv("AI_" + role + "_BASE_URL", base_url)
        monkeypatch.setenv("AI_" + role + "_MODEL", MODEL)
    for name in KEY_FIELDS:
        monkeypatch.setenv(name, "")
    monkeypatch.setenv("ASSISTANT_CHAT_TURN_TIMEOUT_SECONDS", "120")
    cfg = Config.load()
    cfg.state, cfg.uploads = tmp_path / "state.db", tmp_path / "uploads"
    return cfg


def synthetic_photo():
    output = io.BytesIO()
    Image.new("RGB", (1400, 700), "blue").save(output, "PNG")
    return output.getvalue()


def structured_value(photo):
    return ({"items": [{"name": "Synthetic local box", "manufacturer": None, "confidence": 0.9}],
             "image_notes": "Synthetic fixture"} if photo else {"kind": "search", "query": ITEM})


def ollama_response(value, **fields):
    return httpx.Response(200, json={
        "model": MODEL, "done": True, "done_reason": "stop",
        "message": {"role": "assistant", "content": json.dumps(value)}, **fields,
    })


def login(client):
    response = client.post(PREFIX + "/auth/login", json={"username": "alice", "password": "synthetic"})
    assert response.status_code == 200
    value = response.json()
    client.headers.update({"X-CSRF-Token": value["csrfToken"], "X-Collection-ID": value["collectionId"]})
    return value


def assert_native_payload(request, photo, jpeg=None):
    assert request.url.path.endswith("/api/chat") and request.method == "POST"
    assert not any(name in request.headers for name in ("authorization", "x-api-key", "x-goog-api-key", "cookie"))
    payload = json.loads(request.content)
    assert set(payload) == {"model", "stream", "format", "messages", "options", "keep_alive"}
    assert payload["model"] == MODEL and payload["stream"] is False and payload["keep_alive"] == "5m"
    assert payload["options"] == {"temperature": 0, "num_ctx": 4096, "num_predict": 1200 if photo else 600}
    assert payload["format"] == (Vision.model_json_schema() if photo else Intent.model_json_schema())
    users = [message for message in payload["messages"] if message["role"] == "user"]
    assert len(users) == 1
    if photo:
        decoded = base64.b64decode(users[0]["images"][0], validate=True)
        if jpeg is not None:
            assert decoded == jpeg
        with Image.open(io.BytesIO(decoded)) as image:
            assert image.format == "JPEG" and image.size == (1024, 512)
    else:
        assert all("images" not in message for message in payload["messages"])
    assert "Bearer alice" not in request.content.decode()
    return payload


@pytest.mark.parametrize("selection", ["legacy", "shared", "roles"])
def test_exact_minicpm_model_selects_both_roles_without_a_provider_special_case(monkeypatch, selection):
    if selection == "legacy":
        monkeypatch.setenv("OLLAMA_URL", HOST)
        monkeypatch.setenv("OLLAMA_MODEL", MODEL)
    elif selection == "shared":
        monkeypatch.setenv("AI_PROVIDER", "ollama")
        monkeypatch.setenv("AI_BASE_URL", HOST)
        monkeypatch.setenv("AI_MODEL", MODEL)
    else:
        # Complete role overrides must replace even explicitly selected cloud roles.
        monkeypatch.setenv("AI_PROVIDER", "anthropic-messages")
        monkeypatch.setenv("AI_BASE_URL", "https://synthetic-cloud.test/native")
        monkeypatch.setenv("AI_MODEL", "claude-haiku-4-5")
        for role in ("CHAT", "VISION"):
            monkeypatch.setenv("AI_" + role + "_PROVIDER", "ollama")
            monkeypatch.setenv("AI_" + role + "_BASE_URL", HOST)
            monkeypatch.setenv("AI_" + role + "_MODEL", MODEL)
    for name in KEY_FIELDS:
        monkeypatch.setenv(name, "")
    cfg = Config.load()
    assert cfg.chat_provider_settings == cfg.vision_provider_settings == ProviderSettings("ollama", HOST, MODEL)


def test_minicpm_is_opt_in_and_does_not_replace_neutral_defaults():
    cfg = Config.load()
    assert cfg.chat_provider_settings == cfg.vision_provider_settings == ProviderSettings()
    assert cfg.chat_timeout == 60 and cfg.analysis_timeout == 900 and cfg.capture_sync_timeout == 90


@pytest.mark.parametrize("repair", [False, True])
@respx.mock
def test_minicpm_authenticated_chat_and_legacy_photo_use_real_structured_callers(tmp_path, monkeypatch, repair):
    cfg = minicpm_config(monkeypatch, tmp_path)
    fake = FakeClients()
    item_id = fake.add(ITEM)
    fake.items[item_id]["locationPath"] = "Synthetic room / Drawer"
    app = create_app(cfg, fake)
    assert app.state.vision.chat_provider is app.state.vision.provider
    photo = synthetic_photo()
    jpeg = normalize(photo, cfg)
    seen, counts = [], {False: 0, True: 0}

    def handler(request):
        is_photo = any("images" in message for message in json.loads(request.content)["messages"])
        counts[is_photo] += 1
        seen.append((is_photo, assert_native_payload(request, is_photo, jpeg)))
        value = structured_value(is_photo)
        if repair and counts[is_photo] == 1:
            value["skip_review"] = True
        return ollama_response(value)

    route = respx.post(HOST + "/api/chat").mock(side_effect=handler)
    with TestClient(app, base_url=cfg.origin, headers={"Origin": cfg.origin}) as client:
        metadata = login(client)
        assert metadata["ai"] == {
            role: {"provider": "ollama", "model": MODEL} for role in ("chat", "vision")}
        assert HOST not in json.dumps(metadata)
        chat = client.post(PREFIX + "/chat", json={"message": MESSAGE})
        assert chat.status_code == 200, chat.text
        assert [value["id"] for value in chat.json()["items"]] == [item_id]
        assert "Synthetic room / Drawer" in chat.json()["message"]
        capture = client.post(PREFIX + "/captures", files={"image": ("synthetic.png", photo, "image/png")})
        assert capture.status_code == 200, capture.text
        assert capture.json()["draft"]["name"] == "Synthetic local box"
        assert capture.json()["warnings"]
        assert client.get(PREFIX + "/actions").json()["actions"] == []
        assert next(cfg.uploads.iterdir()).read_bytes() == jpeg
    assert route.call_count == (4 if repair else 2)
    assert all(fake.calls[name] == 0 for name in MUTATIONS)
    for is_photo in (False, True):
        payloads = [payload for role, payload in seen if role == is_photo]
        if repair:
            assert payloads[0]["messages"][0]["content"] == payloads[1]["messages"][0]["content"]
            assert payloads[1]["messages"][-1] == {
                "role": "system",
                "content": "Return only one JSON object that exactly matches the requested schema.",
            }


@pytest.mark.parametrize("repair", [False, True])
@respx.mock
def test_minicpm_durable_photo_worker_keeps_draft_and_confirmation_boundary(tmp_path, monkeypatch, repair):
    cfg = minicpm_config(monkeypatch, tmp_path)
    fake = FakeClients()
    app = create_app(cfg, fake)
    photo, calls = synthetic_photo(), []
    jpeg = normalize(photo, cfg)

    def handler(request):
        calls.append(assert_native_payload(request, True, jpeg))
        value = structured_value(True)
        if repair and len(calls) == 1:
            value["skip_review"] = True
        return ollama_response(value)

    route = respx.post(HOST + "/api/chat").mock(side_effect=handler)
    with TestClient(app, base_url=cfg.origin, headers={"Origin": cfg.origin}) as client:
        login(client)
        submitted = client.post(PREFIX + "/capture-jobs",
                                files={"image": ("synthetic.png", photo, "image/png")},
                                headers={"Idempotency-Key": "synthetic-minicpm-submission"})
        assert submitted.status_code == 202
        job = terminal(client, submitted.json())
        assert job["status"] == "succeeded" and job["result"]["draft"]["name"] == "Synthetic local box"
        assert job["result"]["warnings"] and client.get(PREFIX + "/actions").json()["actions"] == []
        assert next(cfg.uploads.iterdir()).read_bytes() == jpeg
    assert route.call_count == (2 if repair else 1)
    assert all(fake.calls[name] == 0 for name in MUTATIONS)


@pytest.mark.parametrize("photo", [False, True])
@pytest.mark.parametrize("failure", ["unavailable", "timeout", "schema", "truncated", "tools"])
@respx.mock
def test_minicpm_failures_stay_manual_or_read_only_without_provider_fallback(tmp_path, monkeypatch, photo, failure):
    cfg = minicpm_config(monkeypatch, tmp_path)
    fake = FakeClients()
    app = create_app(cfg, fake)

    def handler(request):
        assert_native_payload(request, photo, normalize(synthetic_photo(), cfg) if photo else None)
        if failure == "unavailable":
            return httpx.Response(503)
        if failure == "timeout":
            raise httpx.ReadTimeout("Synthetic local timeout", request=request)
        value = structured_value(photo)
        if failure == "schema":
            value["skip_review"] = True
        if failure == "truncated":
            return ollama_response(value, done_reason="length")
        if failure == "tools":
            return ollama_response(value, message={"content": json.dumps(value), "tool_calls": [{"name": "delete_all"}]})
        return ollama_response(value)

    route = respx.post(HOST + "/api/chat").mock(side_effect=handler)
    with TestClient(app, base_url=cfg.origin, headers={"Origin": cfg.origin}) as client:
        login(client)
        if photo:
            response = client.post(PREFIX + "/captures",
                                   files={"image": ("synthetic.png", synthetic_photo(), "image/png")})
            assert response.status_code == 200 and response.json()["draft"]["name"] == ""
            assert response.json()["warnings"] and next(cfg.uploads.iterdir()).exists()
        else:
            response = client.post(PREFIX + "/chat", json={"message": MESSAGE})
            assert response.status_code == (503 if failure in {"unavailable", "timeout"} else 502)
            assert response.json()["error"]["code"] == (
                "model_unavailable" if failure in {"unavailable", "timeout"} else "invalid_intent")
        assert client.get(PREFIX + "/actions").json()["actions"] == []
    assert route.call_count == (1 if failure in {"unavailable", "timeout"} else 2)
    assert all(fake.calls[name] == 0 for name in MUTATIONS)


@pytest.mark.parametrize("photo", [False, True])
@respx.mock
def test_minicpm_whole_deadline_cancels_native_request_without_retry_or_write(tmp_path, monkeypatch, photo):
    cfg = minicpm_config(monkeypatch, tmp_path)
    if photo:
        cfg.analysis_timeout = .1
    else:
        cfg.chat_timeout = .1
    fake, started, cancelled = FakeClients(), [], []
    app = create_app(cfg, fake)

    async def handler(request):
        started.append(request)
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(True)

    respx.post(HOST + "/api/chat").mock(side_effect=handler)
    with TestClient(app, base_url=cfg.origin, headers={"Origin": cfg.origin}) as client:
        login(client)
        if photo:
            result = client.post(PREFIX + "/captures",
                                 files={"image": ("synthetic.png", synthetic_photo(), "image/png")})
            assert result.status_code == 200 and result.json()["draft"]["name"] == ""
            assert result.json()["warnings"] and next(cfg.uploads.iterdir()).exists()
        else:
            result = client.post(PREFIX + "/chat", json={"message": MESSAGE})
            assert result.status_code == 503 and result.json()["error"]["code"] == "chat_timed_out"
        assert client.get(PREFIX + "/actions").json()["actions"] == []
    # respx records completed responses, not a callback cancelled in flight.
    assert cancelled == [True] and len(started) == 1
    assert all(fake.calls[name] == 0 for name in MUTATIONS)


@respx.mock
def test_minicpm_opt_in_120_second_chat_budget_reaches_native_transport(tmp_path, monkeypatch):
    cfg = minicpm_config(monkeypatch, tmp_path)
    client = VisionClient(cfg)
    seen = []

    def handler(request):
        seen.append(request)
        return ollama_response(structured_value(False))

    respx.post(HOST + "/api/chat").mock(side_effect=handler)

    async def scenario():
        try:
            assert (await client.intent(MESSAGE)).query == ITEM
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert 119 <= seen[0].extensions["timeout"]["read"] <= 120


@pytest.mark.parametrize("local_role", ["chat", "vision"])
@pytest.mark.parametrize("cloud_kind", ["anthropic-messages", "gemini"])
@respx.mock
def test_minicpm_local_role_does_not_receive_selected_cloud_role_key(tmp_path, monkeypatch, local_role, cloud_kind):
    cloud_url, key = "https://synthetic-cloud.test/native", "synthetic-selected-cloud-key"
    key_file = tmp_path / "synthetic-cloud-key"
    key_file.write_text(key)
    for name, value in {"AI_PROVIDER": cloud_kind, "AI_BASE_URL": cloud_url,
                        "AI_MODEL": "claude-haiku-4-5" if cloud_kind == "anthropic-messages" else "gemini-3.1-flash-lite",
                        "AI_API_KEY_FILE": str(key_file)}.items():
        monkeypatch.setenv(name, value)
    for suffix, value in {"PROVIDER": "ollama", "BASE_URL": HOST, "MODEL": MODEL}.items():
        monkeypatch.setenv("AI_" + local_role.upper() + "_" + suffix, value)
    cfg = Config.load()
    cfg.state = tmp_path / "mixed-state.db"
    client = VisionClient(cfg)
    local_calls, cloud_calls = [], []

    def local(request):
        local_calls.append(request)
        assert not any(name in request.headers for name in ("authorization", "x-api-key", "x-goog-api-key"))
        return ollama_response(structured_value(local_role == "vision"))

    def cloud(request):
        cloud_calls.append(request)
        value = json.dumps(structured_value(local_role == "chat"))
        if cloud_kind == "anthropic-messages":
            assert request.headers["x-api-key"] == key
            return httpx.Response(200, json={"type": "message", "role": "assistant", "stop_reason": "end_turn",
                                             "content": [{"type": "text", "text": value}]})
        assert request.headers["x-goog-api-key"] == key
        return httpx.Response(200, json={"candidates": [{"finishReason": "STOP",
            "content": {"role": "model", "parts": [{"text": value}]}}]})

    respx.post(HOST + "/api/chat").mock(side_effect=local)
    cloud_path = "/v1/messages" if cloud_kind == "anthropic-messages" else "/models/gemini-3.1-flash-lite:generateContent"
    respx.post(cloud_url + cloud_path).mock(side_effect=cloud)

    async def scenario():
        try:
            assert (await client.intent(MESSAGE)).query == ITEM
            assert (await client.analyze(normalize(synthetic_photo(), cfg), "Synthetic only")).items[0].name == "Synthetic local box"
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert len(local_calls) == len(cloud_calls) == 1
