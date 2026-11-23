"""Deterministic protocol fixture, never an inference-quality test."""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Fixture(BaseHTTPRequestHandler):
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        draft = {
            "items": [{"name": "Camping stove", "manufacturer": "Test fixture",
                       "model": "Fixture-1", "suggested_tags": ["camping"],
                       "quantity_estimate": 1, "confidence": .8, "needs_user_review": True}],
            "image_notes": "Deterministic fixture, not model inference",
        }
        if "kind" in request.get("format", {}).get("properties", {}):
            draft = {"kind": "search", "query": "Camping stove"}
        result = json.dumps({"message": {"content": json.dumps(draft)}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 11434), Fixture).serve_forever()
