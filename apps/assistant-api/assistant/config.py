import base64
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet


def read_secret(name: str) -> str:
    value = Path(os.environ[name]).read_text().strip()
    if len(value) < 32:
        raise RuntimeError(f"{name} must point to at least 32 random characters")
    return value


@dataclass
class Config:
    homebox: str
    mcp: str
    ollama: str
    model: str
    origin: str
    state: Path
    uploads: Path
    mcp_secret: str
    cipher: Fernet
    secure: bool
    session_ttl: int
    action_ttl: int
    capture_ttl: int
    max_upload: int
    max_pixels: int
    image_edge: int
    instance_id: str

    @classmethod
    def load(cls):
        origin = os.environ["ASSISTANT_PUBLIC_ORIGIN"]
        parsed = urlsplit(origin)
        if parsed.path or parsed.query or parsed.fragment or parsed.username or not parsed.netloc:
            raise RuntimeError("ASSISTANT_PUBLIC_ORIGIN must be an exact origin without a path")
        secure = parsed.scheme == "https"
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        insecure = os.environ.get("ASSISTANT_ALLOW_INSECURE_HTTP", "false") == "true"
        if not secure and not (parsed.scheme == "http" and local and insecure):
            raise RuntimeError("HTTPS is required outside explicit loopback-only disposable tests")
        secret = read_secret("ASSISTANT_SESSION_SECRET_FILE")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        return cls(
            homebox=os.environ.get("HOMEBOX_URL", "http://homebox:7745").rstrip("/"),
            mcp=os.environ.get("ASSISTANT_MCP_URL", "http://homebox-mcp:8001/mcp"),
            ollama=os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/"),
            model=os.environ.get("OLLAMA_MODEL", "qwen3-vl:2b"),
            origin=origin, state=Path(os.environ.get("ASSISTANT_STATE_PATH", "/data/assistant.db")),
            uploads=Path(os.environ.get("ASSISTANT_UPLOAD_DIR", "/data/uploads")),
            mcp_secret=read_secret("MCP_SHARED_SECRET_FILE"), cipher=Fernet(key), secure=secure,
            session_ttl=int(os.environ.get("ASSISTANT_SESSION_TTL_SECONDS", "28800")),
            action_ttl=int(os.environ.get("ASSISTANT_ACTION_TTL_SECONDS", "900")),
            capture_ttl=int(os.environ.get("ASSISTANT_CAPTURE_TTL_SECONDS", "3600")),
            max_upload=int(os.environ.get("ASSISTANT_MAX_UPLOAD_BYTES", "12582912")),
            max_pixels=int(os.environ.get("ASSISTANT_MAX_IMAGE_PIXELS", "24000000")),
            image_edge=int(os.environ.get("ASSISTANT_IMAGE_MAX_EDGE", "1024")),
            instance_id=str(UUID(os.environ["HOMEBOX_INSTANCE_ID"])),
        )
