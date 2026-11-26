import base64
import hashlib
import os
from dataclasses import dataclass, field
from ipaddress import IPv4Address, ip_address, ip_network
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.fernet import Fernet

from .providers import ProviderSettings
from .provider_config import ProviderRoles, resolve_provider_roles


def read_secret(name: str) -> str:
    value = Path(os.environ[name]).read_text().strip()
    if len(value) < 32:
        raise RuntimeError(f"{name} must point to at least 32 random characters")
    return value


def public_origin(value: str, name: str) -> str:
    try:
        parsed = urlsplit(value)
        port = parsed.port
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or value != "https://" + parsed.netloc
                or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)
                or any(c in value for c in "*\\%")
                or port is not None and not 1 <= port <= 65535
                or parsed.netloc.endswith(":")):
            raise ValueError
        try:
            ip_address(parsed.hostname)
        except ValueError:
            labels = parsed.hostname.split(".")
            if (len(parsed.hostname) > 253 or parsed.hostname.replace(".", "").isdigit()
                    or any(not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
                           or not label.isascii() or not label.replace("-", "").isalnum() for label in labels)):
                raise ValueError
        return value
    except ValueError:
        raise RuntimeError(f"{name} must be an exact HTTPS origin without credentials, a path, query, or fragment") from None


def lan_origin(value: str) -> str:
    if not value:
        return ""
    try:
        parsed = urlsplit(public_origin(value, "ASSISTANT_LAN_ORIGIN"))
        address = ip_address(parsed.hostname)
        if (not isinstance(address, IPv4Address) or parsed.port not in (None, 443)
                or not any(address in ip_network(network) for network in
                           ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))):
            raise ValueError
        return "https://" + str(address)
    except ValueError:
        raise RuntimeError("ASSISTANT_LAN_ORIGIN must be an exact RFC1918 IPv4 HTTPS origin on port 443") from None


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
    analysis_queue_limit: int = 8
    analysis_queue_timeout: int = 120
    analysis_timeout: int = 900
    capture_sync_timeout: int = 90
    ai_provider: str = "ollama"
    ai_base_url: str | None = None
    ai_model: str | None = None
    ai_chat_model: str | None = None
    ai_vision_model: str | None = None
    ai_api_key: str | None = field(default=None, repr=False)
    ai_roles: ProviderRoles | None = field(default=None, repr=False)
    chat_timeout: int = 60
    conversation_ttl: int = 1800
    instance_label: str = ""
    lan_origin: str = ""
    homebox_public_origin: str = ""

    @property
    def public_companion_origins(self):
        origins = [self.origin]
        if self.lan_origin:
            origins.append(self.lan_origin)
            if self.homebox_public_origin:
                host = urlsplit(self.homebox_public_origin).hostname
                origins.append("https://" + (f"[{host}]" if ":" in host else host))
        return tuple(dict.fromkeys(origins))

    @property
    def provider_settings(self):
        return self.vision_provider_settings

    @property
    def vision_provider_settings(self):
        if self.ai_roles is not None:
            return self.ai_roles.vision
        return ProviderSettings(kind=self.ai_provider,
                                base_url=self.ai_base_url or (None if self.ai_provider == "gemini" else self.ollama),
                                model=self.ai_vision_model or self.ai_model or
                                (None if self.ai_provider == "gemini" else self.model), api_key=self.ai_api_key)

    @property
    def chat_provider_settings(self):
        if self.ai_roles is not None:
            return self.ai_roles.chat
        return ProviderSettings(kind=self.ai_provider,
                                base_url=self.ai_base_url or (None if self.ai_provider == "gemini" else self.ollama),
                                model=self.ai_chat_model or self.ai_model or
                                (None if self.ai_provider == "gemini" else self.model), api_key=self.ai_api_key)

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
        local_origin = lan_origin(os.environ.get("ASSISTANT_LAN_ORIGIN", ""))
        if local_origin and not secure:
            raise RuntimeError("ASSISTANT_LAN_ORIGIN requires HTTPS for ASSISTANT_PUBLIC_ORIGIN")
        native_origin = os.environ.get("HOMEBOX_PUBLIC_ORIGIN", "") if local_origin else ""
        if native_origin:
            native_origin = public_origin(native_origin, "HOMEBOX_PUBLIC_ORIGIN")
        secret = read_secret("ASSISTANT_SESSION_SECRET_FILE")
        key = base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest())
        ollama_url = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
        ollama_model = os.environ.get("OLLAMA_MODEL", "qwen3-vl:2b")
        roles = resolve_provider_roles(os.environ, ollama_url=ollama_url, ollama_model=ollama_model,
                                       key_reader=lambda path: Path(path).read_text())
        config = cls(
            homebox=os.environ.get("HOMEBOX_URL", "http://homebox:7745").rstrip("/"),
            mcp=os.environ.get("ASSISTANT_MCP_URL", "http://homebox-mcp:8001/mcp"),
            ollama=ollama_url, model=ollama_model,
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
            analysis_queue_limit=int(os.environ.get("ASSISTANT_ANALYSIS_QUEUE_LIMIT", "8")),
            analysis_queue_timeout=int(os.environ.get("ASSISTANT_ANALYSIS_QUEUE_TIMEOUT_SECONDS", "120")),
            analysis_timeout=int(os.environ.get("ASSISTANT_ANALYSIS_TIMEOUT_SECONDS", "900")),
            capture_sync_timeout=int(os.environ.get("ASSISTANT_CAPTURE_SYNC_TIMEOUT_SECONDS", "90")),
            ai_provider=roles.vision.kind, ai_base_url=roles.vision.base_url,
            ai_model=roles.vision.model, ai_api_key=roles.vision.api_key,
            ai_chat_model=roles.chat.model, ai_vision_model=roles.vision.model, ai_roles=roles,
            chat_timeout=int(os.environ.get("ASSISTANT_CHAT_TURN_TIMEOUT_SECONDS", "60")),
            conversation_ttl=int(os.environ.get("ASSISTANT_CONVERSATION_TTL_SECONDS", "1800")),
            instance_label=os.environ.get("ASSISTANT_INSTANCE_LABEL", "")[:100],
            lan_origin=local_origin, homebox_public_origin=native_origin,
        )
        for name, maximum in (("analysis_queue_limit", 32), ("analysis_queue_timeout", 600),
                              ("analysis_timeout", 3600), ("capture_sync_timeout", 120),
                              ("chat_timeout", 120), ("conversation_ttl", 28800)):
            if not 1 <= getattr(config, name) <= maximum:
                raise RuntimeError(f"{name} must be between 1 and {maximum}")
        if config.capture_ttl <= config.analysis_queue_timeout + config.analysis_timeout:
            raise RuntimeError("Capture retention must exceed the total queue and analysis budgets")
        config.vision_provider_settings  # Validate protocol, models, URL, and secret without exposing them.
        config.chat_provider_settings
        return config
