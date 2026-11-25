"""Docker-controller integration harness, never a production maintenance driver.

Mount public source read-only at /source, a NEW absolute host workspace at the
same absolute container path, and the reviewed Docker socket. No real .env,
secrets, volumes, QA credentials or model service is used. Retains its fixtures.
"""

import argparse
import datetime as dt
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
import uuid


PYTHON = "python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df"


def run(command, *, output=None, expected=0, env=None, timeout=900):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, timeout=timeout, check=False)
    if output:
        output.write_bytes(result.stdout)
        os.chmod(output, 0o600)
    if (expected == 0 and result.returncode != 0) or (expected != 0 and result.returncode == 0):
        raise AssertionError("Command returned unexpected status; inspect the private fixture log.")
    return result.stdout


def native(mode):
    """Executed in a fresh child in Homebox's own network namespace."""
    spec = importlib.util.spec_from_file_location("account_client", "/source/deploy/lifecycle/accounts.py")
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    state = Path("/state")
    if mode in ("registration-open", "registration-closed"):
        assert api.request("GET", "/status")["allowRegistration"] is (mode == "registration-open")
        print("PASS: native public registration status matches the expected state")
        return
    owner = json.loads((state / "owner.json").read_text())
    if mode == "closed-empty":
        try:
            api.request("POST", "/users/register", {
                "name": owner["name"], "email": owner["username"], "password": owner["password"]})
        except api.NativeError as error:
            assert error.status == 403
            print("PASS: empty database does not bypass disabled registration")
            return
        raise AssertionError("Closed registration unexpectedly permitted initial signup")
    token = api.request("POST", "/users/login", {
        "username": owner["username"], "password": owner["password"], "stayLoggedIn": False})["token"]
    profile = api.request("GET", "/users/self", token=token)["item"]
    groups = api.request("GET", "/groups/all", token=token)
    expected_file = state / "expected.json"
    if mode == "seed":
        assert len(groups) == 1, "Fresh synthetic owner must have one collection"
        collection = groups[0]["id"]
        types = api.request("GET", "/entity-types", token=token, collection=collection)
        location_type = next(t["id"] for t in types if t.get("isLocation"))
        location = api.request("POST", "/entities", {
            "name": "Lifecycle synthetic location", "entityTypeId": location_type},
            token=token, collection=collection)
        item = api.request("POST", "/entities", {
            "name": "Lifecycle synthetic item", "quantity": 7, "parentId": location["id"],
            "description": "Generated lifecycle fixture, not private inventory"},
            token=token, collection=collection)
        attachment = b"Synthetic lifecycle attachment, exact-byte preservation.\n" * 13
        boundary = "lifecycle-" + uuid.uuid4().hex
        multipart = (
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"name\"\r\n\r\nfixture.txt\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"fixture.txt\"\r\n"
            "Content-Type: text/plain\r\n\r\n").encode() + attachment + f"\r\n--{boundary}--\r\n".encode()
        import urllib.request
        request = urllib.request.Request(api.BASE + "/entities/" + item["id"] + "/attachments",
            data=multipart, method="POST", headers={"Authorization": token, "X-Tenant": collection,
            "Content-Type": "multipart/form-data; boundary=" + boundary})
        with api.CLIENT.open(request, timeout=30) as response:
            assert response.status == 201
        item = api.request("GET", "/entities/" + item["id"], token=token, collection=collection)
        expected = {"user": profile["id"], "collection": collection, "item": item["id"],
                    "location": location["id"], "attachment": item["attachments"][0]["id"],
                    "attachment_sha256": hashlib.sha256(attachment).hexdigest(), "owner_session": token}
        expected_file.write_text(json.dumps(expected))
        os.chmod(expected_file, 0o600)
    expected = json.loads(expected_file.read_text())
    assert profile["id"] == expected["user"]
    assert api.request("GET", "/users/self", token=expected["owner_session"])["item"]["id"] == expected["user"]
    item = api.request("GET", "/entities/" + expected["item"], token=token, collection=expected["collection"])
    assert item["quantity"] == 7
    assert item["location"]["id"] == expected["location"] and item["parent"]["id"] == expected["location"]
    assert item["attachments"][0]["id"] == expected["attachment"]
    import urllib.request
    request = urllib.request.Request(api.BASE + "/entities/" + expected["item"] +
        "/attachments/" + expected["attachment"], headers={
            "Authorization": token, "X-Tenant": expected["collection"]})
    with api.CLIENT.open(request, timeout=30) as response:
        assert hashlib.sha256(response.read()).hexdigest() == expected["attachment_sha256"]
    def denied(method, route, payload=None, *, auth=token, collection=expected["collection"], statuses=None):
        try:
            api.request(method, route, payload, token=auth, collection=collection)
        except api.NativeError as error:
            assert error.status in statuses if statuses else 400 <= error.status < 500
            return
        raise AssertionError("A native authorization/invitation denial was expected")
    if mode in ("membership-before", "membership-after"):
        member = json.loads((state / "member.json").read_text())
        member_token = api.request("POST", "/users/login", {
            "username": member["username"], "password": member["password"]})["token"]
        member_profile = api.request("GET", "/users/self", token=member_token)["item"]
        if mode == "membership-before":
            assert api.request("GET", "/groups", token=member_token)["id"] == expected["collection"]
            assert api.request("GET", "/entities/" + expected["item"], token=member_token,
                               collection=expected["collection"])["id"] == expected["item"]
            denied("POST", "/groups/invitations", {"uses": 1}, auth=member_token)
            denied("DELETE", "/groups/members/" + expected["user"], auth=member_token)
            denied("DELETE", "/groups/members/" + expected["user"])
            expired = api.request("POST", "/groups/invitations", {
                "uses": 1, "expiresAt": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=3)).isoformat()},
                token=token, collection=expected["collection"])
            time.sleep(4)
            used = json.loads((state / "invitation.json").read_text())["token"]
            members_before = {member["id"] for member in api.request(
                "GET", "/groups/members", token=token, collection=expected["collection"])}
            for value in ("synthetic-invalid-token", expired["token"], used):
                email, password = secrets.token_hex(6) + "@example.invalid", secrets.token_urlsafe(24)
                denied("POST", "/users/register", {"name": "Synthetic denied registration",
                    "email": email, "password": password, "token": value},
                    auth=None, collection=None, statuses=(500,))
                denied("POST", "/users/login", {"username": email, "password": password},
                       auth=None, collection=None, statuses=(401,))
                assert {member["id"] for member in api.request(
                    "GET", "/groups/members", token=token, collection=expected["collection"])} == members_before
            expected["member"] = member_profile["id"]
            expected["member_session"] = member_token
            other = api.request("POST", "/groups", {"name": "Synthetic second collection"},
                                token=member_token, collection=expected["collection"])
            expected["other_collection"] = other["id"]
            expected_file.write_text(json.dumps(expected))
        else:
            assert member_profile["id"] == expected["member"]
            assert api.request("GET", "/users/self", token=expected["member_session"])["item"]["id"] == expected["member"]
            denied("GET", "/entities/" + expected["item"], auth=expected["member_session"])
            denied("GET", "/entities/" + expected["item"], auth=member_token)
            groups = api.request("GET", "/groups/all", token=member_token)
            assert expected["collection"] not in {group["id"] for group in groups}
            assert expected["other_collection"] in {group["id"] for group in groups}
    elif mode in ("solo-before", "solo-after"):
        solo = json.loads((state / "solo.json").read_text())
        solo_token = api.request("POST", "/users/login", {
            "username": solo["username"], "password": solo["password"]})["token"]
        solo_profile = api.request("GET", "/users/self", token=solo_token)["item"]
        if mode == "solo-before":
            expected["solo"] = solo_profile["id"]
            expected_file.write_text(json.dumps(expected))
        else:
            assert solo_profile["id"] == expected["solo"]
            groups = api.request("GET", "/groups/all", token=solo_token)
            assert len(groups) == 1 and groups[0]["id"] != expected["collection"]
            denied("GET", "/entities/" + expected["item"], auth=solo_token)
    print("PASS: same native login/user, collection, item, quantity, location and exact attachment bytes")


