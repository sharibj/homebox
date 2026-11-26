"""Exact wildcard HTTPS bindings and offline access-migration guards."""

import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lifecycle"))
import access
from test_access import candidate, configuration

IP = "100.90.231.7"


def wildcard(old):
    value = candidate(old, IP)
    value["services"]["proxy"]["ports"] = access.listener_ports(IP, "all-ipv4")
    if not any(p["target"] == 80 for p in old["services"]["proxy"]["ports"]):
        value["services"]["proxy"]["ports"] = [
            p for p in value["services"]["proxy"]["ports"] if p["target"] != 80]
    return value


def check_render(cfg, mode):
    proxy = cfg["services"]["proxy"]
    expected = {
        "private": access.listener_ports(None, "address"),
        "address": access.listener_ports(IP, "address"),
        "all-ipv4": access.listener_ports(IP, "all-ipv4"),
    }[mode]
    assert access.public_ports(proxy) == access.public_ports({"ports": expected})
    assert len(proxy["ports"]) == len(expected), "Duplicate/conflicting host bindings"
    assert all(not service.get("ports") for name, service in cfg["services"].items() if name != "proxy")
    if mode != "private":
        assert proxy["environment"]["PROXY_LAN_IP"] == IP
        assert proxy["environment"]["CADDY_DEFAULT_SNI"] == IP
        assert cfg["services"]["assistant-api"]["environment"]["HOMEBOX_PUBLIC_ORIGIN"] == "https://" + IP + ":8443"
    assert "0.0.0.0" not in json.dumps(proxy["environment"])
    assert "0.0.0.0" not in json.dumps(cfg["services"]["assistant-api"]["environment"])
    print("PASS: stock Compose " + mode + " exact proxy bindings; no other host ports or wildcard URLs")


