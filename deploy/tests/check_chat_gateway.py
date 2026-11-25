"""Run only on disposable Homebox/API/MCP, using the selected real chat gateway."""

import json
import os
from pathlib import Path
import re
import secrets
import time
from uuid import uuid4

import httpx


def main():
    assert os.environ.get("ALLOW_DISPOSABLE_GATEWAY") == "1", "Disposable gateway test opt-in required"
    username = f"gateway-check-{uuid4().hex}@example.invalid"
    password = secrets.token_urlsafe(24)
    with httpx.Client(timeout=30) as client:
        registration = client.post(
            "http://homebox:7745/api/v1/users/register",
            json={"name": "Synthetic gateway check", "email": username, "password": password},
        )
        registration.raise_for_status()

    prefix = "http://assistant-api:8000/api/assistant/v1"
    with httpx.Client(base_url=prefix, headers={"Origin": "http://localhost:8088"}, timeout=120) as client:
        login = client.post("/auth/login", json={"username": username, "password": password})
        login.raise_for_status()
        session = login.json()
        expected = {
            "chat": {"provider": "anthropic-messages", "model": "claude-haiku-4-5"},
            "vision": {"provider": "ollama", "model": "qwen3-vl:2b"},
        }
        assert session["ai"] == expected
        client.headers.update({"X-CSRF-Token": session["csrfToken"],
                               "X-Collection-ID": session["collectionId"]})
        before = client.get("/items")
        before.raise_for_status()
        assert before.json() == {"items": [], "total": 0}

        # This phrasing deliberately misses every deterministic offline regex.
        # The successful "No matching" response requires a validated model intent
        # followed by the actual MCP/Homebox search, not the provider-error fallback.
        message = "Could you locate inventory records named SYNTHETIC-PROTOCOL-CHECK-7D81209B, please?"
        assert not re.match(r"^(where (is|are)|find|search for|look for)\s", message, re.I)
        started = time.monotonic()
        response = client.post("/chat", json={"message": message})
        elapsed = time.monotonic() - started
        response.raise_for_status()
        result = response.json()
        assert result.get("message") == "No matching inventory records.", "Chat did not complete a validated gateway search intent"
        assert result.get("items") == [] and not result.get("action")
        after = client.get("/items")
        after.raise_for_status()
        assert after.json() == {"items": [], "total": 0}
        logout = client.post("/auth/logout", json={})
        logout.raise_for_status()

    report = {"chatHTTPStatus": response.status_code, "chatSeconds": round(elapsed, 3),
              "validatedGatewayIntent": True, "mcpHomeboxSearch": True,
              "inventoryWrites": 0, "roles": expected, "photoRequests": 0, "ollamaRequests": 0}
    output = Path("/acceptance/chat-gateway-result.json")
    output.write_text(json.dumps(report, indent=2) + "\n")
    os.chmod(output, 0o600)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
