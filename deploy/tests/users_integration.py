"""Retained synthetic six-service CLI/TTY check; no host ports or model calls."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pty
import secrets
import select
import subprocess
import sys
import time

import access_integration as base
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lifecycle"))
from primary import identity

API = "sha256:e9625f5e1e4072fcfc3d3d17b1ccf3948208d8ff48c2812d6a3a01117bab5e51"
FAULT = r'''
if mode in ('register-lost-reply','register-not-sent') and '/source/deploy/lifecycle/accounts.py' in args:
 index=args.index('/source/deploy/lifecycle/accounts.py')
 if args[index+1:index+2]==['create']:
  code="""import sys
sys.path.insert(0,'/source/deploy/lifecycle')
import accounts
original=accounts.request
def request(method,route,*args,**kwargs):
 if method=='POST' and route=='/users/register' and MODE=='register-not-sent':
  raise OSError('Synthetic transport uncertainty before delivery')
 value=original(method,route,*args,**kwargs)
 if method=='POST' and route=='/users/register':
  raise OSError('Synthetic reply lost after native commit')
 return value
accounts.request=request
accounts.main(sys.argv[1:])
""".replace('MODE',repr(mode))
  args=args[:index]+['-c',code]+args[index+1:]
'''


def private(path, value):
    path.write_text(json.dumps(value))
    path.chmod(0o600)


def snapshot(fixture):
    current = fixture.current()
    assert len(current) == 6
    assert all(not value["HostConfig"].get("PortBindings") for value in current.values())
    return {name: identity(value) for name, value in current.items()}


def native_rows(fixture):
    native = fixture.current()["homebox"]
    volume = next(x["Name"] for x in native["Mounts"] if x["Destination"] == "/data")
    code = """import json,sys
from pathlib import Path
sys.path.insert(0,'/lifecycle')
from primary import native_rows
p=Path('/native/homebox.db')
wal=p.with_name('homebox.db-wal')
print(json.dumps({'wal_nonempty':wal.is_file() and wal.stat().st_size>0,
 'users':native_rows(p,'SELECT id,created_at,is_superuser FROM users'),
 'memberships':native_rows(p,'SELECT user_id,group_id,role FROM user_groups')}))
"""
    return json.loads(fixture.run([base.REAL, "run", "--rm", "--pull", "never", "--network", "none",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--user", native["Config"]["User"], "--mount", "type=volume,source=" + volume + ",target=/native,readonly",
        "--mount", "type=bind,source=" + str(fixture.root / "deploy/lifecycle") + ",target=/lifecycle,readonly",
        base.PYTHON, "python", "-B", "-c", code]))


def protected(fixture):
    proxy = fixture.current()["proxy"]
    volume = next(x["Name"] for x in proxy["Mounts"] if x["Destination"] == "/data")
    code = """import hashlib,json
