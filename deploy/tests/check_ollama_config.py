"""Offline checks for the host Ollama profile and existing provider settings."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


MODES = (
    "default", "host", "host-linux", "host-lan", "haiku-both", "external-chat",
    "external-vision", "gemini-chat", "gemini-vision", "gemini-both",
)
KEYS = {
    "/run/secrets/ai/ai_api_key": "synthetic-shared-key",
    "/run/secrets/ai/ai_chat_api_key": "synthetic-chat-key",
    "/run/secrets/ai/ai_vision_api_key": "synthetic-vision-key",
}


def settings_file(path):
    return dict(line.split("=", 1) for raw in Path(path).read_text().splitlines()
                if (line := raw.strip()) and not line.startswith("#"))


def prepare():
    """Write disposable fixtures, never read the protected deployment .env."""
    for name in ("config.py", "provider_config.py", "providers.py"):
        source = (Path("/public/assistant") / name).read_bytes()
        candidate = (Path("/app/assistant") / name).read_bytes()
        if source != candidate:
            raise ValueError("API test image has stale configuration source: " + name)
        print(f"Configuration source {name}: {hashlib.sha256(source).hexdigest()}")
    base = settings_file("/public/default.env")
    base["HOMEBOX_INSTANCE_ID"] = "11111111-1111-4111-8111-111111111111"
    profile = settings_file("/public/host.env")
    required = {"AI_API_KEY_FILE", "ASSISTANT_CHAT_TURN_TIMEOUT_SECONDS"}
    required.update(f"AI_{role}_{field}" for role in ("CHAT", "VISION")
                    for field in ("PROVIDER", "BASE_URL", "MODEL", "API_KEY_FILE"))
    if set(profile) != required:
        raise ValueError("Host profile must explicitly select both roles and clear all three keys")
    for mode in MODES:
        env = dict(base)
        if mode in {"haiku-both", "host", "host-linux", "host-lan"}:
            # Reproduce switching an existing gateway, including stale role overrides.
            env.update(AI_PROVIDER="anthropic-messages",
                       AI_BASE_URL="http://host.docker.internal:6655/anthropic",
                       AI_MODEL="claude-haiku-4-5",
                       AI_API_KEY_FILE="/run/secrets/ai/ai_api_key")
            for role in ("CHAT", "VISION"):
                env.update({f"AI_{role}_PROVIDER": "anthropic-messages",
                            f"AI_{role}_BASE_URL": env["AI_BASE_URL"],
                            f"AI_{role}_MODEL": "claude-haiku-4-5",
                            f"AI_{role}_API_KEY_FILE":
                                f"/run/secrets/ai/ai_{role.lower()}_api_key"})
            if mode.startswith("host"):
                env.update(profile)
                if mode == "host-linux":
                    env.update(AI_CHAT_BASE_URL="http://192.168.50.1:11434",
                               AI_VISION_BASE_URL="http://192.168.50.1:11434")
        elif mode == "gemini-both":
            env.update(AI_PROVIDER="gemini",
                       AI_BASE_URL="https://generativelanguage.googleapis.com/v1beta",
                       AI_MODEL="gemini-3.1-flash-lite",
                       AI_API_KEY_FILE="/run/secrets/ai/ai_api_key")
        elif mode != "default":
            remote = "VISION" if mode.endswith("-vision") else "CHAT"
            for role in ("CHAT", "VISION"):
                env[f"AI_{role}_PROVIDER"] = "ollama"
                env[f"AI_{role}_MODEL"] = "qwen3-vl:2b"
            gemini = mode.startswith("gemini-")
            env.update({
                f"AI_{remote}_PROVIDER": "gemini" if gemini else "openai-compatible",
                f"AI_{remote}_BASE_URL":
                    "https://generativelanguage.googleapis.com/v1beta" if gemini
                    else "https://ai.example.invalid/v1",
                f"AI_{remote}_MODEL":
                    "gemini-3.1-flash-lite" if gemini else "synthetic-remote-model",
                f"AI_{remote}_API_KEY_FILE":
                    f"/run/secrets/ai/ai_{remote.lower()}_api_key",
            })
        if mode == "host-lan":
            env["PROXY_LAN_IP"] = "192.168.65.23"
        Path(f"/fixtures/{mode}.env").write_text(
            "".join(f"{key}={value}\n" for key, value in env.items()))
    print("Prepared ten synthetic environments from public files only.")


if sys.argv[1] == "prepare":
    prepare()
    raise SystemExit(0)

from assistant.config import Config
from assistant.provider_config import resolve_provider_roles


MODE = sys.argv[1]
if MODE not in MODES:
    raise ValueError("Unknown provider fixture mode")
CONFIG = json.load(sys.stdin)
ENV = CONFIG["services"]["assistant-api"]["environment"]


def expected_roles():
    local = ("ollama", "http://ollama:11434", "qwen3-vl:2b", None)
    if MODE.startswith("host"):
        url = ("http://192.168.50.1:11434" if MODE == "host-linux"
               else "http://host.docker.internal:11434")
        host = ("ollama", url, "minicpm-v4.6", None)
        return {"CHAT": host, "VISION": host}
    if MODE == "haiku-both":
        return {role: ("anthropic-messages", "http://host.docker.internal:6655/anthropic",
                       "claude-haiku-4-5", KEYS[f"/run/secrets/ai/ai_{role.lower()}_api_key"])
                for role in ("CHAT", "VISION")}
    if MODE == "gemini-both":
        shared = ("gemini", "https://generativelanguage.googleapis.com/v1beta",
                  "gemini-3.1-flash-lite", KEYS["/run/secrets/ai/ai_api_key"])
        return {"CHAT": shared, "VISION": shared}
    result = {"CHAT": local, "VISION": local}
    if MODE != "default":
        role = "VISION" if MODE.endswith("-vision") else "CHAT"
        gemini = MODE.startswith("gemini-")
        result[role] = (
            "gemini" if gemini else "openai-compatible",
            "https://generativelanguage.googleapis.com/v1beta" if gemini
            else "https://ai.example.invalid/v1",
            "gemini-3.1-flash-lite" if gemini else "synthetic-remote-model",
            KEYS[f"/run/secrets/ai/ai_{role.lower()}_api_key"],
        )
    return result


class OllamaProfileContract(unittest.TestCase):
    def assert_roles(self, roles):
        for name, expected in expected_roles().items():
            role = getattr(roles, name.lower())
            self.assertEqual((role.kind, role.base_url, role.model, role.api_key), expected)

    def test_real_role_resolution_and_anonymous_key_selection(self):
        reader = Mock(side_effect=lambda path: KEYS[path])
        roles = resolve_provider_roles(
            ENV, ollama_url=ENV["OLLAMA_URL"], ollama_model=ENV["OLLAMA_MODEL"],
            key_reader=reader)
        self.assert_roles(roles)
        if MODE == "default" or MODE.startswith("host"):
            reader.assert_not_called()
            for name in ("AI_API_KEY_FILE", "AI_CHAT_API_KEY_FILE", "AI_VISION_API_KEY_FILE"):
                self.assertEqual(ENV[name], "")
        if MODE.startswith("host"):
            # The old shared gateway settings are intentionally still present.
            self.assertEqual(ENV["AI_PROVIDER"], "anthropic-messages")
            for role in ("CHAT", "VISION"):
                self.assertEqual(ENV[f"AI_{role}_PROVIDER"], "ollama")
                self.assertEqual(ENV[f"AI_{role}_BASE_URL"], expected_roles()[role][1])
                self.assertEqual(ENV[f"AI_{role}_MODEL"], "minicpm-v4.6")

    def test_real_config_load_and_budget_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(ENV)
            secret = Path(directory) / "synthetic-core-secret"
            secret.write_text("synthetic-core-secret-not-for-production-00000000")
            for name in ("ASSISTANT_SESSION_SECRET_FILE", "MCP_SHARED_SECRET_FILE"):
                env[name] = str(secret)
            for name in ("AI_API_KEY_FILE", "AI_CHAT_API_KEY_FILE", "AI_VISION_API_KEY_FILE"):
                if env[name]:
                    key = Path(directory) / name
                    key.write_text(KEYS[env[name]])
                    env[name] = str(key)
            with patch.dict(os.environ, env, clear=True):
                cfg = Config.load()
                self.assert_roles(cfg.ai_roles)
                self.assertEqual(cfg.chat_timeout, 120 if MODE.startswith("host") else 60)
                self.assertEqual((cfg.analysis_queue_timeout, cfg.analysis_timeout,
                                  cfg.capture_sync_timeout), (120, 900, 90))
                self.assertGreater(cfg.capture_ttl, cfg.analysis_queue_timeout + cfg.analysis_timeout)
                for invalid in ("0", "121"):
                    os.environ["ASSISTANT_CHAT_TURN_TIMEOUT_SECONDS"] = invalid
                    with self.assertRaisesRegex(RuntimeError, "chat_timeout"):
                        Config.load()

    def test_only_api_gets_optional_egress(self):
        users = {name for name, service in CONFIG["services"].items()
                 if "ai_egress" in service.get("networks", {})}
        self.assertEqual(users, set() if MODE == "default" else {"assistant-api"})
        for name, service in CONFIG["services"].items():
            self.assertFalse(service.get("extra_hosts"), name)
        if MODE != "default":
            self.assertFalse(CONFIG["networks"]["ai_egress"].get("internal", False))
        for name, service in CONFIG["services"].items():
            if name == "proxy":
                continue
            for network in service.get("networks", {}):
                if network != "ai_egress":
                    self.assertTrue(CONFIG["networks"][network].get("internal"), (name, network))

    def test_ports_and_selected_lan_are_preserved(self):
        publishers = {name for name, service in CONFIG["services"].items() if service.get("ports")}
        self.assertEqual(publishers, {"proxy"})
        ports = CONFIG["services"]["proxy"]["ports"]
        self.assertFalse(any(int(port["target"]) == 11434 for port in ports))
        self.assertEqual({port["host_ip"] for port in ports},
                         {"127.0.0.1", "192.168.65.23"} if MODE == "host-lan" else {"127.0.0.1"})
        self.assertEqual(ENV["HOMEBOX_PUBLIC_ORIGIN"],
                         "https://192.168.65.23:8443" if MODE == "host-lan"
                         else "https://homebox.home.arpa")

    def test_isolated_read_only_provider_directory(self):
        mounts = [(name, volume) for name, service in CONFIG["services"].items()
                  for volume in service.get("volumes", [])
                  if volume.get("target") == "/run/secrets/ai"]
        self.assertEqual(len(mounts), 0 if MODE == "default" else 1)
        for name, volume in mounts:
            self.assertEqual(name, "assistant-api")
            self.assertTrue(volume["read_only"])
            self.assertTrue(volume["source"].endswith("/secrets/ai"))
            self.assertFalse(volume.get("bind", {}).get("create_host_path", False))
        for service in CONFIG["services"].values():
            self.assertFalse({"AI_API_KEY", "AI_CHAT_API_KEY", "AI_VISION_API_KEY"}
                             & service.get("environment", {}).keys())

    def test_bundled_model_and_cache_are_unchanged(self):
        ollama = CONFIG["services"]["ollama"]
        self.assertEqual(set(ollama["networks"]), {"inference"})
        self.assertEqual(ENV["OLLAMA_URL"], "http://ollama:11434")
        self.assertEqual(ENV["OLLAMA_MODEL"], "qwen3-vl:2b")
        self.assertEqual(ollama["environment"]["OLLAMA_NUM_PARALLEL"], "1")
        self.assertEqual(ollama["environment"]["OLLAMA_MAX_LOADED_MODELS"], "1")
        self.assertEqual(ollama["environment"]["OLLAMA_CONTEXT_LENGTH"], "4096")
        self.assertTrue(any(volume["type"] == "volume" and volume["source"] == "ollama_data"
                            and volume["target"] == "/root/.ollama"
                            for volume in ollama["volumes"]))


unittest.main(argv=["check_ollama_config.py"], verbosity=2)
