"""Offline transaction regressions, with synthetic addresses and no Docker socket."""
import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lifecycle"))
import access
import local_lan
import metadata
from test_local_lan import device_row, physical

DEPLOY = Path(__file__).resolve().parents[1]
TAIL = "100.90.231.7"
LAN = "10.23.45.67"
OLD_LAN = "10.23.45.66"


def compose(local="", wildcard=False, hostname="homebox.localhost"):
    cfg = {"services": {name: {"image": name + ":original", "environment": {}}
                        for name in metadata.SERVICES}}
    cfg["services"]["homebox"]["environment"] = {"HBOX_OPTIONS_HOSTNAME": hostname}
    proxy = cfg["services"]["proxy"]
    proxy["environment"] = {"PROXY_LAN_IP": TAIL, "CADDY_DEFAULT_SNI": TAIL,
                            "TLS_ISSUER": "internal"}
    proxy["volumes"] = [
        {"type": "bind", "source": "/repo/deploy/reverse-proxy/Caddyfile.lan",
         "target": "/etc/caddy/Caddyfile", "read_only": True},
        {"type": "bind", "source": "/repo/deploy/reverse-proxy/Caddyfile",
         "target": "/etc/caddy/Caddyfile.base", "read_only": True}]
    proxy["ports"] = access.listener_ports(TAIL, "all-ipv4" if wildcard else "address", local)
    cfg["services"]["assistant-api"]["environment"] = {
        "HOMEBOX_PUBLIC_ORIGIN": "https://" + TAIL + ":8443"}
    if local:
        proxy["environment"]["PROXY_LOCAL_LAN_IP"] = local
        proxy["volumes"][0]["source"] = "/repo/deploy/reverse-proxy/Caddyfile.lan-local"
        proxy["volumes"].append({
            "type": "bind", "source": "/repo/deploy/reverse-proxy/run-dual.sh",
            "target": "/etc/caddy/run-dual.sh", "read_only": True})
        proxy["command"] = ["/bin/sh", "/etc/caddy/run-dual.sh"]
        cfg["services"]["assistant-api"]["environment"]["ASSISTANT_LAN_ORIGIN"] = "https://" + local
    return cfg


def forward(before, local):
    """Independent expected candidate, including the published/target port split."""
    cfg = copy.deepcopy(before)
    for name, prefix in metadata.PREFIXES.items():
        cfg["services"][name]["image"] = prefix + ":access-test"
    proxy = cfg["services"]["proxy"]
    proxy["environment"]["PROXY_LOCAL_LAN_IP"] = local
    proxy["volumes"][0]["source"] = "/repo/deploy/reverse-proxy/Caddyfile.lan-local"
    if not any(v["target"] == "/etc/caddy/run-dual.sh" for v in proxy["volumes"]):
        proxy["volumes"].append({
            "type": "bind", "source": "/repo/deploy/reverse-proxy/run-dual.sh",
            "target": "/etc/caddy/run-dual.sh", "read_only": True})
    proxy["command"] = ["/bin/sh", "/etc/caddy/run-dual.sh"]
    proxy["ports"] = [
        {"mode": "ingress", "host_ip": ip, "target": target,
         "published": str(published), "protocol": "tcp"}
        for ip, published, target in (
            ("127.0.0.1", 80, 80), ("127.0.0.1", 443, 443),
            (TAIL, 443, 443), (TAIL, 8443, 8443),
            (local, 443, 14443), (local, 8443, 18443))]
    cfg["services"]["homebox"]["environment"]["HBOX_OPTIONS_HOSTNAME"] = ""
    cfg["services"]["assistant-api"]["environment"]["ASSISTANT_LAN_ORIGIN"] = "https://" + local
    return cfg