from pathlib import Path
paths=list(Path('/keys').glob('*'))+[Path('/ca/caddy/pki/authorities/local')/n for n in ('root.crt','root.key')]
print(json.dumps({str(p):[hashlib.sha256(p.read_bytes()).hexdigest(),p.stat().st_mode,p.stat().st_uid,p.stat().st_gid] for p in paths if p.is_file()},sort_keys=True))
"""
    return json.loads(fixture.run([base.REAL, "run", "--rm", "--pull", "never", "--network", "none",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--mount", "type=bind,source=" + str(fixture.root / "deploy/secrets") + ",target=/keys,readonly",
        "--mount", "type=volume,source=" + volume + ",target=/ca,readonly",
        base.PYTHON, "python", "-B", "-c", code]))


def tty(fixture, steps, expected):
    (fixture.state / "fault-mode").write_text("none")
    master, slave = pty.openpty()
    process = subprocess.Popen([*fixture.base, "users", "create"], stdin=slave, stdout=slave, stderr=slave,
                               env=fixture.env, start_new_session=True)
    os.close(slave)
    output = bytearray()
    position = index = 0
    deadline = time.monotonic() + 180
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
                while index < len(steps):
                    prompt, response = steps[index]
                    found = output.find(prompt.encode(), position)
                    if found < 0:
                        break
                    position = found + len(prompt)
                    os.write(master, response.encode() + b"\n")
                    index += 1
            if process.poll() is not None and not ready:
                break
        else:
            process.kill()
            raise AssertionError("Private TTY test timed out")
        code = process.wait(timeout=15)
    finally:
        os.close(master)
        if process.poll() is None:
            process.kill()
            process.wait()
    path = fixture.state / ("tty-" + str(time.monotonic_ns()) + ".log")
    path.write_bytes(output)
    path.chmod(0o600)
    assert (code == 0) == (expected == 0), "TTY command status differs; inspect private transcript"
    assert index == len(steps), "TTY prompts were not all exercised"
    return bytes(output)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--workspace", type=Path, required=True)
    p.add_argument("--subnet", type=ipaddress.ip_network, required=True)
    p.add_argument("--image-source-project", required=True)
    p.add_argument("--resume-failed-fixture", action="store_true",
                   help="Continue only this driver's retained failed synthetic fixture; never a real installation")
    args = p.parse_args()
    original_images = base.image_sources
    def images(project, run):
        result = original_images(project, run)
        result["assistant-api"] = API
        return result
    base.image_sources = images
    if args.resume_failed_fixture:
        previous = json.loads((args.workspace / "proof.json").read_text())
        assert previous["status"] == "FAIL" and previous["synthetic_only"] and previous["API_image_ID"] == API
        assert previous["project"].startswith("homebox-lifecycle-test-access-")
        fixture = object.__new__(base.Fixture)
        fixture.workspace, fixture.root, fixture.state = args.workspace, args.workspace / "checkout", args.workspace / "private"
        fixture.project, fixture.override = previous["project"], args.workspace / "isolated.yml"
        fixture.sequence = max(int(x.stem.split("-")[1]) for x in fixture.state.glob("command-*.log"))
        fixture.env = {**os.environ, "PATH": str(args.workspace / "bin") + ":" + os.environ["PATH"],
                       "HOMEBOX_LIFECYCLE_TEST_OVERRIDE": str(fixture.override)}
        fixture.base = ["sh", str(fixture.root / "deploy/homebox"), "--project", fixture.project,
                        "--state-dir", str(fixture.state), "--yes"]
        # Replace only this driver's public helper sources, never state, config,
        # inventory or the deployment modules owned by another feature.
        import shutil
        for name in ("homebox", "lifecycle/primary.py", "lifecycle/primary.sh", "lifecycle/accounts.py",
                     "lifecycle/guided_users.py", "lifecycle/create-user.sh"):
            shutil.copy2(Path("/source/deploy") / name, fixture.root / "deploy" / name)
        (fixture.state / "fault-mode").write_text("none")
    else:
        fixture = base.Fixture(args.workspace, args.subnet, args.image_source_project)
    wrapper = fixture.workspace / "bin/docker"
    wrapper.write_text(base.WRAPPER.replace("__STATE__", repr(str(fixture.state)))
        .replace("__ROOT__", repr(str(fixture.root))).replace("__REAL__", repr(base.REAL))
        .replace("sys.exit(subprocess.call", FAULT + "\nsys.exit(subprocess.call"))
    wrapper.chmod(0o755)
    checks = {}
    proof = {"status": "RUNNING", "project": fixture.project, "checks": checks, "synthetic_only": True,
             "no_host_ports_or_model_provider_calls": True, "containers_volumes_retained": True,
             "API_image_ID": API}
    started = time.monotonic()
    try:
        if args.resume_failed_fixture:
            fixture.compose("up", "-d", "--no-recreate", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "240")
            owner = json.loads((fixture.state / "owner.json").read_text())
            proof["images"] = previous["images"]
            fixture.native("verify")
            fixture.companion("verify")
        else:
            owner = {"name": "Synthetic guided owner", "username": "owner@example.invalid",
                     "password": secrets.token_urlsafe(24)}
            private(fixture.state / "owner.json", owner)
            proof["images"] = fixture.legacy_existing()
            fixture.native("seed")
            fixture.companion("seed")
        fixture.healthy()
        before = snapshot(fixture)
        protected_before = protected(fixture)
        assert len(protected_before) == 5
        api_hash = fixture.run([base.REAL, "exec", fixture.current()["assistant-api"]["Id"], "python", "-B", "-c",
            "import hashlib;from pathlib import Path;print(hashlib.sha256(Path('/app/assistant/primary_binding.py').read_bytes()).hexdigest())"]).decode().strip()
        assert api_hash == "3562acf88b8c24d892cbb861831481cfff2a75d9ef770ef3024258f27c784e24"
        proof["API_primary_binding_source_SHA256"] = api_hash
        environment = (fixture.root / "deploy/.env").read_bytes()
        initial = native_rows(fixture)
        assert len(initial["users"]) == (2 if args.resume_failed_fixture else 1)
        owner_id = (json.loads((fixture.state / "ai-primary.json").read_text())["userId"]
                    if args.resume_failed_fixture else initial["users"][0][0])
        collection = next(x[1] for x in initial["memberships"] if x[0] == owner_id and x[2] == "owner")
        primary_receipt = fixture.state / "ai-primary.json"
        receipt_before = primary_receipt.read_bytes() if primary_receipt.exists() else None
        status = fixture.cli("users", "ai-primary")
        assert b"CURRENT" in status and b"does NOT prove ORIGINAL" in status
        assert (primary_receipt.read_bytes() if primary_receipt.exists() else None) == receipt_before
        checks["readonly_unique_current_candidate_live_WAL_native_UID_no_auto_binding_global_yes_not_attestation"] = True
        fixture.cli("users", "ai-primary", "--confirm-original-user-id", owner_id)
        fixture.cli("users", "ai-primary", "--confirm-original-user-id", owner_id)
        assert json.loads((fixture.state / "ai-primary.json").read_text())["status"] == "complete"
        checks["explicit_original_UUID_pins_once_repeats_idempotently_API_show_verified"] = True
        for name, mode in (("member", "none"), ("lost", "register-lost-reply"), ("uncertain", "register-not-sent")):
            if not (fixture.state / (name + ".json")).exists():
                private(fixture.state / (name + ".json"), {"name": "Synthetic " + name,
                    "username": name + "@example.invalid", "password": secrets.token_urlsafe(24)})
            command = ("users", "create", "--credentials-file", "owner.json", "--new-user-file",
                       name + ".json", "--collection", collection)
            fixture.cli(*command, mode=mode, expected=1 if name == "uncertain" else 0)
            fixture.cli(*command, expected=1 if name == "uncertain" else 0)
        rows = native_rows(fixture)
        assert len(rows["users"]) == 3
        assert rows["wal_nonempty"]
        assert all(not x[2] for x in rows["users"])
        assert all(x[2] == "user" for x in rows["memberships"] if x[0] != owner_id)
        checks["file_automation_NEW_ordinary_members_closed_registration_JSON_invite_exact_household"] = True
        checks["actual_lost_native_commit_reply_reconciles_no_duplicate_and_uncertain_not_delivered_never_retries"] = True
        if args.resume_failed_fixture:
            checks["original_no_body_signup_committed_pending_receipt_recovered_via_login_membership_without_duplicate"] = True
        member_id = next(x[0] for x in rows["users"] if x[0] != owner_id)
        fixture.cli("users", "ai-primary", "--confirm-original-user-id", member_id, expected=1)
        checks["new_ordinary_member_cannot_replace_pinned_original_UUID"] = True
        member_password = secrets.token_urlsafe(24)
        transcript = tty(fixture, [("Email: ", owner["username"]), ("Password: ", owner["password"]),
            ("Household number: ", "1"), ("Display name: ", "Synthetic TTY member"),
            ("Email: ", "tty@example.invalid"), ("Password: ", member_password),
            ("Repeat password: ", member_password)], 0)
        assert owner["password"].encode() not in transcript and member_password.encode() not in transcript
        assert b"Repeat password:" in transcript and b"Choose an existing household owned" in transcript
        mismatch = tty(fixture, [("Email: ", owner["username"]), ("Password: ", owner["password"]),
            ("Household number: ", "1"), ("Display name: ", "Synthetic mismatch"),
            ("Email: ", "mismatch@example.invalid"), ("Password: ", "SYNTHETIC_FIRST_PASSWORD"),
            ("Repeat password: ", "SYNTHETIC_DIFFERENT_PASSWORD")], 1)
        assert b"Passwords differ" in mismatch and b"SYNTHETIC_FIRST_PASSWORD" not in mismatch
        assert len(native_rows(fixture)["users"]) == 4
        checks["real_Docker_private_PTY_owner_login_owned_household_picker_hidden_double_password_and_mismatch_no_signup"] = True
        fixture.native("verify")
        fixture.companion("verify")
        fixture.healthy()
        assert snapshot(fixture) == before
        assert protected(fixture) == protected_before
        assert (fixture.root / "deploy/.env").read_bytes() == environment
        assert json.loads((fixture.state / "ai-primary.json").read_text())["userId"] == owner_id
        checks["all6_CIDs_startedAt_images_full_config_HostConfig_mounts_networkIDs_registration_env_primary_inventory_sessions_preserved"] = True
        checks["core_secret_and_CA_bytes_modes_UID_GID_preserved_in_same_Docker_namespace"] = True
        names = ("homebox", "lifecycle/primary.py", "lifecycle/primary.sh", "lifecycle/accounts.py",
                 "lifecycle/guided_users.py", "lifecycle/create-user.sh")
        proof["deployment_helper_SHA256"] = {name: hashlib.sha256((fixture.root / "deploy" / name).read_bytes()).hexdigest()
                                            for name in names}
        proof["integration_driver_SHA256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        proof["public_source_manifest_SHA256"] = hashlib.sha256(Path("/source/.lifecycle-public-source.json").read_bytes()).hexdigest()
        proof["status"] = "PASS"
    finally:
        running = [x["Id"] for x in fixture.current().values() if x["State"].get("Running")]
        if running:
            fixture.run([base.REAL, "stop", "--timeout", "-1", *running])
        proof["elapsed_seconds"] = round(time.monotonic() - started, 3)
        if proof["status"] != "PASS":
            proof["status"] = "FAIL"
        output = fixture.workspace / ("proof-resumed.json" if args.resume_failed_fixture else "proof.json")
        output.write_text(json.dumps(proof, indent=2) + "\n")
        print(json.dumps({"status": proof["status"], "proof": str(output)}))


if __name__ == "__main__":
    main()
