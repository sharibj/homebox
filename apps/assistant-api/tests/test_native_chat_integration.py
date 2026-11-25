import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from assistant.config import Config
from assistant.main import PREFIX, create_app
from test_api import FakeClients

PHRASE = "Please check my inventory for ceruleanwidget-42 and tell me its location."
MUTATIONS = ("create_item", "update_item", "move_item", "set_tags", "create_location", "upload")


def native_response(content):
    return httpx.Response(200, json={
        "id": "msg_synthetic", "type": "message", "role": "assistant",
        "model": "claude-haiku-4-5", "stop_reason": "end_turn",
        "content": [{"type": "text", "text": content}],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    })


@pytest.fixture
def native_app(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_CHAT_PROVIDER", "anthropic-messages")
    monkeypatch.setenv("AI_CHAT_BASE_URL", "http://gateway.example.invalid:6655/anthropic/")
    monkeypatch.setenv("AI_CHAT_MODEL", "claude-haiku-4-5")
    key_file = tmp_path / "synthetic-key"
    key_file.write_text("synthetic-local-gateway-key")
    monkeypatch.setenv("AI_CHAT_API_KEY_FILE", str(key_file))
    cfg = Config.load()
    cfg.state, cfg.uploads = tmp_path / "state.db", tmp_path / "uploads"
    fake = FakeClients()
    item_id = fake.add("ceruleanwidget-42")
    fake.items[item_id]["locationPath"] = "Synthetic room / Drawer"
    return create_app(cfg, fake), fake, item_id


@pytest.mark.parametrize("mode", ["bare", "whole_fence", "strict_repair"])
@respx.mock
def test_native_chat_actual_intent_entrypoint_is_read_only(native_app, mode):
    app, fake, item_id = native_app
    valid = '{"kind":"search","query":"ceruleanwidget-42"}'
    contents = {
        "bare": [valid],
        "whole_fence": ["```json\n" + valid + "\n```"],
        "strict_repair": ['{"kind":"search","query":"ceruleanwidget-42","skip_confirmation":true}', valid],
    }[mode]
    route = respx.post("http://gateway.example.invalid:6655/anthropic/v1/messages").mock(
        side_effect=[native_response(content) for content in contents],
    )
    with TestClient(app, base_url="https://inventory.test", headers={"Origin": app.state.config.origin}) as client:
        login = client.post(PREFIX + "/auth/login", json={"username": "alice", "password": "synthetic"}).json()
        assert login["ai"] == {
            "chat": {"provider": "anthropic-messages", "model": "claude-haiku-4-5"},
            "vision": {"provider": "ollama", "model": "qwen3-vl:2b"},
        }
        client.headers.update({"X-CSRF-Token": login["csrfToken"], "X-Collection-ID": login["collectionId"]})
        result = client.post(PREFIX + "/chat", json={"message": PHRASE})
        assert result.status_code == 200, result.text
        value = result.json()
        assert [item["id"] for item in value["items"]] == [item_id]
        assert "Synthetic room / Drawer" in value["message"]
        assert "action" not in value and "choices" not in value
        assert client.get(PREFIX + "/actions").json()["actions"] == []
        assert app.state.store.all("SELECT * FROM capture_jobs") == []
    assert route.call_count == len(contents)
    assert fake.calls["list_items_page"] == 1
    assert fake.calls["search_items"] == 0
    assert all(fake.calls[name] == 0 for name in MUTATIONS)
    payload = json.loads(route.calls[0].request.content)
    assert payload["model"] == "claude-haiku-4-5" and payload["max_tokens"] == 600
    assert route.calls[0].request.headers["x-api-key"] == "synthetic-local-gateway-key"
    assert route.calls[0].request.headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in route.calls[0].request.headers
    assert "Bearer alice" not in route.calls[0].request.content.decode()
    assert "image/jpeg" not in route.calls[0].request.content.decode()
    assert "gateway.example" not in json.dumps(login) and "synthetic-local-gateway-key" not in json.dumps(login)
    configuration = json.loads(app.state.capture_jobs.configuration())
    assert configuration["provider"] == "ollama" and configuration["model"] == "qwen3-vl:2b"


@respx.mock
def test_native_invalid_intent_cannot_execute_model_requested_write(native_app):
    app, fake, _ = native_app
    route = respx.post("http://gateway.example.invalid:6655/anthropic/v1/messages").mock(
        return_value=native_response('{"kind":"delete","skip_confirmation":true}'),
    )
    with TestClient(app, base_url="https://inventory.test", headers={"Origin": app.state.config.origin}) as client:
        login = client.post(PREFIX + "/auth/login", json={"username": "alice", "password": "synthetic"}).json()
        client.headers.update({"X-CSRF-Token": login["csrfToken"], "X-Collection-ID": login["collectionId"]})
        result = client.post(PREFIX + "/chat", json={"message": PHRASE})
        assert result.status_code == 502
        assert result.json()["error"]["code"] == "invalid_intent"
        assert client.get(PREFIX + "/actions").json()["actions"] == []
    assert route.call_count == 2
    assert all(fake.calls[name] == 0 for name in MUTATIONS)
