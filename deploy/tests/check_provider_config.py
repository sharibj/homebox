"""Validate rendered external-role configuration without making network calls."""

import json
import sys
import unittest


MODE = sys.argv[1]
CONFIG = json.load(sys.stdin)
if MODE not in {"external-chat", "external-vision", "gemini-chat", "gemini-vision", "gemini-both"}:
    raise ValueError("Unknown external provider fixture mode")


class ExternalProviderContract(unittest.TestCase):
    def test_only_api_gets_egress(self):
        users = {name for name, service in CONFIG["services"].items()
                 if "ai_egress" in service.get("networks", {})}
        self.assertEqual(users, {"assistant-api"})
        self.assertFalse(CONFIG["networks"]["ai_egress"].get("internal", False))
        self.assertEqual({name for name, service in CONFIG["services"].items()
                          if service.get("ports")}, {"proxy"})

    def test_override_preserves_explicit_roles(self):
        env = CONFIG["services"]["assistant-api"]["environment"]
        if MODE == "gemini-both":
            self.assertEqual(env["AI_PROVIDER"], "gemini")
            self.assertEqual(env["AI_BASE_URL"], "https://generativelanguage.googleapis.com/v1beta")
            self.assertEqual(env["AI_MODEL"], "gemini-3.1-flash-lite")
            for role in ("CHAT", "VISION"):
                for field in ("PROVIDER", "BASE_URL", "MODEL", "API_KEY_FILE"):
                    self.assertEqual(env[f"AI_{role}_{field}"], "")
        else:
            self.assertEqual(env["AI_PROVIDER"], "ollama")
            self.assertEqual(env["AI_BASE_URL"], "")
            self.assertEqual(env["AI_MODEL"], "")
            remote = "VISION" if MODE.endswith("-vision") else "CHAT"
            local = "CHAT" if remote == "VISION" else "VISION"
            gemini = MODE.startswith("gemini-")
            self.assertEqual(env[f"AI_{remote}_PROVIDER"], "gemini" if gemini else "openai-compatible")
            self.assertEqual(env[f"AI_{remote}_BASE_URL"],
                             "https://generativelanguage.googleapis.com/v1beta" if gemini
                             else "https://ai.example.invalid/v1")
            self.assertEqual(env[f"AI_{remote}_MODEL"],
                             "gemini-3.1-flash-lite" if gemini else "synthetic-remote-model")
            if gemini:
                self.assertEqual(env[f"AI_{remote}_API_KEY_FILE"],
                                 "/run/secrets/ai/ai_" + remote.lower() + "_api_key")
            self.assertEqual(env[f"AI_{local}_PROVIDER"], "ollama")
            self.assertEqual(env[f"AI_{local}_MODEL"], "qwen3-vl:2b")
        for service in CONFIG["services"].values():
            self.assertFalse({"AI_API_KEY", "AI_CHAT_API_KEY", "AI_VISION_API_KEY"}
                             & service.get("environment", {}).keys())

    def test_only_isolated_provider_directory_is_mounted(self):
        mounts = [(name, volume) for name, service in CONFIG["services"].items()
                  for volume in service.get("volumes", [])
                  if volume.get("target") == "/run/secrets/ai"]
        self.assertEqual(len(mounts), 1)
        name, volume = mounts[0]
        self.assertEqual(name, "assistant-api")
        self.assertTrue(volume["read_only"])
        self.assertTrue(volume["source"].endswith("/secrets/ai"))
        self.assertFalse(volume.get("bind", {}).get("create_host_path", False))
        self.assertNotIn("ai_api_key", CONFIG["secrets"])
        self.assertEqual(CONFIG["services"]["assistant-api"]["environment"]["AI_API_KEY_FILE"],
                         "/run/secrets/ai/ai_api_key" if MODE == "gemini-both" else "")


unittest.main(argv=["check_provider_config.py"])
