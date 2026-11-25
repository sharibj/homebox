import traceback
from dataclasses import FrozenInstanceError

import pytest

from assistant.provider_config import ProviderRoles, resolve_provider_roles
from assistant.providers import ProviderSettings


def resolve(env=None, keys=None, calls=None, **kwargs):
    def read_key(path):
        if calls is not None:
            calls.append(path)
        return (keys or {})[path]

    return resolve_provider_roles(
        env or {}, ollama_url=kwargs.pop("ollama_url", "http://ollama:11434"),
        ollama_model=kwargs.pop("ollama_model", "qwen3-vl:2b"), key_reader=read_key, **kwargs,
    )


def external(**extra):
    return {"AI_PROVIDER": "openai-compatible", "AI_BASE_URL": "https://shared.test/v1",
            "AI_MODEL": "common-model", "AI_API_KEY_FILE": "/fake/shared", **extra}


def test_default_roles_preserve_native_ollama_without_reading_keys():
    calls = []
    roles = resolve(calls=calls)
    assert roles == ProviderRoles(ProviderSettings(), ProviderSettings())
    assert calls == []


def test_legacy_ollama_url_model_used_for_both_roles():
    roles = resolve(ollama_url="http://custom-ollama:11434/", ollama_model="custom-legacy")
    for settings in (roles.chat, roles.vision):
        assert settings.base_url == "http://custom-ollama:11434"
        assert settings.model == "custom-legacy"


def test_shared_ollama_fields_override_legacy_defaults():
    roles = resolve({"AI_BASE_URL": "http://new-ollama:11434", "AI_MODEL": "new-common"})
    assert roles.chat == roles.vision == ProviderSettings("ollama", "http://new-ollama:11434", "new-common")


def test_shared_external_settings_and_key_are_loaded_once():
    calls = []
    roles = resolve(external(), keys={"/fake/shared": "shared-key"}, calls=calls)
    assert roles.chat == roles.vision == ProviderSettings(
        "openai-compatible", "https://shared.test/v1", "common-model", "shared-key")
    assert calls == ["/fake/shared"]


def test_independent_models_share_same_endpoint_and_key():
    roles = resolve(external(AI_CHAT_MODEL="chat-small", AI_VISION_MODEL="vision-large"),
                    keys={"/fake/shared": "shared-key"})
    assert roles.chat.model == "chat-small"
    assert roles.vision.model == "vision-large"
    assert roles.chat.base_url == roles.vision.base_url == "https://shared.test/v1"
    assert roles.chat.api_key == roles.vision.api_key == "shared-key"


def test_fully_independent_external_endpoints_models_and_keys_without_common_fields():
    calls = []
    env = {
        "AI_CHAT_PROVIDER": "openai-compatible", "AI_CHAT_BASE_URL": "https://chat.test/v1",
        "AI_CHAT_MODEL": "chat-model", "AI_CHAT_API_KEY_FILE": "/fake/chat",
        "AI_VISION_PROVIDER": "openai-compatible", "AI_VISION_BASE_URL": "https://vision.test/api/v1",
        "AI_VISION_MODEL": "vision-model", "AI_VISION_API_KEY_FILE": "/fake/vision",
    }
    roles = resolve(env, keys={"/fake/chat": "chat-key", "/fake/vision": "vision-key"}, calls=calls)
    assert roles.chat == ProviderSettings("openai-compatible", "https://chat.test/v1", "chat-model", "chat-key")
    assert roles.vision == ProviderSettings("openai-compatible", "https://vision.test/api/v1", "vision-model", "vision-key")
    assert calls == ["/fake/chat", "/fake/vision"]


def test_one_role_switches_to_native_without_inheriting_external_url_key_or_model_default():
    calls = []
    env = external(AI_VISION_PROVIDER="ollama", AI_VISION_MODEL="native-vision")
    roles = resolve(env, keys={"/fake/shared": "shared-key"}, calls=calls)
    assert roles.chat.kind == "openai-compatible"
    assert roles.vision == ProviderSettings("ollama", "http://ollama:11434", "native-vision")
    assert calls == ["/fake/shared"]