def main():
    assert Path("/source/.lifecycle-public-source.json").is_file(), "Mount a reviewed public-only source snapshot, not the working repository"
    assert not Path("/source/deploy/.env").exists() and not Path("/source/deploy/secrets").exists()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--subnet", default="10.251.200.0/24")
    args = parser.parse_args()
    workspace = Path(args.workspace)
    assert workspace.is_absolute() and not workspace.exists(), "Use a NEW absolute workspace"
    subnet = ipaddress.ip_network(args.subnet)
    assert subnet.is_private and subnet.prefixlen == 24
    existing = run(["docker", "network", "ls", "-q"]).decode().split()
    if existing:
        for network in json.loads(run(["docker", "network", "inspect", *existing])):
            for entry in network.get("IPAM", {}).get("Config") or []:
                if entry.get("Subnet"):
                    other = ipaddress.ip_network(entry["Subnet"])
                    assert other.version != subnet.version or not subnet.overlaps(other), "Choose a non-overlapping test subnet"
    workspace.mkdir(mode=0o700)
    project = "homebox-lifecycle-test-" + uuid.uuid4().hex[:10]
    root = workspace / "checkout"
    root.mkdir()
    state = workspace / "private"
    state.mkdir(mode=0o700)
    ignore = shutil.ignore_patterns("node_modules", "dist", "test-results", ".git", ".env*",
                                   "secrets", "backups", "__pycache__", ".pytest_cache",
                                   ".nuxt", ".output", ".data", "*.db", "*.db-wal", "*.db-shm", "*.log")
    for name in ("backend", "frontend", "apps", "services", "deploy"):
        shutil.copytree(Path("/source") / name, root / name, ignore=ignore)
    # Only the public template is copied deliberately; no real .env is mounted.
    shutil.copyfile("/source/deploy/.env.example", root / "deploy/.env.example")
    env = (root / "deploy/.env.example").read_text()
    for key, value in {
        "HOMEBOX_HOST": "homebox.localhost", "ASSISTANT_HOST": "inventory.localhost",
        "PROXY_LAN_IP": "192.168.250.10",
        "AI_CHAT_PROVIDER": "anthropic-messages", "AI_VISION_PROVIDER": "anthropic-messages",
        "AI_CHAT_BASE_URL": "http://provider-fixture:9000/anthropic",
        "AI_VISION_BASE_URL": "http://provider-fixture:9000/anthropic",
        "AI_CHAT_MODEL": "claude-haiku-4-5", "AI_VISION_MODEL": "claude-haiku-4-5",
        "AI_CHAT_API_KEY_FILE": "/run/secrets/ai/ai_chat_api_key",
        "AI_VISION_API_KEY_FILE": "/run/secrets/ai/ai_chat_api_key",
    }.items():
        env = re.sub(r"(?m)^" + key + r"=.*$", key + "=" + value, env)
    (root / "deploy/.env").write_text(env)
    os.chmod(root / "deploy/.env", 0o600)
    keys = root / "deploy/secrets/ai"
    keys.mkdir(parents=True, mode=0o755)
    (keys / "ai_chat_api_key").write_text("synthetic-fixture-" + secrets.token_hex(16))
    os.chmod(keys / "ai_chat_api_key", 0o444)
    override = workspace / "isolated.yml"
    lines = ["services:", "  proxy:", "    ports: !reset []"]
    for name in ("proxy", "homebox", "assistant-api", "homebox-mcp", "pwa", "ollama"):
        if name != "proxy":
            lines.append("  " + name + ":")
        lines += ["    healthcheck:", "      interval: 1s", "      start_period: 1s", "      retries: 30"]
    lines += ["  provider-fixture:", "    image: " + PYTHON, "    read_only: true",
              "    cap_drop: [ALL]", "    security_opt: [no-new-privileges:true]",
              "    command: [python, -B, /fixture/provider.py]",
              "    volumes:", "      - " + str(state) + ":/fixture",
              "    networks: [ai_egress]", "networks:"]
    for name, cidr in zip(("edge", "browser", "homebox_ui", "inventory", "tools", "inference", "ai_egress"),
                          subnet.subnets(new_prefix=28)):
        lines += ["  " + name + ":", "    internal: true", "    ipam:", "      config:",
                  "        - subnet: " + str(cidr)]
    override.write_text("\n".join(lines) + "\n")
    provider = """from http.server import BaseHTTPRequestHandler,HTTPServer
from pathlib import Path
import signal
import sys
signal.signal(signal.SIGTERM,lambda *_: sys.exit(0))
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*args): pass
 def record(self):
  p=Path('/fixture/provider-calls.txt');p.write_text(str(int(p.read_text())+1) if p.exists() else '1')
  self.send_response(503);self.end_headers()
 do_GET=do_POST=do_HEAD=do_OPTIONS=do_PUT=do_PATCH=do_DELETE=do_TRACE=record
HTTPServer(('0.0.0.0',9000),Handler).serve_forever()
"""
    (state / "provider.py").write_text(provider)
    process_env = {**os.environ, "HOMEBOX_LIFECYCLE_TEST_OVERRIDE": str(override)}
    base = ["sh", str(root / "deploy/homebox"), "--project", project, "--state-dir", str(state), "--yes"]
    counter = 0
    def cli(*arguments, expected=0, ambient=None):
        nonlocal counter
        counter += 1
        return run([*base, *arguments], output=state / ("command-" + str(counter) + ".log"),
                   env={**process_env, **(ambient or {})}, expected=expected)
    def compose(*arguments):
        return run(["docker", "compose", "-p", project, "--env-file", str(root / "deploy/.env"),
                    "-f", str(root / "deploy/docker-compose.yml"),
                    "-f", str(root / "deploy/docker-compose.external-ai.yml"),
                    "-f", str(root / "deploy/docker-compose.lan.yml"),
                    "-f", str(override), *arguments])
    def native_check(mode):
        hb = compose("ps", "-q", "homebox").decode().strip()
        return run(["docker", "run", "--rm", "--network", "container:" + hb, "--read-only",
                    "--cap-drop", "ALL", "--mount", "type=bind,source=" + str(root) + ",target=/source,readonly",
                    "--mount", "type=bind,source=" + str(state) + ",target=/state", PYTHON,
                    "python", "-B", "/source/deploy/tests/lifecycle_integration.py", "--native", mode],
                   output=state / ("native-" + mode + "-" + str(counter) + ".log"))
    def fingerprints():
        result = {}
        for path in (root / "deploy/secrets").rglob("*"):
            if path.is_file():
                result[str(path.relative_to(root))] = [hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode & 0o777]
        ca = state / "work/public-ca.crt"
        result["CA"] = hashlib.sha256(ca.read_bytes()).hexdigest()
        return result
    def inspection():
        ids = compose("ps", "-aq").decode().split()
        return {x["Config"]["Labels"]["com.docker.compose.service"]: x
                for x in json.loads(run(["docker", "inspect", *ids]))}
    proof = {"project": project, "subnet": str(subnet), "no_production_mounts": True,
             "no_host_ports": True, "checks": {}, "status": "RUNNING"}
    try:
        cli("fresh", "--resume")
        proof["checks"]["fresh_six_services_https_assets"] = True
        original = inspection()
        assert all(not x["HostConfig"].get("PortBindings") for x in original.values())
        preserved = fingerprints()
        owner = {"name": "Synthetic lifecycle owner", "username": "owner@example.invalid",
                 "password": secrets.token_urlsafe(24)}
        (state / "owner.json").write_text(json.dumps(owner))
        os.chmod(state / "owner.json", 0o600)
        native_check("closed-empty")
        bad_owner = {**owner, "password": []}
        (state / "bad-owner.json").write_text(json.dumps(bad_owner))
        os.chmod(state / "bad-owner.json", 0o600)
        before_bootstrap = (root / "deploy/.env").read_bytes()
        cli("users", "bootstrap", "--credentials-file", "bad-owner.json", expected=1)
        assert (root / "deploy/.env").read_bytes() == before_bootstrap
        cli("users", "bootstrap", "--credentials-file", "owner.json")
        assert "HBOX_ALLOW_REGISTRATION=false" in (root / "deploy/.env").read_text()
        native_check("seed")
        # Model an interrupted public-registration window, with an unrelated
        # operator comment added afterward. Recovery uses the delivered command.
        record = json.loads((state / "installation.json").read_text())
        prior_body = "".join(line for line in (root / "deploy/.env").read_text().splitlines(keepends=True)
                             if not line.startswith(("HBOX_ALLOW_REGISTRATION=", "ASSISTANT_INSTANCE_LABEL=")))
        record["registration_restore"] = prior_body
        (state / "installation.json").write_text(json.dumps(record))
        note = "\n# synthetic concurrent operator note\n"
        (root / "deploy/.env").write_text(prior_body + "HBOX_ALLOW_REGISTRATION=true\n" + note)
        compose("up", "-d", "--no-deps", "--no-build", "--pull", "never", "--wait", "homebox")
        malicious_ambient = {"HBOX_ALLOW_REGISTRATION": "true", "HOMEBOX_HOST": "ignored-ambient.example.invalid",
                             "ASSISTANT_INSTANCE_LABEL": "ignored-ambient-label"}
        override_body = override.read_text()
        forbidden = workspace / "forbidden-empty-native-data"
        forbidden.mkdir(mode=0o777)
        os.chmod(forbidden, 0o777)
        original_native = inspection()["homebox"]["Id"]
        override.write_text(override_body.replace("  homebox:\n",
            "  homebox:\n    volumes:\n      - type: bind\n        source: " + str(forbidden) +
            "\n        target: /data\n"))
        try:
            cli("users", "restore-registration", ambient=malicious_ambient, expected=1)
            assert inspection()["homebox"]["Id"] == original_native and not list(forbidden.iterdir())
            assert json.loads((state / "installation.json").read_text())["registration_restore"] is not None
            native_check("verify")
        finally:
            override.write_text(override_body)
        proof["checks"]["recovery_rejects_changed_desired_data_mount_before_start_or_empty_DB_creation"] = True
        override.write_text(override_body.replace("  homebox:\n",
            "  homebox:\n    environment:\n      HBOX_OPTIONS_ALLOW_REGISTRATION: 'true'\n"))
        try:
            cli("users", "restore-registration", ambient=malicious_ambient, expected=1)
            assert json.loads((state / "installation.json").read_text())["registration_restore"] is not None
            native_check("registration-open")
        finally:
            override.write_text(override_body)
        cli("users", "restore-registration", ambient=malicious_ambient)
        assert (root / "deploy/.env").read_text() == prior_body + note
        assert json.loads((state / "installation.json").read_text())["registration_restore"] is None
        native_check("registration-closed")
        native_check("verify")
        native_env = dict(value.split("=", 1) for value in inspection()["homebox"]["Config"]["Env"])
        api_env = dict(value.split("=", 1) for value in inspection()["assistant-api"]["Config"]["Env"])
        assert native_env["HBOX_OPTIONS_ALLOW_REGISTRATION"] == "false"
        assert api_env["ASSISTANT_INSTANCE_LABEL"] == ""
        assert not any(line.startswith(("HBOX_ALLOW_REGISTRATION=", "ASSISTANT_INSTANCE_LABEL="))
                       for line in (root / "deploy/.env").read_text().splitlines())
        proof["checks"]["omitted_registration_and_optional_config_ignore_ambient_preserve_omission_and_clear_verified_marker"] = True
        proof["checks"]["recovery_ignores_ambient_flags_verifies_live_state_and_retains_marker_on_failure"] = True
        proof["checks"]["explicit_bootstrap_restored_registration"] = True
        proof["checks"]["disabled_empty_DB_failed_signup_and_interrupted_bootstrap_restore"] = True
        expected = json.loads((state / "expected.json").read_text())
        member = {"name": "Synthetic lifecycle member", "username": "member@example.invalid",
                  "password": secrets.token_urlsafe(24)}
        (state / "member.json").write_text(json.dumps(member))
        os.chmod(state / "member.json", 0o600)
        cli("users", "invite", "--credentials-file", "owner.json",
            "--collection", expected["collection"], "--output", "invitation.json")
        cli("users", "accept-invite", "--credentials-file", "member.json", "--invitation-file", "invitation.json")
        native_check("membership-before")
        expected = json.loads((state / "expected.json").read_text())
        cli("users", "join-invite", "--credentials-file", "member.json", expected=1)
        proof["checks"]["closed_registration_invitation"] = True
        proof["checks"]["invalid_expired_reused_invites_and_member_permission_denials"] = True
        proof["checks"]["unsupported_existing_account_join_is_refused"] = True
        before = inspection()
        source = root / "apps/pwa/index.html"
        source.write_text(re.sub(r"<title>.*?</title>", "<title>Lifecycle synthetic redeploy</title>", source.read_text()))
        foreign = workspace / "foreign-configuration-do-not-use"
        foreign.mkdir(mode=0o700)
        (foreign / ".env").write_text("SYNTHETIC_FOREIGN_CONFIGURATION_MUST_NOT_CHANGE=1\n")
        foreign_before = (foreign / ".env").read_bytes()
        ambient_backup = {"HOMEBOX_DEPLOY_DIR": str(foreign), "HOMEBOX_ENV_FILE": str(foreign / ".env"),
                          "HOMEBOX_BACKUP_DIR": str(foreign / "backups")}
        alternative = workspace / "alternate-private"
        alternative_base = ["sh", str(root / "deploy/homebox"), "--project", project,
                            "--state-dir", str(alternative), "--yes"]
        with (state / "concurrent-first.log").open("wb") as output:
            first = subprocess.Popen([*base, "redeploy"], env={**process_env, **ambient_backup},
                                     stdout=output, stderr=subprocess.STDOUT)
            try:
                deadline = time.monotonic() + 10
                while not (root / "deploy/.homebox-operation.lock").is_dir():
                    assert first.poll() is None and time.monotonic() < deadline
                    time.sleep(0.02)
                second = run([*alternative_base, "redeploy"], env=process_env, expected=1,
                             output=state / "concurrent-second.log")
                assert b"shared installation lock" in second
            finally:
                first_result = first.wait(timeout=900)
        assert first_result == 0, "Inspect the private concurrent-first log"
        assert (foreign / ".env").read_bytes() == foreign_before and not (foreign / "backups").exists()
        alternate_output = run([*alternative_base, "redeploy"], env=process_env, expected=1,
                               output=state / "alternate-state.log")
        assert b"original canonical state directory" in alternate_output
        after = inspection()
        assert before["pwa"]["Id"] != after["pwa"]["Id"]
        assert all(before[n]["Id"] == after[n]["Id"] for n in ("homebox", "homebox-mcp", "assistant-api", "ollama", "proxy"))
        assert fingerprints() == preserved
        native_check("verify")
        proof["checks"]["shared_lock_and_canonical_state_refuse_concurrent_or_alternate_state"] = True
        proof["checks"]["backup_ignores_foreign_ambient_deploy_env_and_destination"] = True
        proof["checks"]["source_redeploy_preserves_accounts_inventory_attachments_secrets_CA"] = True
        native_source = root / "backend/app/api/main.go"
        native_source.write_text(native_source.read_text() +
            "\n// Synthetic lifecycle preservation rebuild " + uuid.uuid4().hex + ".\n")
        native_before = inspection()
        cli("redeploy")
        after = inspection()
        assert native_before["homebox"]["Id"] != after["homebox"]["Id"]
        assert native_before["homebox"]["Image"] != after["homebox"]["Image"]
        assert all(native_before[n]["Id"] == after[n]["Id"]
                   for n in ("homebox-mcp", "assistant-api", "pwa", "ollama", "proxy"))
        native_check("verify")
        assert fingerprints() == preserved
        proof["checks"]["native_source_rebuild_recreates_native_preserves_old_session_account_data_attachments_secrets_CA"] = True
        ids = {name: item["Id"] for name, item in after.items()}
        env_bytes = (root / "deploy/.env").read_bytes()
        cli("redeploy")
        assert {name: item["Id"] for name, item in inspection().items()} == ids
        assert (root / "deploy/.env").read_bytes() == env_bytes
        cli("fresh", expected=1)
        assert fingerprints() == preserved
        proof["checks"]["repeat_redeploy_no_recreation_and_fresh_refusal"] = True
        dockerfile = root / "deploy/Dockerfile.homebox"
        old = dockerfile.read_bytes()
        dockerfile.write_text("THIS IS AN INTENTIONAL SYNTHETIC BUILD FAILURE\n")
        try:
            cli("redeploy", expected=1)
        finally:
            dockerfile.write_bytes(old)
        assert (root / "deploy/.env").read_bytes() == env_bytes
        assert {name: item["Id"] for name, item in inspection().items()} == ids
        native_check("verify")
        proof["checks"]["build_failure_leaves_active_release_untouched"] = True
        cli("users", "remove-member", "--credentials-file", "owner.json",
            "--collection", expected["collection"], "--user", expected["member"], "--confirm-revoke")
        native_check("membership-after")
        solo = {"name": "Synthetic solo member", "username": "solo@example.invalid",
                "password": secrets.token_urlsafe(24)}
        (state / "solo.json").write_text(json.dumps(solo))
        os.chmod(state / "solo.json", 0o600)
        cli("users", "invite", "--credentials-file", "owner.json", "--collection", expected["collection"],
            "--output", "solo-invitation.json")
        cli("users", "accept-invite", "--credentials-file", "solo.json",
            "--invitation-file", "solo-invitation.json")
        native_check("solo-before")
        expected = json.loads((state / "expected.json").read_text())
        cli("users", "remove-member", "--credentials-file", "owner.json",
            "--collection", expected["collection"], "--user", expected["solo"], "--confirm-revoke")
        native_check("solo-after")
        assert fingerprints() == preserved
        proof["checks"]["revocation_preserves_account_other_memberships_inventory_and_personal_fallback"] = True
        partial_root = workspace / "partial-checkout"
        partial_state = workspace / "partial-private"
        partial_state.mkdir(mode=0o700)
        shutil.copytree(root, partial_root, ignore=ignore)
        shutil.copyfile(root / "deploy/.env.example", partial_root / "deploy/.env.example")
        partial_dockerfile = partial_root / "deploy/Dockerfile.homebox"
        partial_dockerfile.write_text("THIS IS AN INTENTIONAL PARTIAL-INIT BUILD FAILURE\n")
        partial_base = ["sh", str(partial_root / "deploy/homebox"), "--project", project + "-partial",
                        "--state-dir", str(partial_state), "--yes"]
        run([*partial_base, "fresh"], output=partial_state / "first.log", env=process_env, expected=1)
        def partial_identity():
            paths = [partial_root / "deploy/.env", *(partial_root / "deploy/secrets").glob("*")]
            return {p.name: [hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode & 0o777]
                    for p in paths if p.is_file()}
        partial_before = partial_identity()
        assert len(partial_before) == 4
        run([*partial_base, "fresh", "--resume"], output=partial_state / "repeat.log",
            env=process_env, expected=1)
        assert partial_identity() == partial_before
        proof["checks"]["idempotent_partial_init_after_prestart_build_failure"] = True
        nginx = root / "apps/pwa/nginx.conf"
        original_nginx = nginx.read_bytes()
        nginx.write_text("intentionally_invalid_lifecycle_directive;\n")
        try:
            cli("redeploy", expected=1)
        finally:
            nginx.write_bytes(original_nginx)
        failed = inspection()
        assert not failed["proxy"]["State"]["Running"], "Failed cutover must keep ingress closed"
        receipt = json.loads((state / "installation.json").read_text())
        assert receipt["phase"] == "cutover" and receipt["backup"]["bytes"] > 0
        recovery = Path(receipt["recovery_receipt"])
        assert (recovery / "previous.env").is_file() and (recovery / "previous-images.json").is_file()
        native_check("verify")
        assert fingerprints() == preserved
        proof["checks"]["failed_stateless_cutover_closes_ingress_retains_receipt_and_all_native_data"] = True
        assert not (state / "provider-calls.txt").exists(), "No inference/provider requests are allowed"
        proof["checks"]["zero_provider_requests"] = True
        proof["status"] = "PASS"
        proof["end_state"] = "Intentional failed-PWA case verified closed ingress; only this synthetic project is stopped and retained."
    except Exception:
        proof["status"] = "FAIL"
        raise
    finally:
        proof["last_command_sequence"] = counter
        (workspace / "proof.json").write_text(json.dumps(proof, indent=2) + "\n")
        # Preserve fixture volumes, accounts, files and logs for diagnosis. Stop only our project.
        compose("stop", "--timeout", "-1")
        print(json.dumps({"status": proof["status"], "proof": str(workspace / "proof.json"),
                          "fixtures_retained": True, "production_untouched": True}))


