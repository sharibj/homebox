"""One real-model capture-job measurement against a disposable inventory only."""

import time

PROCESS_STARTED = time.monotonic()

import io
import json
import os
from pathlib import Path
import secrets
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw


def main():
    assert os.environ.get("ALLOW_DISPOSABLE_REAL_MODEL") == "1", "Explicit benchmark opt-in required"
    origin = "http://localhost:8088"
    prefix = "/api/assistant/v1"
    user = f"photo-benchmark-{uuid4().hex}@example.invalid"
    password = secrets.token_urlsafe(24)

    # A maximum-size square under the 24-million-pixel gate normalizes to 1024².
    # It is generated here, never copied from a user's photo or inventory.
    image = Image.new("RGB", (4898, 4898), "#f0f0f0")
    draw = ImageDraw.Draw(image)
    for row in range(8):
        for column in range(8):
            x, y = column * 600 + 40, row * 600 + 40
            color = ((row * 37 + 30) % 256, (column * 41 + 60) % 256, 130)
            draw.rectangle((x, y, x + 500, y + 500), fill=color, outline="black", width=12)
    noise = Image.effect_noise(image.size, 100).convert("RGB")
    image = Image.blend(image, noise, 0.2)
    del noise
    photo = io.BytesIO()
    image.save(photo, "JPEG", quality=80)
    del image
    assert len(photo.getbuffer()) <= 12 * 1024**2

    with httpx.Client(timeout=30) as direct:
        registered = direct.post("http://homebox:7745/api/v1/users/register",
                                 json={"name": "Synthetic photo benchmark", "email": user, "password": password})
        registered.raise_for_status()
    with httpx.Client(base_url="http://assistant-api:8000" + prefix,
                      headers={"Origin": origin}, timeout=30) as client:
        login = client.post("/auth/login", json={"username": user, "password": password})
        login.raise_for_status()
        session = login.json()
        client.headers.update({"X-CSRF-Token": session["csrfToken"],
                               "X-Collection-ID": session["collectionId"]})
        before = client.get("/items")
        before.raise_for_status()
        assert before.json()["total"] == 0
        key = uuid4().hex
        started = time.monotonic()
        response = client.post("/capture-jobs", headers={"Idempotency-Key": key},
                               files={"image": ("synthetic-square.jpg", photo.getvalue(), "image/jpeg")})
        submitted = time.monotonic()
        response.raise_for_status()
        assert response.status_code == 202
        job = response.json()
        transitions = [{"status": job["status"], "seconds": round(submitted - started, 3)}]
        polls = 0
        maximum_poll = 0
        while job["status"] in {"queued", "running"}:
            assert time.monotonic() - started < 1200, "Job exceeded its documented finite budgets"
            time.sleep(1)
            poll_started = time.monotonic()
            current = client.get("/capture-jobs/" + job["id"])
            maximum_poll = max(maximum_poll, time.monotonic() - poll_started)
            polls += 1
            current.raise_for_status()
            job = current.json()
            if transitions[-1]["status"] != job["status"]:
                transitions.append({"status": job["status"], "seconds": round(time.monotonic() - started, 3)})
        assert job["status"] in {"succeeded", "manual"}, job.get("error", {}).get("code")
        assert job["captureId"] and job["result"]["captureId"] == job["captureId"]
        after = client.get("/items")
        after.raise_for_status()
        assert after.json()["total"] == 0, "Analysis must not create inventory"
        report = {
            "sourcePixels": [4898, 4898], "expectedNormalizedPixels": [1024, 1024],
            "uploadBytes": len(photo.getbuffer()), "submissionStatus": response.status_code,
            "submissionSeconds": round(submitted - started, 3), "polls": polls,
            "maximumPollSeconds": round(maximum_poll, 3),
            "terminalStatus": job["status"], "warningCodes": job["warningCodes"],
            "jobTotalSeconds": round(time.monotonic() - started, 3),
            "freshClientProcessSeconds": round(time.monotonic() - PROCESS_STARTED, 3),
            "transitions": transitions, "inventoryWrites": 0,
        }
        output = Path("/acceptance/real-model-benchmark.json")
        output.write_text(json.dumps(report, indent=2) + "\n")
        os.chmod(output, 0o600)
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