@pytest.mark.parametrize("role_overrides", [
    {"AI_CHAT_BASE_URL": "https://other.test/v1"},
    {"AI_CHAT_BASE_URL": "http://shared.test/v1"},
    {"AI_CHAT_BASE_URL": "https://shared.test:8443/v1"},
    {"AI_CHAT_BASE_URL": "https://shared.test/other"},
    {"AI_CHAT_BASE_URL": "https://shared.test/V1"},
    {"AI_CHAT_PROVIDER": "ollama", "AI_CHAT_BASE_URL": "https://shared.test/v1"},
])
def test_common_key_never_crosses_a_changed_trust_endpoint(role_overrides):
    env = external(**role_overrides)
    roles = resolve(env, keys={"/fake/shared": "shared-key"})
    assert roles.chat.api_key is None
    assert roles.vision.api_key == "shared-key"
    # Explicit common model fallback is independent of credential inheritance.
    assert roles.chat.model == "common-model"


@pytest.mark.parametrize("role_url", ["https://shared.test/v1///", "https://SHARED.test:443/v1/"])
def test_equivalent_endpoint_spelling_can_inherit_common_key(role_url):
    roles = resolve(external(AI_CHAT_BASE_URL=role_url), keys={"/fake/shared": "shared-key"})
    assert roles.chat.api_key == roles.vision.api_key == "shared-key"


def test_explicit_role_key_overrides_shared_even_on_same_endpoint():
    roles = resolve(external(AI_CHAT_API_KEY_FILE="/fake/chat"),
                    keys={"/fake/chat": "chat-key", "/fake/shared": "shared-key"})
    assert roles.chat.api_key == "chat-key"
    assert roles.vision.api_key == "shared-key"


@pytest.mark.parametrize("empty", [None, "", " \n\t"])
def test_explicit_empty_role_key_file_selects_anonymous_not_shared_key(empty):
    calls = []
    roles = resolve(external(AI_CHAT_API_KEY_FILE="/fake/anonymous"),
                    keys={"/fake/anonymous": empty, "/fake/shared": "shared-key"}, calls=calls)
    assert roles.chat.api_key is None
    assert roles.vision.api_key == "shared-key"
    assert calls == ["/fake/anonymous", "/fake/shared"]


def test_empty_role_key_path_means_unset_and_inherits_same_endpoint_key():
    roles = resolve(external(AI_CHAT_API_KEY_FILE=""), keys={"/fake/shared": "shared-key"})
    assert roles.chat.api_key == "shared-key"


@pytest.mark.parametrize("missing", ["AI_CHAT_BASE_URL", "AI_CHAT_MODEL"])
def test_external_role_requires_url_and_model_without_using_legacy_ollama_defaults(missing):
    env = {"AI_CHAT_PROVIDER": "openai-compatible", "AI_CHAT_BASE_URL": "https://chat.test/v1",
           "AI_CHAT_MODEL": "chat-model"}
    del env[missing]
    with pytest.raises(ValueError, match="AI_CHAT requires a base URL and model"):
        resolve(env)


def test_unused_incomplete_shared_external_settings_do_not_break_explicit_roles_or_read_key():
    env = external(AI_CHAT_PROVIDER="ollama", AI_VISION_PROVIDER="ollama")
    del env["AI_BASE_URL"]
    del env["AI_MODEL"]
    calls = []
    roles = resolve(env, calls=calls)
    assert roles.chat == roles.vision == ProviderSettings()
    assert calls == []


def test_unused_malformed_shared_endpoint_does_not_break_explicit_roles_or_read_key():
    env = external(AI_BASE_URL="https://user:secret@shared.test",
                   AI_CHAT_BASE_URL="https://chat.test/v1", AI_VISION_BASE_URL="https://vision.test/v1")
    calls = []
    roles = resolve(env, calls=calls)
    assert roles.chat.api_key is None and roles.vision.api_key is None
    assert calls == []


def test_key_reader_failure_is_sanitized_and_no_secret_path_appears_in_traceback():
    def key_reader(path):
        raise RuntimeError("do-not-expose-file-path-or-key")

    with pytest.raises(ValueError) as error:
        resolve_provider_roles(external(), ollama_url="http://ollama:11434", ollama_model="legacy",
                               key_reader=key_reader)
    rendered = "".join(traceback.format_exception(error.value))
    assert str(error.value) == "AI_API_KEY_FILE could not be read"
    assert "/fake/shared" not in rendered
    assert "do-not-expose-file-path-or-key" not in rendered


