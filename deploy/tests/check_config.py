"""Check the rendered Compose contract without importing third-party modules."""

import json
import sys
import unittest
import uuid


CONFIG = json.load(sys.stdin)


class DeploymentContract(unittest.TestCase):
    def test_only_proxy_publishes_ports(self):
        published = {name: svc["ports"] for name, svc in CONFIG["services"].items() if svc.get("ports")}
        self.assertEqual(set(published), {"proxy"})
        self.assertEqual({int(port["target"]) for port in published["proxy"]}, {80, 443})
        self.assertTrue(all(port["host_ip"] == "127.0.0.1" for port in published["proxy"]))

    def test_network_boundaries(self):
        for name, service in CONFIG["services"].items():
            if name == "proxy":
                continue
            for network in service.get("networks", {}):
                self.assertTrue(CONFIG["networks"][network].get("internal"), (name, network))
        self.assertNotIn("inference", CONFIG["services"]["proxy"]["networks"])
        self.assertNotIn("tools", CONFIG["services"]["proxy"]["networks"])
        self.assertEqual(set(CONFIG["services"]["ollama"]["networks"]), {"inference"})

    def test_account_and_secret_boundary(self):
        api = CONFIG["services"]["assistant-api"]["environment"]
        mcp = CONFIG["services"]["homebox-mcp"]["environment"]
        hb = CONFIG["services"]["homebox"]["environment"]
        self.assertEqual(api["ASSISTANT_ALLOW_INSECURE_HTTP"], "false")
        self.assertNotEqual(uuid.UUID(api["HOMEBOX_INSTANCE_ID"]).int, 0)
        self.assertNotEqual(api["ASSISTANT_PUBLIC_ORIGIN"], api["HOMEBOX_PUBLIC_ORIGIN"])
        self.assertEqual(api["MCP_SHARED_SECRET_FILE"], mcp["MCP_SHARED_SECRET_FILE"])
        self.assertTrue(hb["HBOX_AUTH_API_KEY_PEPPER_FILE"].startswith("/run/secrets/"))
        for service in CONFIG["services"].values():
            env = service.get("environment", {})
            self.assertFalse({"HOMEBOX_TOKEN", "HOMEBOX_API_KEY", "HBOX_AUTH_API_KEY_PEPPER"} & env.keys())

    def test_durable_state_and_optional_model(self):
        services = CONFIG["services"]
        self.assertNotIn("ollama", services["assistant-api"].get("depends_on", {}))
        for name in ("homebox", "assistant-api", "ollama"):
            self.assertTrue(any(v["type"] == "volume" for v in services[name]["volumes"]), name)
        self.assertEqual(services["ollama"]["environment"]["OLLAMA_NUM_PARALLEL"], "1")
        self.assertEqual(services["ollama"]["environment"]["OLLAMA_MAX_LOADED_MODELS"], "1")

    def test_resource_and_image_limits(self):
        total = sum(int(service["mem_limit"]) for service in CONFIG["services"].values())
        self.assertLessEqual(total, 12 * 1024**3)
        for name, service in CONFIG["services"].items():
            self.assertIn("healthcheck", service, name)
            self.assertGreater(service["pids_limit"], 0, name)
            self.assertTrue(service["read_only"], name)
            image = service["image"]
            self.assertNotIn(":latest", image)
            if "build" not in service:
                self.assertIn("@sha256:", image)


unittest.main(argv=["check_config.py"])
