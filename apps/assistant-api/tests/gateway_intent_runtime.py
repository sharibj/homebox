"""Ops-only live local-gateway check with disposable auth and inventory fixtures."""
import json
import os
import secrets
import tempfile
from pathlib import Path
from urllib.parse import urlsplit


def main():
    # Check the authorized destination before reading any provider credential.
    url = urlsplit(os.environ.get("AI_CHAT_BASE_URL", ""))
    if (os.environ.get("AI_CHAT_PROVIDER") != "anthropic-messages"
            or os.environ.get("AI_CHAT_MODEL") != "claude-haiku-4-5"
            or url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
            or url.port != 6655 or url.path.rstrip("/") != "/anthropic"
            or url.username or url.password or url.query or url.fragment):
        raise RuntimeError("Use only the approved local Anthropic chat gateway and model for this check")
    with tempfile.TemporaryDirectory(prefix="gateway-intent-") as directory:
        root = Path(directory)
        session_secret, mcp_secret = root / "session-secret", root / "mcp-secret"
        for path in (session_secret, mcp_secret):
            path.write_text(secrets.token_urlsafe(48))
            path.chmod(0o600)
        for key in list(os.environ):
            if key.startswith("AI_") and not key.startswith("AI_CHAT_"):
                del os.environ[key]
        os.environ.update(
            ASSISTANT_PUBLIC_ORIGIN="https://inventory.test",
            ASSISTANT_STATE_PATH=str(root / "state.db"), ASSISTANT_UPLOAD_DIR=str(root / "uploads"),
            ASSISTANT_SESSION_SECRET_FILE=str(session_secret), MCP_SHARED_SECRET_FILE=str(mcp_secret),
            HOMEBOX_INSTANCE_ID="56cd30b1-7bdd-466e-9459-8b402cd3349c",
            OLLAMA_URL="http://ollama:11434", OLLAMA_MODEL="qwen3-vl:2b",
        )
        from fastapi.testclient import TestClient
        from assistant.config import Config
        from assistant.main import PREFIX, create_app
        from test_api import FakeClients

        cfg, fake = Config.load(), FakeClients()
        item_id = fake.add("ceruleanwidget-42")
        fake.items[item_id]["locationPath"] = "Synthetic room / Drawer"
        app = create_app(cfg, fake)
        assert cfg.chat_provider_settings.kind == "anthropic-messages"
        assert cfg.vision_provider_settings.kind == "ollama"
        assert app.state.vision.chat_provider is not app.state.vision.provider
        generate = app.state.vision.chat_provider.generate
        calls = 0

        async def counted(*args, **kwargs):
            nonlocal calls
            calls += 1
            assert kwargs.get("image_jpeg") is None
            return await generate(*args, **kwargs)

        async def no_vision(*args, **kwargs):
            raise AssertionError("A chat request must not call the vision provider")

        app.state.vision.chat_provider.generate = counted
        app.state.vision.provider.generate = no_vision
        with TestClient(app, base_url=cfg.origin, headers={"Origin": cfg.origin}) as client:
            login = client.post(PREFIX + "/auth/login", json={"username": "alice", "password": "synthetic"}).json()
            client.headers.update({"X-CSRF-Token": login["csrfToken"], "X-Collection-ID": login["collectionId"]})
            result = client.post(PREFIX + "/chat", json={
                "message": "Please check my inventory for ceruleanwidget-42 and tell me its location.",
            })
            assert result.status_code == 200
            value = result.json()
            assert [item["id"] for item in value["items"]] == [item_id], "Strict intent did not resolve the synthetic fixture"
            assert "Synthetic room / Drawer" in value["message"]
            assert "action" not in value and 1 <= calls <= 2
            assert client.get(PREFIX + "/actions").json()["actions"] == []
            assert app.state.store.all("SELECT * FROM capture_jobs") == []
        mutations = sum(fake.calls[name] for name in (
            "create_item", "update_item", "move_item", "set_tags", "create_location", "upload",
        ))
        assert mutations == 0 and fake.calls["list_items_page"] == 1 and fake.calls["search_items"] == 0
        print(json.dumps({
            "status": "passed", "native_model_calls": calls, "strict_intent": "search",
            "synthetic_matches": 1, "mutation_calls": mutations, "vision_calls": 0,
            "chat_model": cfg.chat_provider_settings.model, "vision_model": cfg.vision_provider_settings.model,
        }), flush=True)


if __name__ == "__main__":
    main()
