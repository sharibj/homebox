"""Resolve deployment-only chat and vision settings without crossing key scopes."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from .providers import GEMINI_BASE_URL, GEMINI_MODEL, ProviderSettings


@dataclass(frozen=True)
class ProviderRoles:
    chat: ProviderSettings
    vision: ProviderSettings

    # Do not turn credentials into an endpoint/resource fingerprint.
    __hash__ = None


def _value(env, name):
    return env.get(name, "").strip() or None


def _endpoint(settings):
    parsed = urlsplit(settings.base_url)
    return (
        settings.kind, parsed.scheme.lower(), parsed.hostname.lower(),
        parsed.port or (443 if parsed.scheme == "https" else 80),
        parsed.path.rstrip("/"),
    )


def resolve_provider_roles(
    env: Mapping[str, str], *, ollama_url: str, ollama_model: str,
    key_reader: Callable[[str], str | None],
) -> ProviderRoles:
    """Resolve both roles, loading only explicitly selected credential files.

    Role models may inherit an explicit common model across providers. Common
    credentials may inherit only within the same protocol and complete endpoint.
    An explicit role key file with empty contents opts into anonymous access.
    """
    shared_kind = _value(env, "AI_PROVIDER") or "ollama"
    shared_url = _value(env, "AI_BASE_URL")
    shared_model = _value(env, "AI_MODEL")
    shared_key_file = _value(env, "AI_API_KEY_FILE")
    shared_endpoint = None
    endpoint_url = shared_url or (ollama_url if shared_kind == "ollama"
                                  else GEMINI_BASE_URL if shared_kind == "gemini" else None)
    if endpoint_url:
        try:
            # The common model need not exist if both role models are explicit.
            shared_endpoint = _endpoint(ProviderSettings(shared_kind, endpoint_url, "endpoint-only"))
        except ValueError:
            # Invalid unused shared settings must not break independent roles.
            pass

    settings_by_role = {}
    for role in ("CHAT", "VISION"):
        prefix = "AI_" + role
        kind = _value(env, prefix + "_PROVIDER") or shared_kind
        base_url = _value(env, prefix + "_BASE_URL")
        if not base_url and kind == shared_kind:
            base_url = shared_url
        if not base_url and kind == "ollama":
            base_url = ollama_url
        if not base_url and kind == "gemini":
            base_url = GEMINI_BASE_URL
        model = _value(env, prefix + "_MODEL") or shared_model
        if not model and kind == "ollama":
            model = ollama_model
        if not model and kind == "gemini":
            model = GEMINI_MODEL
        if not base_url or not model:
            raise ValueError(prefix + " requires a base URL and model for its provider")
        try:
            settings_by_role[role] = ProviderSettings(kind, base_url, model)
        except ValueError:
            raise ValueError(prefix + " provider configuration is invalid") from None

    loaded_keys = {}

    def read_key(path, label):
        if path not in loaded_keys:
            try:
                value = key_reader(path)
                loaded_keys[path] = (value.strip() or None) if value is not None else None
            except Exception:
                raise ValueError(label + " could not be read") from None
        return loaded_keys[path]

    resolved = {}
    for role, settings in settings_by_role.items():
        label = "AI_" + role + "_API_KEY_FILE"
        key_file = _value(env, label)
        if key_file is None and shared_key_file and _endpoint(settings) == shared_endpoint:
            key_file = shared_key_file
            label = "AI_API_KEY_FILE"
        key = read_key(key_file, label) if key_file is not None else None
        try:
            resolved[role] = replace(settings, api_key=key)
        except ValueError:
            raise ValueError(label + " contains an invalid API key") from None
    return ProviderRoles(chat=resolved["CHAT"], vision=resolved["VISION"])
