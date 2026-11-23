"""Disposable upstreams and HTTP checks for the real pinned Caddy image."""

import http.server
import json
import pathlib
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request


def serve():
    for port, name in ((7745, "homebox"), (8000, "assistant"), (8080, "pwa")):
        class Handler(http.server.BaseHTTPRequestHandler):
            service = name

            def do_GET(self):
                payload = json.dumps({
                    "service": self.service,
                    "path": self.path,
                    "cookie": self.headers.get("Cookie"),
                    "origin": self.headers.get("Origin"),
                    "csrf": self.headers.get("X-CSRF-Token"),
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Event().wait()


def check():
    ca = pathlib.Path("/certs/caddy/pki/authorities/local/root.crt")
    deadline = time.monotonic() + 20
    while not ca.is_file():
        if time.monotonic() > deadline:
            raise AssertionError("Caddy did not initialize its local CA")
        time.sleep(0.1)
    context = ssl.create_default_context(cafile=ca)

    def get(host, path):
        request = urllib.request.Request(f"https://{host}/{path}", headers={
            "Cookie": "test-cookie=not-a-secret",
            "Origin": "https://inventory.home.arpa",
            "X-CSRF-Token": "test-csrf",
        })
        return urllib.request.urlopen(request, context=context, timeout=3)

    while True:
        try:
            response = get("inventory.home.arpa", "api/assistant/v1/session?fixture=1")
            break
        except (OSError, urllib.error.URLError):
            if time.monotonic() > deadline:
                raise
            time.sleep(0.1)
    data = json.load(response)
    assert data["service"] == "assistant", data
    assert data["path"] == "/api/assistant/v1/session?fixture=1", data
    assert data["cookie"] == "test-cookie=not-a-secret", data
    assert data["origin"] == "https://inventory.home.arpa", data
    assert data["csrf"] == "test-csrf", data
    assert response.headers["Cache-Control"] == "no-store", response.headers
    assert response.headers["X-Content-Type-Options"] == "nosniff", response.headers
    assert response.headers.get("Access-Control-Allow-Origin") is None
    assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
    print("PASS: API prefix, cookie, Origin and CSRF forwarding; no-store and same-origin headers")

    for path in ("", "t/t_fixture", "sw.js"):
        response = get("inventory.home.arpa", path)
        assert json.load(response)["service"] == "pwa", path
    print("PASS: companion shell, NFC route and worker stay on PWA upstream")

    for path in ("api/v1/status", "api/unrelated"):
        try:
            get("inventory.home.arpa", path)
        except urllib.error.HTTPError as error:
            assert error.code == 404, error
        else:
            raise AssertionError(f"Companion exposed {path}")
    print("PASS: Homebox API and unrelated API prefixes denied on companion origin")

    response = get("homebox.home.arpa", "api/v1/status")
    assert json.load(response)["service"] == "homebox"
    print("PASS: Homebox uses its own TLS hostname; CA and hostnames verified")


if __name__ == "__main__":
    {"serve": serve, "check": check}[sys.argv[1]]()
