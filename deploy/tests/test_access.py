"""Offline negative guards for the explicit configuration-only transition."""
import copy
import importlib.util
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1] / "lifecycle"
if not ROOT.exists():
    ROOT = Path("/source/deploy/lifecycle")
sys.path.insert(0, str(ROOT))
import access
import metadata


def configuration():
    return {"name": "example", "services": {
        "proxy": {"image": "caddy@sha256:pinned", "environment": {
            "CADDY_DEFAULT_SNI": "inventory.localhost", "TLS_ISSUER": "internal"},
            "volumes": [{"type": "bind", "source": "/repo/deploy/reverse-proxy/Caddyfile",
                         "target": "/etc/caddy/Caddyfile", "read_only": True,
                         "bind": {"create_host_path": True}}],
            "ports": [{"mode": "ingress", "host_ip": "127.0.0.1", "target": 443,
                       "published": "443", "protocol": "tcp"}]},
        "assistant-api": {"image": "api:old", "environment": {
            "HOMEBOX_PUBLIC_ORIGIN": "https://homebox.localhost",
            "AI_CHAT_BASE_URL": "http://ollama:11434"}},
    }, "networks": {"private": {"internal": True}}, "volumes": {"data": {}}}


def candidate(old, ip="100.64.0.1"):
    cfg = copy.deepcopy(old)
    proxy = cfg["services"]["proxy"]
    proxy["environment"].update(PROXY_LAN_IP=ip, CADDY_DEFAULT_SNI=ip)
    mount = proxy["volumes"][0]
    mount["source"] += ".lan"
    proxy["volumes"].append({
        "type": "bind", "source": "/repo/deploy/reverse-proxy/Caddyfile",
        "target": "/etc/caddy/Caddyfile.base", "read_only": True,
        "bind": {"create_host_path": True}})
    proxy["ports"] += [{"mode": "ingress", "host_ip": ip, "target": port,
                       "published": str(port), "protocol": "tcp"} for port in (443, 8443)]
    cfg["services"]["assistant-api"]["environment"]["HOMEBOX_PUBLIC_ORIGIN"] = "https://" + ip + ":8443"
    cfg["services"]["assistant-api"]["image"] = "api:access-example"
    return cfg


