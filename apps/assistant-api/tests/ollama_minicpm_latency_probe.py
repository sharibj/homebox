"""Real TCP/HTTP timeout probe against a synthetic Ollama, never a live model.

Run each role in a fresh isolated Docker process. The default 58-second server
delay is deliberate test input, not a model benchmark. This is not collected by
pytest, so the regular suite does not acquire two minutes of artificial delay.
"""

import argparse
import io
import json
import runpy
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

# Disposable fixture secrets must exist before importing the real API app.
runpy.run_path(str(Path(__file__).with_name("conftest.py")))

from assistant.images import normalize
from assistant.main import PREFIX, create_app
from test_api import FakeClients
from test_capture_jobs import terminal
from test_ollama_minicpm import (ITEM, MESSAGE, MODEL, MUTATIONS, assert_native_payload,
                                 clear_provider_environment, login, minicpm_config,
                                 structured_value, synthetic_photo)


class SyntheticOllama:
    def __init__(self, photo, delay):
        self.photo, self.delay, self.requests, self.errors = photo, delay, [], []
        self.received, self.finished = threading.Event(), threading.Event()
        self.elapsed = None
        self.jpeg = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                try:
                    self.connection.settimeout(5)
                    assert self.path == "/api/chat"
                    size = int(self.headers["Content-Length"])
                    assert 0 < size <= 2 * 1024 * 1024
                    content = self.rfile.read(size)
                    request = httpx.Request("POST", owner.url + self.path, headers=dict(self.headers), content=content)
                    payload = assert_native_payload(request, owner.photo, owner.jpeg)
                    owner.requests.append(payload)
                    owner.received.set()
                    started = time.monotonic()
                    # Deliberate synthetic inference latency exercises HTTPX's
                    # real socket read timeout and the real API/job deadline.
                    time.sleep(owner.delay)
                    data = json.dumps({
                        "model": MODEL, "done": True, "done_reason": "stop",
                        "message": {"role": "assistant", "content": json.dumps(structured_value(owner.photo))},
                    }).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
                    self.wfile.flush()
                    owner.elapsed = time.monotonic() - started
                except Exception as error:
                    owner.errors.append(type(error).__name__)
                    owner.received.set()
                finally:
                    owner.finished.set()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.url = "http://127.0.0.1:" + str(self.server.server_port)
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .05}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        assert not self.thread.is_alive()


def run(role, delay):
    photo = role == "vision"
    with tempfile.TemporaryDirectory(prefix="minicpm-latency-") as directory, pytest.MonkeyPatch.context() as patch:
        clear_provider_environment(patch)
        with SyntheticOllama(photo, delay) as backend:
            cfg = minicpm_config(patch, Path(directory), backend.url)
            raw_photo = synthetic_photo()
            backend.jpeg = normalize(raw_photo, cfg)
            fake = FakeClients()
            item_id = fake.add(ITEM)
            fake.items[item_id]["locationPath"] = "Synthetic room / Drawer"
            app = create_app(cfg, fake)
            assert app.state.vision.provider is app.state.vision.chat_provider
            assert cfg.chat_timeout == 120 and cfg.analysis_timeout == 900
            transport_timeouts = []

            async def capture_timeout(request):
                transport_timeouts.append(request.extensions["timeout"])

            app.state.vision.provider._client.event_hooks["request"].append(capture_timeout)
            with TestClient(app, base_url=cfg.origin, headers={"Origin": cfg.origin}) as client:
                metadata = login(client)
                assert metadata["ai"] == {
                    name: {"provider": "ollama", "model": MODEL} for name in ("chat", "vision")}
                started = time.monotonic()
                if photo:
                    response = client.post(
                        PREFIX + "/capture-jobs",
                        files={"image": ("synthetic.png", raw_photo, "image/png")},
                        headers={"Idempotency-Key": "synthetic-minicpm-latency"},
                    )
                    assert response.status_code == 202, response.text
                    assert backend.received.wait(timeout=5), "Synthetic Ollama did not receive the queued job"
                    assert backend.finished.wait(timeout=delay + 10), "Synthetic Ollama exceeded its bounded delay"
                    assert backend.errors == [], backend.errors
                    result = terminal(client, response.json(), timeout=5)
                    assert result["status"] == "succeeded"
                    assert result["result"]["draft"]["name"] == "Synthetic local box"
                    assert result["result"]["warnings"]
                    assert next(cfg.uploads.iterdir()).read_bytes() == backend.jpeg
                else:
                    response = client.post(PREFIX + "/chat", json={"message": MESSAGE})
                    assert response.status_code == 200, response.text
                    assert [value["id"] for value in response.json()["items"]] == [item_id]
                    assert "Synthetic room / Drawer" in response.json()["message"]
                elapsed = time.monotonic() - started
                assert backend.errors == [], backend.errors
                assert len(backend.requests) == len(transport_timeouts) == 1
                assert backend.elapsed is not None and backend.elapsed >= delay
                assert delay <= elapsed < delay + 15
                assert client.get(PREFIX + "/actions").json()["actions"] == []
                assert all(fake.calls[name] == 0 for name in MUTATIONS)
                read_timeout = transport_timeouts[0]["read"]
                assert (890 if photo else 115) <= read_timeout <= (900 if photo else 120)
                payload = backend.requests[0]
                dimensions = None
                if photo:
                    with Image.open(io.BytesIO(backend.jpeg)) as image:
                        dimensions = list(image.size)
                return {
                    "role": role, "provider": "ollama", "model": MODEL,
                    "syntheticLoopbackHttp": True, "authenticatedApi": True,
                    "delaySeconds": delay, "modelHttpElapsedSeconds": round(backend.elapsed, 3),
                    "apiElapsedSeconds": round(elapsed, 3), "apiStatus": response.status_code,
                    "terminalJobStatus": "succeeded" if photo else None,
                    "nativeCalls": 1, "nativeReadTimeoutSeconds": round(read_timeout, 3),
                    "maxOutputTokens": payload["options"]["num_predict"],
                    "normalizedJpegDimensions": dimensions, "inventoryWrites": 0,
                }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", choices=("chat", "vision"), required=True)
    parser.add_argument("--delay-seconds", type=float, default=58)
    arguments = parser.parse_args()
    if not 0 < arguments.delay_seconds <= 58:
        parser.error("Use a positive synthetic delay of at most 58 seconds")
    print(json.dumps(run(arguments.role, arguments.delay_seconds)))