def test_invalid_role_endpoint_error_does_not_echo_credentials_or_read_keys():
    calls = []
    with pytest.raises(ValueError) as error:
        resolve(external(AI_VISION_BASE_URL="https://user:do-not-expose@vision.test"), calls=calls)
    assert str(error.value) == "AI_VISION provider configuration is invalid"
    assert "do-not-expose" not in str(error.value)
    assert calls == []


def test_invalid_key_content_and_role_repr_do_not_expose_key():
    invalid_keys = {"/fake/shared": "do-not-expose\nkey"}
    with pytest.raises(ValueError) as error:
        resolve(external(), keys=invalid_keys)
    assert "do-not-expose" not in "".join(traceback.format_exception(error.value))
    roles = resolve(external(), keys={"/fake/shared": "private-key"})
    assert "private-key" not in repr(roles)
    with pytest.raises(TypeError):
        hash(roles)
    with pytest.raises(FrozenInstanceError):
        roles.chat = ProviderSettings()


def test_explicit_common_model_can_be_shared_across_distinct_protocols():
    roles = resolve({"AI_MODEL": "explicit-common", "AI_CHAT_PROVIDER": "openai-compatible",
                     "AI_CHAT_BASE_URL": "https://chat.test/v1"})
    assert roles.chat.model == roles.vision.model == "explicit-common"
    assert roles.chat.kind == "openai-compatible" and roles.vision.kind == "ollama"


def test_no_key_reader_call_for_unconfigured_credentials_even_with_external_endpoints():
    calls = []
    env = external()
    del env["AI_API_KEY_FILE"]
    roles = resolve(env, calls=calls)
    assert roles.chat.api_key is None and roles.vision.api_key is None
    assert calls == []


def test_explicit_external_roles_need_no_shared_external_url_or_model():
    env = {
        "AI_PROVIDER": "openai-compatible", "AI_API_KEY_FILE": "/fake/unused-shared",
        "AI_CHAT_BASE_URL": "https://chat.test/v1", "AI_CHAT_MODEL": "chat-model",
        "AI_VISION_BASE_URL": "https://vision.test/v1", "AI_VISION_MODEL": "vision-model",
    }
    calls = []
    roles = resolve(env, calls=calls)
    assert roles.chat == ProviderSettings("openai-compatible", "https://chat.test/v1", "chat-model")
    assert roles.vision == ProviderSettings("openai-compatible", "https://vision.test/v1", "vision-model")
    assert calls == []


def test_native_anthropic_chat_role_does_not_change_default_local_vision_role():
    roles = resolve({
        "AI_CHAT_PROVIDER": "anthropic-messages",
        "AI_CHAT_BASE_URL": "http://gateway.test:6655/anthropic/",
        "AI_CHAT_MODEL": "claude-haiku-4-5", "AI_CHAT_API_KEY_FILE": "/fake/native-chat",
    }, keys={"/fake/native-chat": "native-chat-key"})
    assert roles.chat == ProviderSettings("anthropic-messages", "http://gateway.test:6655/anthropic",
                                          "claude-haiku-4-5", "native-chat-key")
    assert roles.vision == ProviderSettings()


def test_native_anthropic_shared_key_never_inherits_across_protocol_on_same_endpoint():
    roles = resolve({
        "AI_PROVIDER": "anthropic-messages", "AI_BASE_URL": "https://gateway.test/anthropic",
        "AI_MODEL": "common-model", "AI_API_KEY_FILE": "/fake/native-common",
        "AI_CHAT_PROVIDER": "openai-compatible", "AI_CHAT_BASE_URL": "https://gateway.test/anthropic",
    }, keys={"/fake/native-common": "native-common-key"})
    assert roles.chat.api_key is None
    assert roles.vision.api_key == "native-common-key"


@pytest.mark.parametrize("missing", ["AI_CHAT_BASE_URL", "AI_CHAT_MODEL"])
def test_native_anthropic_requires_explicit_external_settings_and_has_no_cloud_fallback(missing):
    env = {"AI_CHAT_PROVIDER": "anthropic-messages", "AI_CHAT_BASE_URL": "https://gateway.test/anthropic",
           "AI_CHAT_MODEL": "claude-haiku-4-5"}
    del env[missing]
    with pytest.raises(ValueError, match="AI_CHAT requires a base URL and model"):
        resolve(env)