class DeltaTests(unittest.TestCase):
    def test_PWA_without_compose_environment_preserves_complete_image_environment(self):
        old = {"Config": {"Env": ["PATH=/usr/bin", "NGINX_VERSION=pinned"]},
               "HostConfig": {"Memory": 123}, "Mounts": []}
        cfg = {"services": {"pwa": {"image": "pwa:existing"}}}
        self.assertTrue(access.runtime_matches(Mock(), "pwa", old, old, cfg))
        changed = copy.deepcopy(old)
        changed["Config"]["Env"].append("UNAPPROVED=1")
        self.assertFalse(access.runtime_matches(Mock(), "pwa", changed, old, cfg))

    def test_only_exact_planned_delta(self):
        for ip in ("100.64.0.0", "100.99.101.89", "100.127.255.255"):
            with self.subTest(ip=ip):
                access.validate_delta(configuration(), candidate(configuration(), ip),
                                      ip, "access-example", {"assistant-api": "api"})

    def test_unrelated_changes_refused(self):
        mutations = (
            lambda c: c["services"]["assistant-api"]["environment"].update(AI_CHAT_BASE_URL="https://other.invalid"),
            lambda c: c["services"]["proxy"].update(image="other"),
            lambda c: c["networks"]["private"].update(internal=False),
            lambda c: c["volumes"].update(other={}),
            lambda c: c["services"]["proxy"]["ports"][0].update(host_ip="0.0.0.0"),
            lambda c: c["services"]["proxy"]["volumes"][0].update(read_only=False),
            lambda c: c["services"]["proxy"].update(privileged=True),
        )
        for mutation in mutations:
            cfg = candidate(configuration())
            mutation(cfg)
            with self.subTest(mutation=mutation), self.assertRaisesRegex(ValueError, "beyond"):
                access.validate_delta(configuration(), cfg, "100.64.0.1",
                                      "access-example", {"assistant-api": "api"})

    def test_address_must_be_tailnet_not_just_private(self):
        for ip in ("0.0.0.0", "10.1.2.3", "100.63.255.255", "100.128.0.0", "::1"):
            with self.subTest(ip=ip), self.assertRaises(ValueError):
                access.address(ip)

    def test_duplicate_mounts_never_collapsed(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            access.mounts({"Mounts": [{"Destination": "/data"}, {"Destination": "/data"}]})

    def test_full_mount_and_host_fields_preserved(self):
        cfg = configuration()
        old = {"Config": {"Env": ["CADDY_DEFAULT_SNI=inventory.localhost", "TLS_ISSUER=internal"]},
               "HostConfig": {"Memory": 123, "PortBindings": access.public_ports(cfg["services"]["proxy"]),
                              "Binds": ["/repo/deploy/reverse-proxy/Caddyfile:/etc/caddy/Caddyfile:ro"]},
               "Mounts": [{"Type": "bind", "Source": "/repo/deploy/reverse-proxy/Caddyfile",
                           "Destination": "/etc/caddy/Caddyfile", "Mode": "ro", "RW": False,
                           "Propagation": "rprivate"}]}
        m = Mock()
        m.bind_source_matches.side_effect = lambda a, b: a == b
        self.assertTrue(access.runtime_matches(m, "proxy", old, old, cfg))
        changed = copy.deepcopy(old)
        changed["Mounts"][0]["Propagation"] = "shared"
        self.assertFalse(access.runtime_matches(m, "proxy", changed, old, cfg))
        changed = copy.deepcopy(old)
        changed["HostConfig"]["Memory"] = 1
        self.assertFalse(access.runtime_matches(m, "proxy", changed, old, cfg))
        desktop = copy.deepcopy(old)
        desktop["Mounts"][0]["Source"] = "/host_mnt" + desktop["Mounts"][0]["Source"]
        desktop["HostConfig"]["Binds"] = ["/host_mnt" + old["HostConfig"]["Binds"][0]]
        m.bind_source_matches.side_effect = lambda a, b: a == b or a == "/host_mnt" + b
        self.assertTrue(access.runtime_matches(m, "proxy", desktop, desktop, cfg))
        desktop["HostConfig"]["Binds"][0] += ",shared"
        self.assertFalse(access.runtime_matches(m, "proxy", desktop, old, cfg))


class EntryGuards(unittest.TestCase):
    def test_rollback_entry_skips_only_lost_address_requirement(self):
        m = Mock()
        with patch.dict(os.environ, {"LC_ACCESS_ACTION": "rollback"}), \
                patch.object(access, "guard") as guard, patch.object(access, "phase") as phase, \
                patch.object(access, "instructions") as instructions:
            access.dispatch("access-select", m)
        guard.assert_called_once_with(m, require_address=False)
        phase.assert_called_once_with(m, "recovering", "rollback")
        instructions.assert_called_once_with(m)

    def test_recovery_never_bypasses_other_guards_to_change_direction(self):
        m = Mock()
        with patch.dict(os.environ, {"LC_ACCESS_ACTION": "rollback"}), \
                patch.object(access, "guard", side_effect=ValueError("resource drift")), \
                patch.object(access, "phase") as phase, self.assertRaisesRegex(ValueError, "resource drift"):
            access.dispatch("access-select", m)
        phase.assert_not_called()

    def test_proxy_authentication_refuses_wrong_canonical_binding(self):
        m = Mock()
        m.journal.return_value = {"binding": {"root": "other"}, "access_change": {"status": "pending"}}
        m.selected.return_value = {"root": "original"}
        with patch.object(access, "plan", return_value={"binding": {"root": "original"}}), \
                self.assertRaisesRegex(ValueError, "canonical"):
            access.trusted_proxy(m)
        m.containers.assert_not_called()

    def test_witnessed_proxy_can_close_before_owner_source_resource_address_checks(self):
        m = Mock()
        binding = {"root": "original"}
        m.journal.return_value = {"binding": binding, "access_change": {"status": "pending"}}
        m.selected.return_value = binding
        m.containers.return_value = {"proxy": {"Id": "witnessed"}}
        spec = {"binding": binding, "owner": {"binding": binding, "established": True},
                "before": {"proxy": {"Id": "witnessed"}}}
        with patch.object(access, "plan", return_value=spec):
            self.assertEqual(access.trusted_proxy(m), "witnessed")
        m.installation_owner.assert_not_called()
        m.volume_snapshot.assert_not_called()

    def test_incomplete_ready_journal_refuses_before_any_mutation(self):
        m = Mock()
        m.selected.return_value = {"root": "same"}
        m.installation_owner.return_value = {"established": True, "binding": {"root": "same"}}
        m.journal.return_value = {"binding": {"root": "same"}, "resources": {"receipt": True},
                                  "phase": "cutover"}
        with self.assertRaisesRegex(ValueError, "Finish the original deployment"):
            access.prepare(m)
        m.check_resources.assert_not_called()
        m.save.assert_not_called()

    def test_missing_established_owner_not_adopted(self):
        m = Mock()
        m.installation_owner.return_value = {"established": False}
        with self.assertRaisesRegex(ValueError, "established"):
            access.base_context(m)
        m.save.assert_not_called()

    def test_normal_commands_blocked_during_pending_change(self):
        for command in ("fresh", "redeploy", "backup", "adopt", "status", "users", "models"):
            with patch.dict(os.environ, {"LC_COMMAND": command}), \
                    patch.object(metadata, "journal", return_value={"access_change": {"status": "pending"}}), \
                    self.subTest(command=command), self.assertRaisesRegex(ValueError, "access resume"):
                metadata.select()

    def test_exact_pending_plan_file_digest_required(self):
        m = Mock()
        m.journal.return_value = {"access_change": {"id": "a" * 32, "sha256": "correct"}}
        m.STATE = Path("/tmp")
        m.file_digest.return_value = "wrong"
        with self.assertRaisesRegex(ValueError, "plan identity"):
            access.plan(m)

    def test_missing_TLS_proof_cannot_commit_private_environment(self):
        m = Mock()
        m.journal.return_value = {"access_change": {"phase": "verified", "direction": "forward"}}
        m.load.return_value = {}
        with patch.object(access, "guard", return_value={}), \
                patch.object(access, "directory", return_value=Path("/private")), \
                self.assertRaisesRegex(ValueError, "HTTPS"):
            access.finish(m)
        m.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
