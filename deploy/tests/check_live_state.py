"""Create/verify synthetic state for a real, isolated stack backup roundtrip."""

import hashlib
import io
import json
import os
from pathlib import Path
import sys

import httpx
from PIL import Image


DIRECTORY = Path("/acceptance")
PREFIX = "/api/assistant/v1"
credentials = json.loads((DIRECTORY / "credentials.json").read_text())
user = credentials["users"][0]
expected_path = DIRECTORY / "restore-expected.json"


def main(mode):
    expected = None if mode == "create" else json.loads(expected_path.read_text())
    with httpx.Client(base_url="http://proxy:8088" + PREFIX,
                      headers={"Host": "localhost:8088", "Origin": "http://localhost:8088"},
                      timeout=60) as client:
        if mode == "verify":
            client.cookies.set("hb_assistant", expected["sessionCookie"])
            response = client.get("/session")
        else:
            response = client.post("/auth/login", json={"username": user["email"], "password": user["password"]})
        response.raise_for_status()
        session = response.json()
        client.headers.update({"X-CSRF-Token": session["csrfToken"], "X-Collection-ID": session["collectionId"]})

        def execute(kind, payload, **extra):
            proposal = client.post("/actions", json={"type": kind, "payload": payload, **extra})
            proposal.raise_for_status()
            action = proposal.json()
            confirmed = client.post(f"/actions/{action['id']}/confirm",
                                    json={"revision": action["revision"]},
                                    headers={"Idempotency-Key": action["id"]})
            confirmed.raise_for_status()
            result = confirmed.json()
            assert result["status"] == "succeeded", result
            return result

        if mode == "create":
            assert not expected_path.exists(), "Use a fresh acceptance state fixture"
            location = execute("location.create", {"name": "Deployment restore box"})["result"]
            photo = io.BytesIO()
            Image.new("RGB", (80, 60), (29, 67, 113)).save(photo, "JPEG")
            capture = client.post("/captures", files={"image": ("restore-fixture.jpg", photo.getvalue(), "image/jpeg")})
            capture.raise_for_status()
            draft = capture.json()
            assert not draft["draft"]["name"] and draft["warnings"], "Expected the real model-offline fallback"
            action = execute("item.create", {
                **draft["draft"], "name": "Deployment restore fixture", "locationId": location["id"],
                "serialNumber": "RESTORE-SYNTHETIC-001", "notes": "Generated backup test data",
            }, captureId=draft["captureId"])
            entity_id = action["result"]["id"]
            mapping = client.post("/tags/mappings", json={"targetType": "item", "entityId": entity_id})
            mapping.raise_for_status()
            expected = {
                "entityId": entity_id, "locationId": location["id"], "actionId": action["id"],
                "tagId": mapping.json()["tagId"], "collectionId": session["collectionId"],
                "instanceId": credentials["instanceId"],
            }
        entity = client.get(f"/entities/{expected['entityId']}")
        entity.raise_for_status()
        assert entity.json()["name"] == "Deployment restore fixture"
        assert entity.json()["locationId"] == expected["locationId"]
        resolved = client.get(f"/tags/resolve/{expected['tagId']}")
        resolved.raise_for_status()
        assert resolved.json()["entity"]["id"] == expected["entityId"]
        action = client.get(f"/actions/{expected['actionId']}")
        action.raise_for_status()
        assert action.json()["status"] == "succeeded"
        assert action.json()["result"]["id"] == expected["entityId"]
        if mode == "create":
            expected["sessionCookie"] = client.cookies.get("hb_assistant")
            assert expected["sessionCookie"]
        print("PASS: live inventory, confirmed action and NFC mapping agree")

    # Independently verify the source Homebox record and exact persisted photo.
    with httpx.Client(base_url="http://proxy:8089/api/v1",
                      headers={"Host": "localhost:8089"}, timeout=30) as homebox:
        login = homebox.post("/users/login", json={"username": user["email"], "password": user["password"]})
        login.raise_for_status()
        homebox.headers.update({"Authorization": login.json()["token"], "X-Tenant": expected["collectionId"]})
        record = homebox.get(f"/entities/{expected['entityId']}")
        record.raise_for_status()
        detail = record.json()
        assert detail["serialNumber"] == "RESTORE-SYNTHETIC-001"
        assert detail["notes"] == "Generated backup test data"
        assert len(detail["attachments"]) == 1
        attachment_id = detail["attachments"][0]["id"]
        attachment = homebox.get(f"/entities/{expected['entityId']}/attachments/{attachment_id}")
        attachment.raise_for_status()
        digest = hashlib.sha256(attachment.content).hexdigest()
        assert len(attachment.content) > 100
        if mode == "create":
            expected.update({"attachmentId": attachment_id, "attachmentSHA256": digest,
                             "attachmentBytes": len(attachment.content)})
        else:
            assert attachment_id == expected["attachmentId"]
            assert digest == expected["attachmentSHA256"]
            assert len(attachment.content) == expected["attachmentBytes"]
        if mode == "create":
            with expected_path.open("x") as output:
                os.chmod(expected_path, 0o600)
                json.dump(expected, output)
        print(f"PASS: actual Homebox attachment bytes verified ({len(attachment.content)} bytes, SHA-256 {digest})")
    if mode == "create":
        print("PASS: model absent, photo normalized -> manual draft -> confirmed item with attachment")
    else:
        print("PASS: restored encrypted credentials, user/collection access, item/photo/action/mapping remain usable")


if __name__ == "__main__":
    assert len(sys.argv) == 2 and sys.argv[1] in {"create", "verify"}
    main(sys.argv[1])