class TransactionFixture:
    def __init__(self, root, local="", wildcard=False, hostname=""):
        self.root = root
        self.cfg = compose(local, wildcard, hostname)
        self.m = Mock()
        m = self.m
        m.STATE, m.DEPLOY, m.WORK, m.SOURCE = (
            root / "state", root / "deployment", root / "state/work", DEPLOY.parent)
        for path in (m.STATE, m.DEPLOY, m.WORK, m.DEPLOY / "secrets"):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
        m.SERVICES, m.APPS, m.PREFIXES = metadata.SERVICES, metadata.APPS, metadata.PREFIXES
        m.save.side_effect, m.load.side_effect = metadata.save, metadata.load
        m.digest.side_effect, m.file_digest.side_effect = metadata.digest, metadata.file_digest
        m.environment.side_effect, m.replace_setting.side_effect = metadata.environment, metadata.replace_setting
        m.read_env.side_effect = lambda path=None: (path or m.DEPLOY / ".env").read_bytes().decode()
        m.journal.side_effect = lambda: metadata.load(m.STATE / "installation.json")
        m.selected.return_value = {"root": "/repo", "project": "synthetic", "state": str(m.STATE), "daemon": "test"}
        owner = {"established": True, "binding": m.selected()}
        m.installation_owner.return_value = owner
        body = ("# preserve protected bytes\r\nHOMEBOX_INSTANCE_ID=synthetic-instance\r\n"
                "HOMEBOX_HOST=homebox.localhost\r\nSTACK_VERSION=original\r\nPROXY_LAN_IP=" + TAIL + "\r\n")
        if local:
            body += "PROXY_LOCAL_LAN_IP=" + local + "\r\n"
        if wildcard:
            body += "PROXY_HTTPS_BIND_IP=0.0.0.0\r\nPROXY_LAN_BIND_IP=0.0.0.0\r\n"
        (m.DEPLOY / ".env").write_bytes(body.encode())
        (m.DEPLOY / ".env").chmod(0o600)
        (m.WORK / "tailscale-ip.txt").write_text(TAIL + "\n")
        self.inventory()
        metadata.save(m.WORK / "access-ca.json", {
            "root.crt": {"sha256": "synthetic-ca"}, "root.key": {"sha256": "synthetic-private-key-hash"}})
        resources = {"volumes": {"data": "unchanged-volume"}, "secrets": {}, "instance_id": "synthetic-instance"}
        m.volume_snapshot.return_value = resources["volumes"]
        m.secret_fingerprints.return_value = {}
        m.resource_snapshot.return_value = resources
        m.config.side_effect = lambda: self.cfg
        self.runtime = {}
        for name, cfg in self.cfg["services"].items():
            mounts = [{"Type": "volume", "Name": name + "-data", "Source": "/volumes/" + name,
                       "Destination": "/data", "Mode": "rw", "RW": True, "Propagation": ""}]
            binds = []
            for value in cfg.get("volumes", []):
                mounts.append({"Type": "bind", "Source": value["source"], "Destination": value["target"],
                               "Mode": "ro", "RW": False, "Propagation": "rprivate"})
                binds.append(value["source"] + ":" + value["target"] + ":ro")
            self.runtime[name] = {
                "Id": name + "-original", "Image": "sha256:" + name,
                "Config": {"Env": ["PATH=/usr/bin"] + [k + "=" + v for k, v in cfg["environment"].items()]},
                "HostConfig": {"Binds": binds, "PortBindings": access.public_ports(cfg)},
                "Mounts": mounts, "NetworkSettings": {"Networks": {"private": {"NetworkID": "network-original"}}},
                "State": {"Running": True, "Health": {"Status": "healthy"}, "StartedAt": "original"}}
        m.containers.side_effect = lambda: self.runtime
        m.bind_source_matches.side_effect = lambda actual, expected: actual == expected
        metadata.save(m.STATE / "installation.json", {
            "binding": m.selected(), "resources": resources, "phase": "ready",
            "overlays": [False, True, ""], "bootstrap_pending": True})

    def inventory(self, assigned=LAN, down=False):
        links, addresses = physical(ip=assigned) if assigned else ([], [])
        if down:
            links[0]["operstate"] = "DOWN"
        metadata.save(self.m.WORK / "local-lan-links.json", links)
        metadata.save(self.m.WORK / "local-lan-addresses.json", addresses)
        (self.m.WORK / "local-lan-devices.tsv").write_text(device_row())
        (self.m.WORK / "local-lan-devices.tsv").chmod(0o600)

    def backup(self):
        path = self.m.DEPLOY / "backups/access-native-synthetic.tar.gz"
        path.parent.mkdir(mode=0o700)
        path.write_bytes(b"synthetic offline archive identity")
        path.chmod(0o600)
        (self.m.WORK / "access-backup-name").write_text(path.name)
        access.dispatch("access-backup-record", self.m)
        return path

    def prepare(self):
        with patch.dict(os.environ, {"LC_ACCESS_ACTION": "lan", "LC_ACCESS_IP": LAN,
                                     "LC_ACCESS_LISTEN_ALL": "0"}, clear=True):
            access.prepare(self.m)
        return access.plan(self.m)

    def update_change(self, **values):
        state = self.m.journal()
        state["access_change"].update(values)
        metadata.save(self.m.STATE / "installation.json", state)

    def rescue_log(self):
        self.update_change(direction="rollback", phase="configuring-proxy", restore_attempt="b" * 32)
        (self.m.WORK / "access-exact-restore.log").write_text(
            "access-restore " + "b" * 32 + "\nlisten " + OLD_LAN + ":443: cannot assign requested address\n")

    def proofs(self, direction):
        m = self.m
        cfg = copy.deepcopy(self.cfg)
        metadata.save(access.directory(m) / "target-config.json", cfg)
        self.update_change(phase="verified", direction=direction, attempt="c" * 32)
        sha = metadata.file_digest(access.directory(m) / "target-config.json")
        metadata.save(access.directory(m) / "https-proof.json", {
            "status": "PASS", "tls_verified": True, "access_config_sha256": sha,
            "access_attempt": "c" * 32, "service_IDs": {k: v["Id"] for k, v in self.runtime.items()}})
        for ip, local, name in access.origin_targets(cfg):
            metadata.save(access.directory(m) / ("origin-" + name + ".json"), {
                "status": "passed", "IP_SAN": ip, "local_listener": local,
                "access_attempt": "c" * 32, "access_config_sha256": sha,
                "publicCA_SHA256": "synthetic-ca", "invalidNonGETOriginCasesRejected": 49})


class DualLanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def fixture(self, **kwargs):
        return TransactionFixture(self.root / str(len(list(self.root.iterdir()))), **kwargs)

    def test_strict_LAN_address_spelling_and_ranges(self):
        for value in (LAN, "172.16.12.34", "192.168.123.4"):
            self.assertEqual(local_lan.address(value), value)
        for value in ("10.023.45.67", LAN + "/24", LAN + ":443", " " + LAN,
                      LAN + "\n", TAIL, "127.0.0.1", "0.0.0.0", "::1", "example.invalid"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                local_lan.address(value)

    def test_cli_rejects_unsupported_LAN_flags_before_engine_or_state(self):
        state = self.root / "never-created"
        for args in ([], ["--ip"], ["--listen-all"], ["--ip", LAN, "--listen-all"],
                     ["--ip", LAN, "--unexpected"], ["--ip", LAN, "--ip", OLD_LAN]):
            result = subprocess.run(["/bin/sh", str(DEPLOY / "homebox"), "--state-dir", str(state),
                                     "access", "lan", *args], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertIn("Usage:", result.stderr)
            self.assertFalse(state.exists())

    def test_precise_public_ports_use_distinct_LAN_container_listeners(self):
        rows = access.listener_ports(TAIL, "address", LAN)
        self.assertEqual({(p["host_ip"], p["published"], p["target"]) for p in rows}, {
            ("127.0.0.1", "80", 80), ("127.0.0.1", "443", 443),
            (TAIL, "443", 443), (TAIL, "8443", 8443), (LAN, "443", 14443), (LAN, "8443", 18443)})
        self.assertEqual(len(rows), 6)

    def test_null_compose_runtime_fields_inherit_but_explicit_empty_does_not(self):
        f = self.fixture()
        old = copy.deepcopy(f.runtime["assistant-api"])
        fields = (("Cmd", "command", ["uvicorn", "assistant.main:app"], []),
                  ("Entrypoint", "entrypoint", ["/image-entrypoint"], []),
                  ("User", "user", "10001", ""),
                  ("WorkingDir", "working_dir", "/app", ""))
        old["Config"].update({key: default for key, _, default, _ in fields})
        # Omission, all four nulls, and each null independently inherit the
        # captured defaults of the same immutable image.
        selections = [{}] + [{setting: None for _, setting, _, _ in fields}]
        selections += [{setting: None} for _, setting, _, _ in fields]
        for selected in selections:
            cfg = copy.deepcopy(f.cfg)
            cfg["services"]["assistant-api"].update(selected)
            with self.subTest(inherited=selected):
                self.assertTrue(access.runtime_matches(f.m, "assistant-api", old, old, cfg))
        # [] and "" are deliberate overrides, not spelling variants of null.
        for key, setting, _, empty in fields:
            cfg = copy.deepcopy(f.cfg)
            cfg["services"]["assistant-api"][setting] = empty
            cleared = copy.deepcopy(old)
            cleared["Config"][key] = empty
            with self.subTest(explicit_clear=setting):
                self.assertFalse(access.runtime_matches(f.m, "assistant-api", old, old, cfg))
                self.assertTrue(access.runtime_matches(f.m, "assistant-api", cleared, old, cfg))

    def test_existing_nondual_wildcard_mode_is_unchanged(self):
        env = {"PROXY_LAN_IP": TAIL, "PROXY_HTTPS_BIND_IP": "0.0.0.0", "PROXY_LAN_BIND_IP": "0.0.0.0"}
        self.assertEqual(access.listener_mode(env), "all-ipv4")
        self.assertEqual([(p["host_ip"], p["target"]) for p in access.listener_ports(TAIL, "all-ipv4")],
                         [("127.0.0.1", 80), ("0.0.0.0", 443), ("0.0.0.0", 8443)])

    def test_LAN_activation_clears_old_wildcard_and_preserves_bootstrap_pending(self):
        f = self.fixture(wildcard=True)
        spec = f.prepare()
        after = metadata.environment((access.directory(f.m) / "after.env").read_text())
        self.assertEqual((after["PROXY_HTTPS_BIND_IP"], after["PROXY_LAN_BIND_IP"]), ("", ""))
        self.assertEqual((spec["before_listener_mode"], spec["listener_mode"]), ("all-ipv4", "address"))
        self.assertTrue(f.m.journal()["bootstrap_pending"])

    def test_dual_wildcard_rejected_before_plan_or_protected_mutations(self):
        f = self.fixture()
        before = (f.m.DEPLOY / ".env").read_bytes()
        with patch.dict(os.environ, {"LC_ACCESS_ACTION": "lan", "LC_ACCESS_IP": LAN,
                                     "LC_ACCESS_LISTEN_ALL": "1"}, clear=True), self.assertRaisesRegex(ValueError, "precise"):
            access.prepare(f.m)
        self.assertFalse((f.m.STATE / "access").exists())
        self.assertEqual((f.m.DEPLOY / ".env").read_bytes(), before)
        self.assertFalse((f.m.DEPLOY / "backups").exists())
        f.m.save.assert_not_called()
        with self.assertRaises(ValueError):
            access.listener_ports(TAIL, "all-ipv4", LAN)

    def test_absent_optional_env_keeps_private_defaults_and_bytes(self):
        f = self.fixture()
        before = f.m.read_env()
        self.assertNotIn("PROXY_LOCAL_LAN_IP", metadata.environment(before))
        self.assertEqual(access.listener_mode(metadata.environment(before)), "address")
        f.prepare()
        self.assertEqual(f.m.read_env(), before)
        self.assertEqual((access.directory(f.m) / "before.env").read_bytes(), before.encode())

    def test_first_native_recreation_requires_verified_backup_and_narrow_delta(self):
        f = self.fixture(hostname="homebox.localhost")
        with self.assertRaisesRegex(ValueError, "fresh verified private backup"):
            f.prepare()
        g = self.fixture(hostname="homebox.localhost")
        backup = g.backup()
        spec = g.prepare()
        self.assertTrue(spec["native_reconfigure"])
        self.assertEqual(access.changed(spec), {"proxy", "assistant-api", "homebox"})
        self.assertEqual(spec["native_backup"]["sha256"], metadata.file_digest(backup))
        cfg = forward(g.cfg, LAN)
        access.validate_delta(g.cfg, cfg, TAIL, "access-test", metadata.PREFIXES, local_ip=LAN)
        self.assertEqual(cfg["services"]["homebox"]["environment"], {"HBOX_OPTIONS_HOSTNAME": ""})

    def test_native_writer_refuses_backup_drift_before_recreation_flag(self):
        f = self.fixture(hostname="homebox.localhost")
        backup = f.backup()
        f.prepare()
        backup.write_bytes(b"synthetic changed archive")
        with patch.dict(os.environ, {"LC_ACCESS_PHASE": "configuring-native"}, clear=True), \
                self.assertRaisesRegex(ValueError, "unchanged private backup"):
            access.dispatch("access-phase", f.m)
        self.assertFalse(f.m.journal()["access_change"].get("native_recreation_started", False))

    def test_repeat_LAN_has_no_native_recreation_or_env_delta(self):
        f = self.fixture(local=OLD_LAN)
        spec = f.prepare()
        self.assertFalse(spec["native_reconfigure"])
        self.assertEqual(access.changed(spec), {"proxy", "assistant-api"})
        cfg = forward(f.cfg, LAN)
        access.validate_delta(f.cfg, cfg, TAIL, "access-test", metadata.PREFIXES, local_ip=LAN)
        self.assertEqual(cfg["services"]["homebox"]["environment"], f.cfg["services"]["homebox"]["environment"])

    def test_candidate_refuses_unrelated_native_env_and_wrong_LAN_target(self):
        before = compose()
        for mutate in (
                lambda c: c["services"]["homebox"]["environment"].update(UNAPPROVED="1"),
                lambda c: c["services"]["proxy"]["ports"][-1].update(target=8443),
                lambda c: c["services"]["proxy"]["volumes"][-1].update(read_only=False)):
            cfg = forward(before, LAN)
            mutate(cfg)
            with self.subTest(mutate=mutate), self.assertRaisesRegex(ValueError, "beyond"):
                access.validate_delta(before, cfg, TAIL, "access-test", metadata.PREFIXES, local_ip=LAN)

    def test_shell_backup_record_precedes_capture_and_prepare(self):
        # Execute the actual shell entry, stopping at prepare, with only command-boundary stubs.
        harness = r'''
set -eu
confirm() { :; }
ip() { printf '[]\n'; }
tailscale() { printf '%s\n' "$TAIL"; }
docker() { case "$1" in exec) printf '1\n';; esac; }
dce() { case "$1" in ps) printf 'synthetic-api\n';; *) printf '{}\n';; esac; }
capture() { printf 'capture\n' >>"$EVENTS"; }
sh() { case "$1" in
  */backup.sh) printf 'backup:%s\n' "$HOMEBOX_BACKUP_STOP_TIMEOUT" >>"$EVENTS";;
  */local_lan_evidence.sh) :;;
  *) return 90;;
esac; }
meta() {
  printf '%s\n' "$1" >>"$EVENTS"
  case "$1" in
    access-local-preflight) printf '%s\n' "$NATIVE" >"$STATE/work/access-needs-native";;
    access-prepare) exit 0;;
  esac
}
. "$DEPLOY/lifecycle/access.sh"
'''
        for native in ("1", "0"):
            state = self.root / ("shell-" + native)
            (state / "work").mkdir(parents=True)
            events = state / "events"
            env = {**os.environ, "STATE": str(state), "DEPLOY": str(DEPLOY), "TAIL": TAIL,
                   "EVENTS": str(events), "NATIVE": native, "ACCESS_ACTION": "lan",
                   "ACCESS_LOCAL": "1", "ACCESS_LISTEN_ALL": "0", "PROJECT": "synthetic",
                   "EXTERNAL": "0", "LAN": "1", "LOCAL_LAN": "0", "TEST_OVERRIDE": "", "PYTHON": "unused"}
            result = subprocess.run(["/bin/sh", "-c", harness], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            rows = events.read_text().splitlines()
            self.assertEqual(rows[-1], "access-prepare")
            if native == "1":
                self.assertLess(rows.index("backup:-1"), rows.index("access-backup-record"))
                self.assertLess(rows.index("access-backup-record"), rows.index("capture"))
                self.assertLess(rows.index("capture"), rows.index("access-prepare"))
            else:
                self.assertNotIn("capture", rows)
                self.assertNotIn("access-backup-record", rows)

    def test_rescue_guard_retains_source_image_volume_secret_and_UUID_checks(self):
        for kind, message in (("source", "source/binding"), ("image", "image ID"),
                              ("volume", "volume identity"), ("secret", "Core/provider"),
                              ("uuid", "UUID")):
            f = self.fixture(local=OLD_LAN)
            f.prepare()
            if kind == "source":
                original = metadata.file_digest
                f.m.file_digest.side_effect = lambda p: "drift" if p == DEPLOY / "homebox" else original(p)
            elif kind == "image":
                f.runtime["homebox"]["Image"] = "sha256:drift"
            elif kind == "volume":
                f.m.volume_snapshot.return_value = {"data": "different"}
            elif kind == "secret":
                (f.m.DEPLOY / "secrets/unapproved").write_text("synthetic")
            else:
                original = metadata.environment
                f.m.environment.side_effect = lambda b: {**original(b), "HOMEBOX_INSTANCE_ID": "different"}
            with self.subTest(kind=kind), self.assertRaisesRegex(ValueError, message):
                access.dispatch("access-rescue", f.m)
            self.assertFalse(f.m.journal()["access_change"].get("rescue_authorized", False))

    def test_before_env_and_precomputed_rescue_plan_are_immutable(self):
        for filename, message in (("before.env", "snapshot"), ("after.env", "snapshot"), ("rescue.env", "snapshot"),
                                  ("rescue-expected-config.json", "precomputed")):
            f = self.fixture(local=OLD_LAN)
            f.prepare()
            path = access.directory(f.m) / filename
            path.write_bytes(path.read_bytes() + b"\n")
            with self.subTest(filename=filename), self.assertRaisesRegex(ValueError, message):
                access.guard(f.m, require_address=False)

    def test_rescue_uses_raw_assignment_not_failed_physical_evidence(self):
        f = self.fixture(local=OLD_LAN)
        f.prepare()
        f.rescue_log()
        f.inventory(OLD_LAN, down=True)
        self.assertTrue(local_lan.assigned(f.m, OLD_LAN))
        with self.assertRaises(ValueError):
            local_lan.evidence(f.m, OLD_LAN)
        with self.assertRaisesRegex(ValueError, "OS-proven"):
            access.dispatch("access-rescue", f.m)
        f.inventory("")
        access.dispatch("access-rescue", f.m)
        self.assertTrue(f.m.journal()["access_change"]["rescue_authorized"])
        self.assertEqual(f.m.journal()["access_change"]["direction"], "rescue")

    def test_rescue_requires_current_exact_bind_failure(self):
        for malformed in ("wrong-attempt", "wrong-ip", "unrelated-error", "wrong-phase"):
            f = self.fixture(local=OLD_LAN)
            f.prepare()
            f.inventory("")
            f.rescue_log()
            path = f.m.WORK / "access-exact-restore.log"
            body = path.read_text()
            if malformed == "wrong-attempt":
                path.write_text(body.replace("b" * 32, "a" * 32))
            elif malformed == "wrong-ip":
                path.write_text(body.replace(OLD_LAN, LAN))
            elif malformed == "unrelated-error":
                path.write_text(body.replace("cannot assign requested address", "permission denied"))
            else:
                f.update_change(phase="configuring-api")
            with self.subTest(malformed=malformed), self.assertRaisesRegex(ValueError, "exact bind failure"):
                access.dispatch("access-rescue", f.m)

    def test_rescue_candidate_may_only_withdraw_old_LAN_sockets(self):
        f = self.fixture(local=OLD_LAN)
        f.prepare()
        f.inventory("")
        f.rescue_log()
        access.dispatch("access-rescue", f.m)
        expected = metadata.load(access.directory(f.m) / "rescue-expected-config.json")
        f.cfg = copy.deepcopy(expected)
        access.candidate(f.m)
        self.assertEqual(metadata.load(access.directory(f.m) / "target-config.json"), expected)
        f.cfg["services"]["homebox"]["environment"]["UNAPPROVED"] = "1"
        with self.assertRaisesRegex(ValueError, "withdraw only"):
            access.candidate(f.m)

    def test_rescue_preserves_absent_empty_and_precise_production_ports(self):
        for shape in ("absent", "empty", "production"):
            with self.subTest(ports=shape):
                f = self.fixture(local=OLD_LAN)
                if shape == "absent":
                    del f.cfg["services"]["proxy"]["ports"]
                elif shape == "empty":
                    f.cfg["services"]["proxy"]["ports"] = []
                if shape != "production":
                    f.runtime["proxy"]["HostConfig"]["PortBindings"] = {}
                expected = copy.deepcopy(f.cfg)
                if shape == "production":
                    self.assertEqual(len(f.cfg["services"]["proxy"]["ports"]), 6)
                    expected["services"]["proxy"]["ports"] = [
                        {"mode": "ingress", "host_ip": ip, "target": port,
                         "published": str(port), "protocol": "tcp"}
                        for ip, port in (("127.0.0.1", 80), ("127.0.0.1", 443),
                                         (TAIL, 443), (TAIL, 8443))]
                access.normalize(expected)
                env = {"LC_ACCESS_ACTION": "lan", "LC_ACCESS_IP": LAN, "LC_ACCESS_LISTEN_ALL": "0"}
                if shape != "production":
                    # The supported override deliberately publishes no ports.
                    f.m.TEST_COMPOSE_OVERRIDE = f.root / "synthetic-override.yml"
                    f.m.TEST_COMPOSE_OVERRIDE.write_text("services: {}\n")
                    env["LC_TEST_OVERRIDE"] = "1"
                with patch.dict(os.environ, env, clear=True):
                    access.prepare(f.m)
                    actual = metadata.load(access.directory(f.m) / "rescue-expected-config.json")
                    self.assertEqual(actual, expected)
                    self.assertEqual("ports" in actual["services"]["proxy"], shape != "absent")
                    f.inventory("")
                    f.rescue_log()
                    access.dispatch("access-rescue", f.m)
                    # Simulate raw Compose output independently of the saved plan.
                    f.cfg = copy.deepcopy(expected)
                    access.candidate(f.m)
                    self.assertEqual(metadata.load(access.directory(f.m) / "target-config.json"), expected)

    def test_verified_rescue_is_always_pending_and_retains_original_environment(self):
        f = self.fixture(local=OLD_LAN)
        f.prepare()
        before = (access.directory(f.m) / "before.env").read_bytes()
        f.inventory("")
        f.rescue_log()
        access.dispatch("access-rescue", f.m)
        f.cfg = metadata.load(access.directory(f.m) / "rescue-expected-config.json")
        access.candidate(f.m)
        f.runtime["proxy"]["HostConfig"]["PortBindings"] = access.public_ports(f.cfg["services"]["proxy"])
        f.proofs("rescue")
        with patch.dict(os.environ, {"LC_ACCESS_RECOVERY_ONLY": "0"}, clear=True):
            access.finish(f.m)
        change = f.m.journal()["access_change"]
        self.assertEqual((change["status"], change["phase"], change["direction"]),
                         ("pending", "rescue-active", "forward"))
        self.assertEqual((f.m.DEPLOY / ".env").read_bytes(), before)
        self.assertEqual((access.directory(f.m) / "before.env").read_bytes(), before)

    def test_rollback_requires_exact_config_and_restores_exact_old_env(self):
        f = self.fixture(local=OLD_LAN)
        f.prepare()
        before = (access.directory(f.m) / "before.env").read_bytes()
        f.update_change(direction="rollback")
        access.candidate(f.m)
        f.cfg["services"]["homebox"]["environment"]["UNAPPROVED"] = "1"
        with self.assertRaisesRegex(ValueError, "exact original"):
            access.candidate(f.m)
        f.cfg = copy.deepcopy(access.plan(f.m)["config"])
        f.proofs("rollback")
        with patch.dict(os.environ, {"LC_ACCESS_RECOVERY_ONLY": "0"}, clear=True):
            access.finish(f.m)
        self.assertEqual((f.m.DEPLOY / ".env").read_bytes(), before)
        self.assertEqual(f.m.journal()["access_change"]["status"], "rolled-back")

    def test_registration_restore_uses_validated_dual_hostname_without_waiving_guard(self):
        for dual, hostname, valid in ((True, "", True), (False, "homebox.localhost", True),
                                       (False, "", False), (False, "wrong.invalid", False),
                                       (True, "homebox.localhost", False), (True, "wrong.invalid", False)):
            body = "HOMEBOX_HOST=homebox.localhost\nHBOX_ALLOW_REGISTRATION=false\n"
            if dual:
                body += "PROXY_LOCAL_LAN_IP=" + LAN + "\nPROXY_LAN_IP=" + TAIL + "\n"
            state = {"registration_restore": body, "registration_nonce": "nonce",
                     "binding": {"project": "synthetic"}}
            native = {"Id": "synthetic-native", "State": {"Running": True, "Health": {"Status": "healthy"}},
                      "Config": {"Labels": {"com.docker.compose.project": "synthetic"}, "Env": [
                          "HBOX_OPTIONS_ALLOW_REGISTRATION=false", "HBOX_OPTIONS_HOSTNAME=" + hostname]}}
            response = io.StringIO('{"allowRegistration": false}')
            opener = Mock()
            opener.open.return_value = response
            with self.subTest(dual=dual, hostname=hostname), \
                    patch.object(metadata, "journal", return_value=state), \
                    patch.object(metadata, "read_env", return_value=body), \
                    patch.object(metadata, "load", return_value=[native]), \
                    patch.object(metadata, "config", return_value=compose(LAN if dual else "", hostname="" if dual else "homebox.localhost")), \
                    patch.object(metadata, "update") as update, \
                    patch.object(metadata.urllib.request, "build_opener", return_value=opener):
                if valid:
                    metadata.verify_registration()
                    self.assertFalse(update.call_args.kwargs["registration_verified"]["allowRegistration"])
                else:
                    with self.assertRaisesRegex(ValueError, "hostname"):
                        metadata.verify_registration()
                    update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
