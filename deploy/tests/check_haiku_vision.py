"""Real Haiku capture-job proof. Use only the isolated, empty QA inventory."""

import io
import json
import os
from pathlib import Path
import re
import secrets
import time
from uuid import uuid4

import httpx
from PIL import Image, ImageDraw, ImageFont


def main():
    assert os.environ.get("ALLOW_DISPOSABLE_HAIKU_VISION") == "1"
    root = Path(os.environ.get("ACCEPTANCE_OUTPUT_DIR", "/acceptance"))
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    username = f"vision-qa-{uuid4().hex}@example.invalid"
    password = secrets.token_urlsafe(24)
    marker = "HBV482731"  # Expected ground truth; sent to the model only in pixels.
    image = Image.new("RGB", (4898, 4898), "#eeeeee")
    draw = ImageDraw.Draw(image)
    draw.polygon([(650, 1400), (1350, 800), (4150, 800), (3450, 1400)], fill="#5599ee", outline="black", width=18)
    draw.polygon([(3450, 1400), (4150, 800), (4150, 3300), (3450, 3950)], fill="#124077", outline="black", width=18)
    draw.rectangle((650, 1400, 3450, 3950), fill="#2466bb", outline="black", width=18)
    draw.rounded_rectangle((850, 1900, 3250, 3400), radius=45, fill="white", outline="black", width=14)
    title = ImageFont.load_default(size=210)
    serial = ImageFont.load_default(size=240)
    draw.text((1040, 2110), "BLUE STORAGE", font=title, fill="black")
    draw.text((1550, 2430), "BOX", font=title, fill="black")
    draw.text((1050, 2850), marker, font=serial, fill="black")
    source = root / "vision-image.jpg"
    image.save(source, "JPEG", quality=90)
    os.chmod(source, 0o644)  # Generated public fixture, not user content.
    del image, draw
    content = source.read_bytes()
    assert len(content) <= 12 * 1024**2

    with httpx.Client(timeout=30) as direct:
        response = direct.post("http://homebox:7745/api/v1/users/register",
                               json={"name": "Synthetic acceptance", "email": username, "password": password})
        response.raise_for_status()
    credentials = root / "qa-credentials.json"
    credentials.write_text(json.dumps({"username": username, "password": password}) + "\n")
    os.chmod(credentials, 0o600)

    prefix = "http://assistant-api:8000/api/assistant/v1"
    with httpx.Client(base_url=prefix, headers={"Origin": "http://localhost:8088"}, timeout=120) as client:
        login = client.post("/auth/login", json={"username": username, "password": password})
        login.raise_for_status()
        session = login.json()
        for role in ("chat", "vision"):
            assert session["ai"][role] == {"provider": "anthropic-messages", "model": "claude-haiku-4-5"}
        client.headers.update({"X-CSRF-Token": session["csrfToken"], "X-Collection-ID": session["collectionId"]})
        before = client.get("/items"); before.raise_for_status()
        assert before.json()["total"] == 0
        key = uuid4().hex
        started = time.monotonic()
        submitted = client.post("/capture-jobs", headers={"Idempotency-Key": key},
                                files={"image": ("image.jpg", content, "image/jpeg")})
        submit_seconds = time.monotonic() - started
        assert submitted.status_code == 202
        job = submitted.json()
        polls = 0
        states = [job["status"]]
        while job["status"] in {"queued", "running"}:
            assert time.monotonic() - started < 1050
            time.sleep(1)
            response = client.get("/capture-jobs/" + job["id"])
            response.raise_for_status()
            job = response.json()
            polls += 1
            if states[-1] != job["status"]:
                states.append(job["status"])
        assert job["status"] == "succeeded", job["warningCodes"]
        draft = job["result"]["draft"]
        recognized = json.dumps(draft).lower()
        assert marker in re.sub("[^A-Z0-9]", "", recognized.upper()), "Pixel-only marker was not recognized"
        assert "blue" in recognized and any(word in recognized for word in ("box", "bin", "container"))
        normalized = Path(os.environ.get("FIXTURE_STATE_PATH", "/fixture-state")) / "uploads" / (job["captureId"] + ".jpg")
        with Image.open(normalized) as saved:
            size = saved.size
        assert size == (1024, 1024)
        after = client.get("/items"); after.raise_for_status()
        assert after.json()["total"] == 0 and not job["result"].get("action")
        report = {"status": "passed", "sourcePixels": [4898, 4898], "sourcePixelCount": 4898**2,
                  "uploadBytes": len(content), "normalizedPixels": list(size),
                  "submissionHTTP": submitted.status_code, "submissionSeconds": round(submit_seconds, 3),
                  "totalSeconds": round(time.monotonic() - started, 3), "polls": polls,
                  "states": states, "pixelOnlyMarkerRead": True, "recognizedBlueStorageBox": True,
                  "inventoryWrites": 0, "model": session["ai"]["vision"]["model"]}
        (root / "vision-proof.json").write_text(json.dumps(report, indent=2) + "\n")
        os.chmod(root / "vision-proof.json", 0o600)
        (root / "vision-result-private.json").write_text(json.dumps(job, indent=2) + "\n")
        os.chmod(root / "vision-result-private.json", 0o600)
        print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
