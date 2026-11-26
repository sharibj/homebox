"""Immutable provider settings for one operation, without changing ENV defaults."""

import hashlib
import json
from contextlib import nullcontext
from urllib.parse import urlsplit

from .ai_settings import AISnapshot as ProviderSnapshot
from .provider_config import ProviderRoles


def resource_identity(settings):
    parsed = urlsplit(settings.base_url)
    endpoint = (settings.kind, parsed.scheme.lower(), parsed.hostname.lower(),
                parsed.port or (443 if parsed.scheme == "https" else 80), parsed.path.rstrip("/"))
    return hashlib.sha256(json.dumps(endpoint, separators=(",", ":")).encode()).hexdigest()


def operation_for(vision):
    operation = getattr(vision, "operation", None)
    return operation() if callable(operation) else nullcontext()


def snapshot_for(vision, config):
    current = getattr(vision, "current_snapshot", None)
    if callable(current):
        return current()
    return ProviderSnapshot(ProviderRoles(chat=config.chat_provider_settings, vision=config.vision_provider_settings))


def chat_configuration(snapshot):
    settings = snapshot.roles.chat
    legacy = resource_identity(settings) + ":" + settings.model
    if snapshot.chat_token is None:
        return legacy
    return json.dumps([legacy, snapshot.chat_token], separators=(",", ":"))


def vision_configuration(snapshot):
    settings = snapshot.roles.vision
    value = {"provider": settings.kind, "model": settings.model, "baseUrlFingerprint": resource_identity(settings)}
    if snapshot.vision_token is not None:
        value["settingsToken"] = snapshot.vision_token
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
