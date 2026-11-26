"""Owned synthetic Docker transition; no host ports or actual Tailnet changes."""
import argparse
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
import uuid

PYTHON = "python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df"
REAL = shutil.which("docker")
TAIL_IP = "100.90.231.7"


def image_sources(project, run):
    if not re.fullmatch(r"homebox-lifecycle-test-[a-z0-9_-]+", project):
        raise ValueError("Image source must be an explicitly selected synthetic test project.")
    ids = run([REAL, "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project]).decode().split()
    if len(ids) != 6:
        raise ValueError("Explicit image-source project must contain exactly six application containers.")
    rows = json.loads(run([REAL, "inspect", *ids]))
    images = {x["Config"]["Labels"]["com.docker.compose.service"]: x["Image"] for x in rows}
    if set(images) != {"homebox", "homebox-mcp", "assistant-api", "pwa", "proxy", "ollama"}:
        raise ValueError("Explicit image-source project does not have the six expected services.")
    return images

WRAPPER = r'''#!/usr/local/bin/python
import os,signal,subprocess,sys
from pathlib import Path
args=sys.argv[1:]
state=Path(__STATE__)
mode=(state/'fault-mode').read_text().strip()
if mode=='kill-after-proxy' and args[-1:] == ['access-verify']:
 parent=os.getppid();lock=Path(__ROOT__)/'deploy/.homebox-operation.lock'
 assert (lock/'pid').read_text().strip()==str(parent)
 (state/'fault-sigkill-parent').write_text(str(parent))
 # Fault injector clears only its own known lock, modeling its reviewed removal
 # after SIGKILL. The production command never clears a stale lock itself.
 (lock/'pid').unlink();lock.rmdir()
 os.kill(parent,signal.SIGKILL);sys.exit(86)
if mode=='after-api' and args[-1:] == ['access-phase'] and 'LC_ACCESS_PHASE=configuring-proxy' in args:
 (state/'fault-after-api').write_text('injected before proxy reconfiguration\n');sys.exit(86)
if mode=='env-before-journal' and args[-1:] == ['access-finish']:
 code="""import sys
sys.path.insert(0,'/source/deploy/lifecycle')
import access,metadata
original=access.stage_file
def fail_after_env(path,body,info):
 original(path,body,info)
 if path==metadata.DEPLOY/'.env':raise OSError('Synthetic env-committed-before-journal fault')
access.stage_file=fail_after_env
access.dispatch('access-finish',metadata)
"""
 args=args[:-4]+['python','-B','-c',code]
 (state/'fault-env-before-journal').write_text('injected only in controller Docker command\n')
sys.exit(subprocess.call([__REAL__,*args]))
'''

COMPANION = r'''
import http.cookiejar,json,socket,ssl,urllib.request
from pathlib import Path
s=Path('/state');mode=MODE;ip=IP
old=socket.getaddrinfo
socket.getaddrinfo=lambda host,port,*a,**kw:old('127.0.0.1',port,*a,**kw)
context=ssl.create_default_context(cafile=str(s/'work/public-ca.crt'))
jar=http.cookiejar.LWPCookieJar(str(s/'companion.cookies'))
if mode!='seed':jar.load(ignore_discard=True)
client=urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPSHandler(context=context),
                                 urllib.request.HTTPCookieProcessor(jar))
base='https://inventory.localhost/api/assistant/v1'
def get(url):return json.load(client.open(url,timeout=20))
def login(origin):
 c=json.loads((s/'owner.json').read_text())
 request=urllib.request.Request(origin+'/api/assistant/v1/auth/login',
  data=json.dumps({k:c[k] for k in ('username','password')}).encode(),
  headers={'Origin':origin,'Content-Type':'application/json'})
 return json.load(client.open(request,timeout=20))
if mode=='seed':
 value=login('https://inventory.localhost');jar.save(ignore_discard=True)
 (s/'companion-expected.json').write_text(json.dumps({
  'user':value['user']['id'],'collection':value['collectionId'],'instance':value['scope']['instanceId']}))
else:
 expected=json.loads((s/'companion-expected.json').read_text())
 value=get(base+'/session')
 assert (value['user']['id'],value['collectionId'],value['scope']['instanceId'])==tuple(expected.values())
 if mode=='tailnet':
  value=login('https://'+ip)
  value=get('https://'+ip+'/api/assistant/v1/session')
  assert (value['user']['id'],value['collectionId'],value['scope']['instanceId'])==tuple(expected.values())
print('PASS: old companion session and selected native collection preserved; new IP login checked when selected')
'''


class Fixture:
    def __init__(self, workspace, subnet, image_source_project):
        assert workspace.is_absolute() and not workspace.exists()
        assert Path("/source/.lifecycle-public-source.json").is_file()
        assert not Path("/source/deploy/.env").exists()
        ids = subprocess.check_output([REAL, "network", "ls", "-q"]).decode().split()
        for n in json.loads(subprocess.check_output([REAL, "network", "inspect", *ids])) if ids else []:
            for entry in n.get("IPAM", {}).get("Config") or []:
                if entry.get("Subnet"):
                    other = ipaddress.ip_network(entry["Subnet"])
                    assert other.version != subnet.version or not other.overlaps(subnet)
        workspace.mkdir(mode=0o700)
        self.workspace, self.root, self.state = workspace, workspace / "checkout", workspace / "private"
        self.image_source_project = image_source_project
        self.state.mkdir(mode=0o700)
        for name in ("backend", "frontend", "apps", "services", "deploy"):
            shutil.copytree(Path("/source") / name, self.root / name,
                            ignore=shutil.ignore_patterns(".env*", "secrets", "backups", "__pycache__", "node_modules"))
        shutil.copy2("/source/deploy/.env.example", self.root / "deploy/.env.example")
        self.project = "homebox-lifecycle-test-access-" + uuid.uuid4().hex[:8]
        self.override = workspace / "isolated.yml"
        lines = ["services:", "  proxy:", "    ports: !reset []"]
        for name in ("proxy", "homebox", "homebox-mcp", "assistant-api", "pwa", "ollama"):
            if name != "proxy":
                lines.append("  " + name + ":")
            lines += ["    healthcheck:", "      interval: 1s", "      start_period: 1s", "      retries: 30"]
        lines += ["networks:"]
        for name, cidr in zip(("edge", "browser", "homebox_ui", "inventory", "tools", "inference"),
                              subnet.subnets(new_prefix=28)):
            lines += ["  " + name + ":", "    internal: true", "    ipam:", "      config:", "        - subnet: " + str(cidr)]
        self.override.write_text("\n".join(lines) + "\n")
        binary = workspace / "bin"
        binary.mkdir()
        (binary / "docker").write_text(WRAPPER.replace("__STATE__", repr(str(self.state)))
                                      .replace("__ROOT__", repr(str(self.root))).replace("__REAL__", repr(REAL)))
        (binary / "docker").chmod(0o755)
        (binary / "tailscale").write_text(
            "#!/bin/sh\n[ \"$*\" = 'ip -4' ] || exit 2\n"
            "[ \"$(cat " + str(self.state / "fault-mode") + ")\" != tailscale-unavailable ] || exit 127\n"
            "printf '%s\\n' " + TAIL_IP + "\n")
        (binary / "tailscale").chmod(0o755)
        self.env = {**os.environ, "PATH": str(binary) + ":" + os.environ["PATH"],
                    "HOMEBOX_LIFECYCLE_TEST_OVERRIDE": str(self.override)}
        self.base = ["sh", str(self.root / "deploy/homebox"), "--project", self.project,
                     "--state-dir", str(self.state), "--yes"]
        self.sequence = 0

    def run(self, args, expected=0, env=None):
        self.sequence += 1
        result = subprocess.run(args, capture_output=True, env=env, timeout=1200)
        path = self.state / ("command-" + str(self.sequence) + ".log")
        path.write_bytes(result.stdout + result.stderr)
        path.chmod(0o600)
        assert (result.returncode == 0) == (expected == 0), "Unexpected status; private log " + str(path)
        return result.stdout + result.stderr

    def cli(self, *args, mode="none", expected=0):
        (self.state / "fault-mode").write_text(mode)
        return self.run([*self.base, *args], expected, self.env)

    def compose(self, *args):
        return self.run([REAL, "compose", "-p", self.project, "--env-file", str(self.root / "deploy/.env"),
                         "-f", str(self.root / "deploy/docker-compose.yml"), "-f", str(self.override), *args])

    def legacy_existing(self):
        """Start six cached exact images, then use the published explicit adopt."""
        images = image_sources(self.image_source_project, self.run)
        version = "access-existing-" + uuid.uuid4().hex
        for service, prefix in {"homebox": "homebox-inventory", "homebox-mcp": "homebox-inventory-mcp",
                                "assistant-api": "homebox-inventory-assistant", "pwa": "homebox-inventory-pwa"}.items():
            reference = prefix + ":" + version
            self.run([REAL, "tag", images[service], reference])
        env = (self.root / "deploy/.env.example").read_text()
        for name, value in {"STACK_VERSION": version, "HOMEBOX_HOST": "homebox.localhost",
                            "ASSISTANT_HOST": "inventory.localhost", "HBOX_ALLOW_REGISTRATION": "true",
                            "HOMEBOX_INSTANCE_ID": str(uuid.uuid4()), "PROXY_BIND_IP": "127.0.0.1",
                            "PROXY_LAN_IP": ""}.items():
            env = re.sub(r"(?m)^" + name + "=.*$", name + "=" + value, env)
        path = self.root / "deploy/.env"
        path.write_text(env)
        path.chmod(0o600)
        keys = self.root / "deploy/secrets"
        keys.mkdir(mode=0o755)
        for name in ("homebox_api_pepper", "assistant_session_secret", "mcp_shared_secret"):
            (keys / name).write_text(secrets.token_urlsafe(32))
            (keys / name).chmod(0o444)
        self.compose("up", "-d", "--no-build", "--pull", "never", "--wait", "--wait-timeout", "240")
        self.run([REAL, "run", "--rm", "--network", "container:" + self.current()["homebox"]["Id"],
                  "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                  "--mount", "type=bind,source=" + str(self.root) + ",target=/source,readonly",
                  "--mount", "type=bind,source=" + str(self.state) + ",target=/state", PYTHON,
                  "python", "-B", "/source/deploy/lifecycle/accounts.py", "bootstrap",
                  "--credentials-file", "owner.json"])
        path.write_text(env.replace("HBOX_ALLOW_REGISTRATION=true", "HBOX_ALLOW_REGISTRATION=false"))
        self.compose("up", "-d", "--no-deps", "--no-build", "--pull", "never",
                     "--wait", "--wait-timeout", "180", "homebox")
        self.cli("adopt")
        (self.state / "work").mkdir(mode=0o700, exist_ok=True)
        self.run([REAL, "cp", self.current()["proxy"]["Id"] + ":/data/caddy/pki/authorities/local/root.crt",
                  str(self.state / "work/public-ca.crt")])
        return images

    def current(self):
        ids = self.run([REAL, "ps", "-aq", "--filter", "label=com.docker.compose.project=" + self.project]).decode().split()
        return {x["Config"]["Labels"]["com.docker.compose.service"]: x
                for x in json.loads(self.run([REAL, "inspect", *ids]))} if ids else {}

    def journal(self):
        return json.loads((self.state / "installation.json").read_text())

    def native(self, mode):
        return self.run([REAL, "run", "--rm", "--network", "container:" + self.current()["homebox"]["Id"],
                         "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                         "--mount", "type=bind,source=" + str(self.root) + ",target=/source,readonly",
                         "--mount", "type=bind,source=" + str(self.state) + ",target=/state",
                         PYTHON, "python", "-B", "/source/deploy/tests/lifecycle_integration.py", "--native", mode])

    def companion(self, mode):
        code = COMPANION.replace("mode=MODE;ip=IP", "mode=" + repr(mode) + ";ip=" + repr(TAIL_IP))
        return self.run([REAL, "run", "--rm", "--network", "container:" + self.current()["proxy"]["Id"],
                         "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                         "--mount", "type=bind,source=" + str(self.state) + ",target=/state",
                         PYTHON, "python", "-B", "-c", code])

    def healthy(self):
        value = self.current()
        assert len(value) == 6 and all(x["State"].get("Health", {}).get("Status") == "healthy" for x in value.values())
        return value

    def preserved(self, before, allow_proxy_stopped=False):
        after = self.current()
        assert set(after) == set(before)
        for name in after:
            assert after[name]["Image"] == before[name]["Image"]
            assert not after[name]["HostConfig"].get("PortBindings"), "Fixture must never publish host ports"
            if name not in ("proxy", "assistant-api"):
                assert after[name]["Id"] == before[name]["Id"]
                assert after[name]["State"]["StartedAt"] == before[name]["State"]["StartedAt"]
                assert after[name]["Config"]["Env"] == before[name]["Config"]["Env"]
        if not allow_proxy_stopped:
            self.healthy()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--image-source-project", required=True,
                   help="Existing synthetic six-container project supplying cached exact image IDs")
    p.add_argument("--image-preflight-only", action="store_true", help="Only read the selected six image IDs")
    p.add_argument("--workspace", type=Path)
    p.add_argument("--subnet", type=ipaddress.ip_network)
    args = p.parse_args()
    if args.image_preflight_only:
        print(json.dumps({"status": "PASS", "read_only": True, "image_source_project": args.image_source_project,
                          "image_IDs": image_sources(args.image_source_project, subprocess.check_output)}))
        return
    if args.workspace is None or args.subnet is None:
        p.error("--workspace and --subnet are required for the transition test")
    fixture = Fixture(args.workspace, args.subnet, args.image_source_project)
    proof = {"status": "RUNNING", "project": fixture.project, "checks": {}, "synthetic_only": True,
             "no_host_ports_or_actual_Tailnet_changes": True, "all_app_containers_volumes_retained": True,
             "Tailnet_assignment": "Explicit fake tailscale CLI in fixture PATH only; production has no bypass",
             "namespace_TLS_Origin_is_not_actual_Tailnet_routing": True}
    started = time.monotonic()
    try:
        owner = fixture.state / "owner.json"
        owner.write_text(json.dumps({"name": "Synthetic access owner", "username": "owner@example.invalid",
                                     "password": secrets.token_urlsafe(24)}))
        owner.chmod(0o600)
        proof["baseline"] = {
            "creation": "Six cached production images started with published Compose, explicit published adopt; no C6 lifecycle code, no build, no model or image download",
            "image_source_project": args.image_source_project,
            "image_IDs": fixture.legacy_existing(),
            "runtime_API_source": "Published production runtime; only two test files differ in d594. No API test-suite rerun claimed."}
        fixture.native("seed")
        fixture.companion("seed")
        original = fixture.healthy()
        before = fixture.journal()
        env = (fixture.root / "deploy/.env").read_bytes()
        protected = {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode, p.stat().st_uid, p.stat().st_gid)
                     for p in (fixture.root / "deploy/secrets").rglob("*") if p.is_file()}
        fixture.cli("access", "tailscale", "--ip", TAIL_IP, mode="after-api", expected=1)
        assert (fixture.state / "fault-after-api").exists()
        assert fixture.journal()["access_change"]["status"] == "pending"
        assert not fixture.current()["proxy"]["State"]["Running"]
        assert (fixture.root / "deploy/.env").read_bytes() == env
        fixture.preserved(original, True)
        assert b"access resume" in fixture.cli("redeploy", expected=1)
        proof["checks"]["after_API_fault_closes_ingress_durable_pending_blocks_normal_maintenance"] = True
        (fixture.state / "work/tailscale-ip.txt").write_text("100.90.231.8\n")
        fixture.cli("access", "rollback", mode="tailscale-unavailable")
        fixture.preserved(original)
        assert (fixture.root / "deploy/.env").read_bytes() == env
        assert fixture.journal()["overlays"] == before["overlays"]
        fixture.native("verify")
        fixture.companion("verify")
        proof["checks"]["explicit_configuration_only_rollback_preserves_all6images_4unaffected_IDs_data_sessions"] = True
        fixture.cli("access", "tailscale", "--ip", TAIL_IP, mode="kill-after-proxy", expected=1)
        assert (fixture.state / "fault-sigkill-parent").exists()
        assert fixture.current()["proxy"]["State"]["Running"]
        fixture.cli("access", "resume", mode="tailscale-unavailable", expected=1)
        assert not fixture.current()["proxy"]["State"]["Running"]
        assert fixture.journal()["access_change"]["status"] == "pending"
        proof["checks"]["actual_SIGKILL_after_detached_proxy_start_then_rejected_missing_CLI_recovery_closes_authenticated_proxy"] = True
        proof["reviewed_synthetic_stale_lock"] = "Fault injector removes only its own exact PID lock immediately before killing that shell. Production never removes stale locks automatically."
        fixture.cli("access", "resume", mode="env-before-journal", expected=1)
        assert (fixture.state / "fault-env-before-journal").exists()
        assert fixture.journal()["access_change"]["status"] == "pending"
        assert not fixture.current()["proxy"]["State"]["Running"]
        assert ("PROXY_LAN_IP=" + TAIL_IP).encode() in (fixture.root / "deploy/.env").read_bytes()
        proof["checks"]["actual_env_installed_before_journal_commit_fault_remains_pending_closed_and_resumable"] = True
        fixture.cli("access", "resume")
        fixture.preserved(original)
        state = fixture.journal()
        assert state["phase"] == "ready" and state["access_change"]["status"] == "complete"
        assert state["overlays"][1]
        assert state["source_hashes"] == before["source_hashes"] and state["active_images"] == before["active_images"]
        assert fixture.journal()["resources"]["volumes"] == before["resources"]["volumes"]
        assert fixture.journal()["resources"]["secrets"] == before["resources"]["secrets"]
        assert fixture.journal()["resources"]["instance_id"] == before["resources"]["instance_id"]
        assert {p.name: (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mode, p.stat().st_uid, p.stat().st_gid)
                for p in (fixture.root / "deploy/secrets").rglob("*") if p.is_file()} == protected
        fixture.native("verify")
        fixture.companion("tailnet")
        root = fixture.state / "access" / state["access_change"]["id"]
        assert json.loads((root / "origin-proof.json").read_text())["IP_SAN"] == TAIL_IP
        ids = {n: x["Id"] for n, x in fixture.current().items()}
        fixture.cli("access", "tailscale", "--ip", TAIL_IP)
        assert {n: x["Id"] for n, x in fixture.current().items()} == ids
        fixture.cli("status")
        proof["checks"].update({
            "real_API_proxy_transition_Tailnet100_IP_SAN_CA_verified_TLS_and_exact_Origin_matrix": True,
            "old_native_and_companion_sessions_new_IP_login_qty7_location_parent_exact_attachment": True,
            "all6_exact_imageIDs_4unaffected_IDs_start_env_volumes_UUID_corekeys_CA_source_receipts_preserved": True,
            "committed_access_repeats_as_noop_and_ordinary_status_accepts_new_overlay_receipt": True})
        proof["status"] = "PASS"
    except Exception:
        proof["status"] = "FAIL"
        raise
    finally:
        current = fixture.current()
        running = [x["Id"] for x in current.values() if x["State"].get("Running")]
        if running:
            fixture.run([REAL, "stop", "--timeout", "-1", *running])
        proof["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (fixture.workspace / "proof.json").write_text(json.dumps(proof, indent=2) + "\n")
        print(json.dumps({"status": proof["status"], "proof": str(fixture.workspace / "proof.json")}))


if __name__ == "__main__":
    main()
