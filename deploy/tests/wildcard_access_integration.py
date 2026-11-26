"""Focused wildcard migration proof using cached synthetic images and no ports."""

import argparse
import hashlib
import ipaddress
import json
from pathlib import Path
import secrets
import subprocess
import time

from access_integration import Fixture, REAL, TAIL_IP, image_sources
from test_wildcard_access import check_render


def stock_render(fixture, env_file, mode):
    args = [REAL, "compose", "-p", fixture.project, "--project-directory", str(fixture.root / "deploy"),
            "--env-file", str(env_file), "-f", str(fixture.root / "deploy/docker-compose.yml")]
    if mode != "private":
        args += ["-f", str(fixture.root / "deploy/docker-compose.lan.yml")]
    cfg = json.loads(fixture.run([*args, "config", "--format", "json"]))
    # The shared render validator's literal is deliberately replaced only for
    # this owned fixture's synthetic Tailnet assignment.
    import test_wildcard_access
    test_wildcard_access.IP = TAIL_IP
    check_render(cfg, mode)
    return cfg["services"]["proxy"]["ports"]


def staged_environment(fixture):
    change = fixture.journal()["access_change"]
    return fixture.state / "access" / change["id"] / "after.env"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image-source-project", required=True)
    parser.add_argument("--image-preflight-only", action="store_true")
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--subnet", type=ipaddress.ip_network)
    args = parser.parse_args()
    if args.image_preflight_only:
        print(json.dumps({"status": "PASS", "read_only": True,
                          "image_IDs": image_sources(args.image_source_project, subprocess.check_output)}))
        return
    if args.workspace is None or args.subnet is None:
        parser.error("--workspace and --subnet are required")
    fixture = Fixture(args.workspace, args.subnet, args.image_source_project)
    proof = {"status": "RUNNING", "project": fixture.project, "checks": {},
             "synthetic_only": True, "no_host_ports": True,
             "no_real_Tailnet_firewall_trust_or_provider_changes": True,
             "all_app_containers_volumes_retained": True}
    started = time.monotonic()
    try:
        owner = fixture.state / "owner.json"
        owner.write_text(json.dumps({"name": "Synthetic wildcard owner", "username": "wildcard@example.invalid",
                                     "password": secrets.token_urlsafe(24)}))
        owner.chmod(0o600)
        proof["image_IDs"] = fixture.legacy_existing()
        fixture.native("seed")
        fixture.companion("seed")
        original = fixture.healthy()
        original_journal = fixture.journal()
        original_env = (fixture.root / "deploy/.env").read_bytes()
        secret_before = {str(p.relative_to(fixture.root / "deploy")): hashlib.sha256(p.read_bytes()).hexdigest()
                         for p in (fixture.root / "deploy/secrets").rglob("*") if p.is_file()}
        proof["private_stock_ports"] = stock_render(fixture, fixture.root / "deploy/.env", "private")

        # Let real API/proxy recreation and TLS/Origin checks finish, then fault
        # after the protected environment is installed but before journal commit.
        fixture.cli("access", "tailscale", "--ip", TAIL_IP, "--listen-all",
                    mode="env-before-journal", expected=1)
        assert fixture.journal()["access_change"]["status"] == "pending"
        assert not fixture.current()["proxy"]["State"]["Running"]
        proof["wildcard_stock_ports"] = stock_render(fixture, staged_environment(fixture), "all-ipv4")
        plan_path = staged_environment(fixture).parent / "plan.json"
        assert json.loads(plan_path.read_text())["listener_mode"] == "all-ipv4"
        fixture.preserved(original, allow_proxy_stopped=True)
        fixture.cli("access", "rollback", mode="tailscale-unavailable")
        assert (fixture.root / "deploy/.env").read_bytes() == original_env
        assert fixture.journal()["overlays"] == original_journal["overlays"]
        fixture.preserved(original)
        fixture.native("verify")
        fixture.companion("verify")
        proof["checks"]["loopback_to_wildcard_real_proxy_TLS_Origin_then_exact_configuration_rollback"] = True

        fixture.cli("access", "tailscale", "--ip", TAIL_IP)
        addressed_env = (fixture.root / "deploy/.env").read_bytes()
        assert fixture.journal()["access_listener_mode"] == "address"
        proof["addressed_stock_ports"] = stock_render(fixture, fixture.root / "deploy/.env", "address")
        fixture.cli("access", "tailscale", "--ip", TAIL_IP, "--listen-all", mode="after-api", expected=1)
        assert not fixture.current()["proxy"]["State"]["Running"]
        fixture.cli("access", "rollback", mode="tailscale-unavailable")
        assert (fixture.root / "deploy/.env").read_bytes() == addressed_env
        assert fixture.journal()["access_listener_mode"] == "address"
        fixture.preserved(original)
        proof["checks"]["healthy_addressed_Tailnet_to_wildcard_fault_rolls_back_exact_previous_IP_mode"] = True

        fixture.cli("access", "tailscale", "--ip", TAIL_IP, "--listen-all")
        fixture.preserved(original)
        state = fixture.journal()
        assert state["access_change"]["status"] == "complete" and state["phase"] == "ready"
        assert state["access_listener_mode"] == "all-ipv4"
        assert state["resources"]["volumes"] == original_journal["resources"]["volumes"]
        assert state["resources"]["secrets"] == original_journal["resources"]["secrets"]
        assert state["resources"]["instance_id"] == original_journal["resources"]["instance_id"]
        assert {str(p.relative_to(fixture.root / "deploy")): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (fixture.root / "deploy/secrets").rglob("*") if p.is_file()} == secret_before
        assert stock_render(fixture, fixture.root / "deploy/.env", "all-ipv4") == proof["wildcard_stock_ports"]
        fixture.native("verify")
        fixture.companion("tailnet")
        operation = fixture.state / "access" / state["access_change"]["id"]
        assert json.loads((operation / "origin-proof.json").read_text())["IP_SAN"] == TAIL_IP
        before_ids = {key: value["Id"] for key, value in fixture.current().items()}
        fixture.cli("access", "tailscale", "--ip", TAIL_IP, "--listen-all")
        assert {key: value["Id"] for key, value in fixture.current().items()} == before_ids
        fixture.cli("status")
        proof["checks"].update({
            "healthy_addressed_Tailnet_to_wildcard_commits_and_repeats_as_noop": True,
            "exact_stock_127_HTTP80_and_single_wildcard_HTTPS443_8443_no_other_ports": True,
            "canonical_Tailnet_IP_SAN_CA_exact_Origin_and_auth_checks_unchanged": True,
            "all6_images_4_unaffected_IDs_started_env_volumes_UUID_keys_CA_and_inventory_preserved": True,
            "old_native_companion_sessions_and_new_canonical_IP_login_preserved": True})
        proof["status"] = "PASS"
    finally:
        current = fixture.current()
        running = [item["Id"] for item in current.values() if item["State"].get("Running")]
        if running:
            fixture.run([REAL, "stop", "--timeout", "-1", *running])
        proof["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (fixture.workspace / "proof.json").write_text(json.dumps(proof, indent=2) + "\n")
        print(json.dumps({"status": proof["status"], "proof": str(fixture.workspace / "proof.json")}))


if __name__ == "__main__":
    main()
