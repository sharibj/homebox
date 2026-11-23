"""Exercise the real API -> streamable HTTP MCP -> disposable Homebox path."""
import io
import os
import time
from uuid import uuid4

import httpx
from PIL import Image

HB = os.environ.get("TEST_HOMEBOX_URL", "http://homebox:7745")
API = os.environ.get("TEST_API_URL", "http://api:8000")
ORIGIN = os.environ.get("TEST_ORIGIN", "http://localhost:18080")
PREFIX = "/api/assistant/v1"


def main():
    for _ in range(60):
        try:
            if httpx.get(API + "/readyz", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(.5)
    else:
        raise AssertionError("Disposable API was not ready")
    username, password = f"companion-{uuid4().hex}@example.invalid", "disposable-test-password"
    response = httpx.post(HB + "/api/v1/users/register", json={
        "name": "Companion integration fixture", "email": username, "password": password,
    }, timeout=30)
    response.raise_for_status()
    with httpx.Client(base_url=API + PREFIX, headers={"Origin": ORIGIN}, timeout=60) as client:
        login = client.post("/auth/login", json={"username": username, "password": password})
        login.raise_for_status()
        client.headers["X-CSRF-Token"] = login.json()["csrfToken"]
        client.headers["X-Collection-ID"] = login.json()["collectionId"]
        assert "token" not in login.json()

        def execute(kind, payload, **extra):
            proposed = client.post("/actions", json={"type": kind, "payload": payload, **extra})
            proposed.raise_for_status()
            action = proposed.json()
            response = client.post(f"/actions/{action['id']}/confirm", json={"revision": action["revision"]},
                                   headers={"Idempotency-Key": action["id"]})
            response.raise_for_status()
            assert response.json()["status"] == "succeeded", response.text
            return response.json()

        garage = execute("location.create", {"name": "Garage"})["result"]
        shelf = execute("location.create", {"name": "Camping shelf", "parentId": garage["id"]})["result"]
        image = Image.new("RGB", (100, 100), "orange")
        buffer = io.BytesIO()
        image.save(buffer, "JPEG")
        capture = client.post("/captures", files={"image": ("object.jpg", buffer.getvalue(), "image/jpeg")})
        capture.raise_for_status()
        draft = capture.json()
        payload = {**draft["draft"], "locationId": garage["id"]}
        action = execute("item.create", payload, captureId=draft["captureId"])
        eid = action["result"]["id"]
        assert client.post(f"/actions/{action['id']}/confirm", json={"revision": 1},
                           headers={"Idempotency-Key": action["id"]}).json()["result"]["id"] == eid
        found = client.post("/chat", json={"message": "Where is my camping stove?"}).json()
        assert found["items"][0]["id"] == eid
        semantic = client.post("/chat", json={"message": "Could you remind me where I stored my camping stove?"}).json()
        assert semantic["items"][0]["id"] == eid
        proposal = client.post("/chat", json={
            "message": "I moved the Camping stove into the Camping shelf.",
        }).json()["action"]
        before = client.get(f"/entities/{eid}").json()
        assert before["locationId"] == garage["id"]
        moved = client.post(f"/actions/{proposal['id']}/confirm", json={"revision": 1},
                            headers={"Idempotency-Key": proposal["id"]})
        assert moved.json()["status"] == "succeeded", moved.text
        assert client.get(f"/entities/{eid}").json()["locationId"] == shelf["id"]
        execute("item.update", {"entityId": eid, "serialNumber": "Reviewed-123", "notes": "Safe metadata"})
        execute("item.tags", {"entityId": eid, "tags": ["camping", "outdoors"]})
        mapping = client.post("/tags/mappings", json={"targetType": "location", "entityId": shelf["id"]}).json()
        tid = mapping["tagId"]
        resolved = client.get(f"/tags/resolve/{tid}").json()
        assert resolved["entity"]["id"] == shelf["id"]
        reassigned = client.patch(f"/tags/mappings/{tid}", json={"entityId": garage["id"]}).json()
        assert reassigned["url"] == mapping["url"]
        assert client.get(f"/tags/resolve/{tid}").json()["entity"]["id"] == garage["id"]
        assert client.patch(f"/tags/mappings/{tid}", json={"status": "revoked"}).status_code == 200
        assert client.get(f"/tags/resolve/{tid}").status_code == 404
        # Verify the actual Homebox photo and fields through this disposable user's own session.
        hb_login = httpx.post(HB + "/api/v1/users/login", json={"username": username, "password": password}).json()
        record = httpx.get(HB + f"/api/v1/entities/{eid}", headers={"Authorization": hb_login["token"]}).json()
        assert record["attachments"] and record["serialNumber"] == "Reviewed-123"
        assert record["notes"] == "Safe metadata"
        assert {v["name"] for v in record["tags"]} == {"camping", "outdoors"}
        print("PASS: real user login, CSRF/session, capture fixture, reviewed create+photo, replay, grounded search")
        print("PASS: proposed-only conversational move, confirmation, edit, tags, location creation, NFC reassign/revoke")
        print("LIMIT: Ollama response is a deterministic protocol fixture; no model or phone benchmark")


if __name__ == "__main__":
    main()