def defaults():
    """Fresh defaults, explicit legacy adoption and no-reinitialization regressions."""
    assert Path("/source/.lifecycle-public-source.json").is_file()
    assert not Path("/source/deploy/.env").exists() and not Path("/source/deploy/secrets").exists()
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--subnet", required=True)
    args = parser.parse_args(sys.argv[2:])
    workspace, subnet = Path(args.workspace), ipaddress.ip_network(args.subnet)
    assert workspace.is_absolute() and not workspace.exists() and subnet.is_private and subnet.prefixlen == 24
    ids = run(["docker", "network", "ls", "-q"]).decode().split()
    if ids:
        for network in json.loads(run(["docker", "network", "inspect", *ids])):
            for entry in network.get("IPAM", {}).get("Config") or []:
                if entry.get("Subnet"):
                    other = ipaddress.ip_network(entry["Subnet"])
                    assert other.version != subnet.version or not subnet.overlaps(other)
    workspace.mkdir(mode=0o700)
    root, state = workspace / "checkout", workspace / "private"
    root.mkdir()
    state.mkdir(mode=0o700)
    ignored = shutil.ignore_patterns(".env*", "secrets", "backups", "node_modules", "dist", "test-results",
                                     ".git", "__pycache__", ".pytest_cache", ".nuxt", ".output", ".data")
    for name in ("backend", "frontend", "apps", "services", "deploy"):
        shutil.copytree(Path("/source") / name, root / name, ignore=ignored)
    shutil.copyfile("/source/deploy/.env.example", root / "deploy/.env.example")
    project = "homebox-lifecycle-test-default-" + uuid.uuid4().hex[:8]
    override = workspace / "isolated.yml"
    lines = ["services:", "  proxy:", "    ports: !reset []"]
    for name in ("proxy", "homebox", "homebox-mcp", "assistant-api", "pwa", "ollama"):
        if name != "proxy":
            lines.append("  " + name + ":")
        lines += ["    healthcheck:", "      interval: 1s", "      start_period: 1s", "      retries: 30"]
    lines += ["networks:"]
    for name, cidr in zip(("edge", "browser", "homebox_ui", "inventory", "tools", "inference"),
                          subnet.subnets(new_prefix=28)):
        lines += ["  " + name + ":", "    internal: true", "    ipam:", "      config:", "        - subnet: " + str(cidr)]
    override.write_text("\n".join(lines) + "\n")
    process_env = {**os.environ, "HOMEBOX_LIFECYCLE_TEST_OVERRIDE": str(override)}
    base = ["sh", str(root / "deploy/homebox"), "--project", project, "--state-dir", str(state), "--yes"]
    sequence = 0
    def cli(*arguments, expected=0):
        nonlocal sequence
        sequence += 1
        return run([*base, *arguments], env=process_env, expected=expected,
                   output=workspace / ("command-" + str(sequence) + ".log"))
    def compose(*arguments):
        return run(["docker", "compose", "-p", project, "--env-file", str(root / "deploy/.env"),
                    "-f", str(root / "deploy/docker-compose.yml"), "-f", str(override), *arguments])
    def containers():
        ids = compose("ps", "-aq").decode().split()
        return {item["Config"]["Labels"]["com.docker.compose.service"]: item
                for item in json.loads(run(["docker", "inspect", *ids]))} if ids else {}
    def volumes():
        names = run(["docker", "volume", "ls", "--filter",
                     "label=com.docker.compose.project=" + project, "--format", "{{.Name}}"]).decode().split()
        return {item["Name"]: {key: item.get(key) for key in ("CreatedAt", "Driver", "Labels", "Mountpoint")}
                for item in json.loads(run(["docker", "volume", "inspect", *names]))} if names else {}
    def keys():
        return {str(p.relative_to(root)): [hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode & 0o777]
                for p in (root / "deploy/secrets").rglob("*") if p.is_file()}
    proof = {"status": "RUNNING", "project": project, "checks": {}, "no_production_mounts": True,
             "no_host_ports": True, "no_model_download_or_inference": True}
    try:
        assert not (root / "deploy/.env").exists()
        cli("fresh")
        original = containers()
        assert len(original) == 6 and all(not item["HostConfig"].get("PortBindings") for item in original.values())
        api = dict(entry.split("=", 1) for entry in original["assistant-api"]["Config"]["Env"])
        assert all((api.get("AI_" + role + "_PROVIDER") or api["AI_PROVIDER"]) == "ollama"
                   for role in ("CHAT", "VISION"))
        listing = run(["docker", "exec", original["ollama"]["Id"], "ollama", "list"]).decode().splitlines()
        assert len([line for line in listing if line.strip()]) == 1
        assert b"MISSING or unavailable" in cli("models", "status")
        assert json.loads((state / "installation.json").read_text())["bootstrap_pending"] is True
        preserved_keys, preserved_volumes = keys(), volumes()
        body = (root / "deploy/.env").read_bytes()
        ids_before = {name: item["Id"] for name, item in original.items()}
        cli("status")
        cli("redeploy")
        cli("fresh", expected=1)
        assert {name: item["Id"] for name, item in containers().items()} == ids_before
        assert (root / "deploy/.env").read_bytes() == body and keys() == preserved_keys
        proof["checks"]["fresh_without_env_or_resume_uses_ollama_closed_signup_no_owner_or_weights"] = True
        proof["checks"]["default_status_and_noop_redeploy_preserve_all_ids_env_keys_and_CA_volume"] = True
        # Model an existing stack installed by the older, receipt-less helpers.
        marker = root / "deploy/.env.lifecycle-owner"
        marker.rename(state / "retained-owner-before-legacy-simulation.json")
        assert b"Unmanaged installation" in cli("redeploy", expected=1)
        state = workspace / "adopt-private"
        state.mkdir(mode=0o700)
        base = ["sh", str(root / "deploy/homebox"), "--project", project, "--state-dir", str(state), "--yes"]
        cli("adopt")
        assert json.loads(marker.read_text())["binding"]["state"] == str(state)
        assert {name: item["Id"] for name, item in containers().items()} == ids_before
        assert volumes() == preserved_volumes and keys() == preserved_keys and (root / "deploy/.env").read_bytes() == body
        proof["checks"]["legacy_stack_refuses_implicit_adoption_explicit_adopt_changes_no_runtime_data"] = True
        cli("backup")
        backup = json.loads((state / "installation.json").read_text())["backup"]
        assert backup["bytes"] > 0 and (root / "deploy/backups" / backup["file"]).is_file()
        compose("stop", "--timeout", "-1")
        assert b"requires all six original healthy services" in cli("redeploy", expected=1)
        assert not any(item["State"]["Running"] for item in containers().values())
        assert volumes() == preserved_volumes
        proof["checks"]["stopped_containers_refused_without_start_or_reinitialization"] = True
        compose("rm", "-f")
        assert not containers()
        assert b"All six original service containers" in cli("redeploy", expected=1)
        assert volumes() == preserved_volumes
        proof["checks"]["zero_containers_with_valid_receipt_refused_without_creation"] = True
        # Only an empty generated assistant fixture volume, after a verified backup.
        missing = project + "_assistant_data"
        assert missing in preserved_volumes
        run(["docker", "volume", "rm", missing])
        remaining = {name: value for name, value in preserved_volumes.items() if name != missing}
        assert b"volume identity is missing or changed" in cli("redeploy", expected=1)
        cli("fresh", "--resume", expected=1)
        assert not containers() and volumes() == remaining
        assert keys() == preserved_keys and (root / "deploy/.env").read_bytes() == body
        run(["docker", "volume", "inspect", missing], expected=1)
        proof["checks"]["missing_volume_refused_without_recreation_or_secret_env_changes"] = True
        proof["checks"]["only_backed_up_empty_assistant_test_volume_intentionally_removed"] = True
        proof["status"] = "PASS"
    except Exception:
        proof["status"] = "FAIL"
        raise
    finally:
        (workspace / "proof.json").write_text(json.dumps(proof, indent=2) + "\n")
        compose("stop", "--timeout", "-1")
        print(json.dumps({"status": proof["status"], "proof": str(workspace / "proof.json"),
                          "synthetic_only": True, "backup_and_remaining_fixtures_retained": True}))


if __name__ == "__main__":
    if sys.argv[1:2] == ["--native"]:
        native(sys.argv[2])
    elif sys.argv[1:2] == ["--defaults"]:
        defaults()
    else:
        main()