class WildcardTests(unittest.TestCase):
    def test_exact_paired_mode_and_private_defaults(self):
        private = {"PROXY_BIND_IP": "127.0.0.1", "PROXY_LAN_IP": IP}
        self.assertEqual(access.listener_mode(private), "address")
        self.assertEqual(access.listener_mode({**private, "PROXY_HTTPS_BIND_IP": "0.0.0.0",
                                              "PROXY_LAN_BIND_IP": "0.0.0.0"}), "all-ipv4")
        for overrides in ({"PROXY_HTTPS_BIND_IP": "0.0.0.0"}, {"PROXY_LAN_BIND_IP": "0.0.0.0"},
                          {"PROXY_BIND_IP": "0.0.0.0"}, {"PROXY_HTTPS_BIND_IP": "::"},
                          {"PROXY_LAN_BIND_IP": "100.90.231.8"}):
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                access.listener_mode({**private, **overrides})

    def test_wildcard_never_becomes_advertised_or_certificate_address(self):
        for bad in ("0.0.0.0", "::", "127.0.0.1", "10.2.3.4", ""):
            with self.subTest(ip=bad), self.assertRaises(ValueError):
                access.listener_mode({"PROXY_LAN_IP": bad, "PROXY_HTTPS_BIND_IP": "0.0.0.0",
                                      "PROXY_LAN_BIND_IP": "0.0.0.0"})

    def test_loopback_to_wildcard_is_exact_delta(self):
        before = configuration()
        after = wildcard(before)
        access.validate_delta(before, after, IP, "access-example", {"assistant-api": "api"},
                              listener_mode="all-ipv4")
        self.assertEqual(access.public_ports(after["services"]["proxy"]), {
            "443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}],
            "8443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8443"}]})

    def test_existing_tailnet_to_wildcard_does_not_duplicate_caddy_or_ports(self):
        before = candidate(configuration(), IP)
        after = copy.deepcopy(before)
        after["services"]["proxy"]["ports"] = [
            p for p in access.listener_ports(IP, "all-ipv4") if p["target"] != 80]
        access.validate_delta(before, after, IP, "access-example", {"assistant-api": "api"},
                              listener_mode="all-ipv4")
        self.assertEqual(len(after["services"]["proxy"]["volumes"]), 2)

    def test_wildcard_to_addressed_is_exact_delta(self):
        before = wildcard(configuration())
        after = candidate(configuration(), IP)
        access.validate_delta(before, after, IP, "access-example", {"assistant-api": "api"})

    def test_wildcard_is_not_allowed_without_explicit_mode(self):
        with self.assertRaisesRegex(ValueError, "beyond"):
            access.validate_delta(configuration(), wildcard(configuration()), IP,
                                  "access-example", {"assistant-api": "api"})

    def test_no_duplicate_or_extra_host_binding_and_no_other_configuration_change(self):
        changes = (
            lambda c: c["services"]["proxy"]["ports"].append(
                {"mode": "ingress", "host_ip": IP, "target": 443, "published": "443", "protocol": "tcp"}),
            lambda c: c["services"]["proxy"]["ports"].append(
                {"mode": "ingress", "host_ip": "127.0.0.1", "target": 443, "published": "443", "protocol": "tcp"}),
            lambda c: c["services"]["proxy"]["ports"].append(
                {"mode": "ingress", "host_ip": "0.0.0.0", "target": 80, "published": "80", "protocol": "tcp"}),
            lambda c: c["services"]["assistant-api"].update(
                ports=[{"host_ip": "0.0.0.0", "target": 8000, "published": "8000"}]),
            lambda c: c["services"]["assistant-api"]["environment"].update(AI_CHAT_BASE_URL="http://other:11434"),
            lambda c: c["services"]["proxy"]["environment"].update(CADDY_DEFAULT_SNI="0.0.0.0"),
            lambda c: c["services"]["proxy"].update(privileged=True),
            lambda c: c["networks"]["private"].update(internal=False),
        )
        for change in changes:
            after = wildcard(configuration())
            change(after)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, "beyond"):
                access.validate_delta(configuration(), after, IP, "access-example",
                                      {"assistant-api": "api"}, listener_mode="all-ipv4")

    def test_plan_unknown_mode_refuses_and_old_private_plan_remains_readable(self):
        m = Mock()
        m.journal.return_value = {"access_change": {"sha256": "correct"}}
        m.file_digest.return_value = "correct"
        with patch.object(access, "directory", return_value=Path("/nonexistent-private-plan")):
            m.load.return_value = {"listener_mode": "future-unknown"}
            with self.assertRaisesRegex(ValueError, "Unknown access listener"):
                access.plan(m)
            m.load.return_value = {"ip": IP}
            self.assertEqual(access.plan(m), {"ip": IP})

    def test_pending_old_source_is_not_adopted_under_new_code(self):
        m = Mock()
        m.selected.return_value = {"root": "same"}
        with patch.object(access, "base_context"), patch.object(access, "pending", return_value=True), \
                patch.object(access, "plan", return_value={"binding": {"root": "same"}, "sources": {"old": "hash"}}), \
                patch.object(access, "sources", return_value={"new": "hash"}), \
                self.assertRaisesRegex(ValueError, "source/binding changed"):
            access.guard(m, require_address=False)
        m.save.assert_not_called()

    def test_rollback_requires_exact_saved_configuration(self):
        m = Mock()
        m.STATE = Path("/private")
        before = candidate(configuration(), IP)
        m.journal.return_value = {"access_change": {"direction": "rollback"}}
        m.config.return_value = copy.deepcopy(before)
        with patch.object(access, "guard", return_value={"config": before}), \
                patch.object(access, "directory", return_value=Path("/private")), \
                patch.object(access, "save") as save:
            access.candidate(m)
            self.assertTrue(save.called)
        m.config.return_value = wildcard(configuration())
        with patch.object(access, "guard", return_value={"config": before}), \
                self.assertRaisesRegex(ValueError, "exact original"):
            access.candidate(m)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--check-render"]:
        check_render(json.load(sys.stdin), sys.argv[2])
    else:
        unittest.main()
