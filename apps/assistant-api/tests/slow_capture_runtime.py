"""Disposable, real-HTTP >180-second capture regression. Never uses real inventory."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from PIL import Image


def build_app():
    from assistant.config import Config
    from assistant.main import create_app
    from test_api import FakeClients
    return create_app(Config.load(), FakeClients())


class SlowModel(BaseHTTPRequestHandler):
    calls = 0

    def log_message(self, *args):
        pass

    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        assert self.path == "/api/chat" and request["stream"] is False
        assert request["options"] == {"temperature": 0, "num_ctx": 4096, "num_predict": 1200}
        assert request["model"] == "qwen3-vl:2b"
        SlowModel.calls += 1
        time.sleep(185)
        body = json.dumps({"done": True, "message": {"content": json.dumps({
            "items": [{"name": "Synthetic blue rectangle", "confidence": .8}],
            "image_notes": "Disposable nonprivate test fixture",
        })}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    with tempfile.TemporaryDirectory(prefix="capture-runtime-") as directory:
        root = Path(directory)
        session_secret, mcp_secret = root / "session-secret", root / "mcp-secret"
        session_secret.write_text("disposable-session-secret-32-characters")
        mcp_secret.write_text("disposable-mcp-secret-at-least-32-characters")
        environment = {
            **os.environ, "ASSISTANT_PUBLIC_ORIGIN": "http://localhost:8000",
            "ASSISTANT_ALLOW_INSECURE_HTTP": "true", "ASSISTANT_STATE_PATH": str(root / "state.db"),
            "ASSISTANT_UPLOAD_DIR": str(root / "uploads"),
            "ASSISTANT_SESSION_SECRET_FILE": str(session_secret), "MCP_SHARED_SECRET_FILE": str(mcp_secret),
            "HOMEBOX_INSTANCE_ID": "56cd30b1-7bdd-466e-9459-8b402cd3349c",
            "OLLAMA_URL": "http://127.0.0.1:11434",
        }
        for key in list(environment):
            if key.startswith("AI_") or key.startswith("ASSISTANT_ANALYSIS_") or key == "ASSISTANT_CAPTURE_SYNC_TIMEOUT_SECONDS":
                del environment[key]
        server = ThreadingHTTPServer(("127.0.0.1", 11434), SlowModel)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        process = subprocess.Popen([
            sys.executable, "-m", "uvicorn", "slow_capture_runtime:build_app", "--factory",
            "--app-dir", "/app/tests", "--host", "127.0.0.1", "--port", "8000", "--no-access-log",
        ], env=environment)
        try:
            with httpx.Client(base_url="http://localhost:8000", timeout=10, trust_env=False,
                              headers={"Origin": environment["ASSISTANT_PUBLIC_ORIGIN"]}) as client:
                for attempt in range(100):
                    try:
                        if client.get("/healthz").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(.1)
                else:
                    raise AssertionError("Disposable API did not start")
                prefix = "/api/assistant/v1"
                login = client.post(prefix + "/auth/login", json={"username": "alice", "password": "disposable"}).json()
                client.headers.update({"X-CSRF-Token": login["csrfToken"], "X-Collection-ID": login["collectionId"]})
                image = io.BytesIO()
                Image.new("RGB", (1024, 1024), "blue").save(image, "JPEG")
                started = time.monotonic()
                submitted = client.post(prefix + "/capture-jobs",
                                        files={"image": ("synthetic.jpg", image.getvalue(), "image/jpeg")},
                                        headers={"Idempotency-Key": "lost-response-recovery-key"})
                assert submitted.status_code == 202, submitted.text
                submit_seconds = time.monotonic() - started
                assert submit_seconds < 5
                # Discard the POST response and recover using only the key.
                recovered = client.get(prefix + "/capture-jobs", params={"submissionKey": "lost-response-recovery-key"}).json()["jobs"]
                assert len(recovered) == 1
                job_id = recovered[0]["id"]
                print(json.dumps({"phase": "submitted_and_recovered", "submit_seconds": round(submit_seconds, 3)}), flush=True)
                with httpx.Client(base_url="http://localhost:8000", timeout=10, trust_env=False,
                                  cookies=client.cookies, headers={"X-Collection-ID": login["collectionId"]}) as reloaded:
                    while True:
                        response = reloaded.get(prefix + "/capture-jobs/" + job_id)
                        assert response.status_code == 200, response.text
                        job = response.json()
                        if job["status"] not in {"queued", "running"}:
                            break
                        assert time.monotonic() - started < 210
                        time.sleep(1)
                    elapsed = time.monotonic() - started
                    assert elapsed > 180 and job["status"] == "succeeded", job
                    assert job["result"]["draft"]["name"] == "Synthetic blue rectangle"
                    assert reloaded.get(prefix + "/items").json()["items"] == []
                assert SlowModel.calls == 1
                assert (root / "uploads" / (job["captureId"] + ".jpg")).exists()
                print(json.dumps({"phase": "complete_after_browser_budget", "seconds": round(elapsed, 3),
                                  "model_calls": SlowModel.calls, "status": job["status"],
                                  "inventory_items": 0, "photo_retained": True}), flush=True)
        finally:
            process.terminate()
            process.wait(timeout=10)
            server.shutdown()


if __name__ == "__main__":
    main()
