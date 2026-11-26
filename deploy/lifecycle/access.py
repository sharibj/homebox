"""Explicit, image-preserving configuration maintenance for Tailnet access."""
import copy
import ipaddress
import json
import os
from pathlib import Path
import stat
import uuid

TAILNET = ipaddress.ip_network("100.64.0.0/10")
CHANGED = {"assistant-api", "proxy"}
SOURCE_FILES = (
    "deploy/homebox", "deploy/lifecycle/metadata.py", "deploy/lifecycle/access.py",
    "deploy/lifecycle/access.sh",
    "deploy/docker-compose.yml", "deploy/docker-compose.external-ai.yml",
    "deploy/docker-compose.lan.yml", "deploy/reverse-proxy/Caddyfile",
    "deploy/reverse-proxy/Caddyfile.lan", "deploy/tests/check_lan_access.py",
)


def address(value):
    ip = ipaddress.ip_address(value)
    if ip.version != 4 or ip not in TAILNET:
        raise ValueError("Use the locally assigned Tailscale IPv4 in 100.64.0.0/10.")
    return str(ip)


def sync_parent(path):
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def save(m, path, value):
    m.save(path, value)
    sync_parent(path)


def stage_file(path, body, info):
    if path.is_symlink():
        raise ValueError("Refusing symlink access configuration.")
    temporary = path.with_name(path.name + ".access-" + uuid.uuid4().hex)
    with temporary.open("xb") as output:
        os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
        os.fchown(output.fileno(), info.st_uid, info.st_gid)
        output.write(body.encode())
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    sync_parent(path)


def pending(m):
    return m.journal().get("access_change", {}).get("status") == "pending"


def directory(m):
    value = m.journal().get("access_change", {})
    identifier = value.get("id", "")
    if len(identifier) != 32 or any(c not in "0123456789abcdef" for c in identifier):
        raise ValueError("Missing trusted private access operation.")
    root = m.STATE / "access" / identifier
    for path in (root.parent, root):
        if path.is_symlink() or (path.exists() and (
                not path.is_dir() or path.stat().st_uid != os.getuid()
                or stat.S_IMODE(path.stat().st_mode) & 0o077)):
            raise ValueError("Access operation directories must remain owned and private.")
    return root


def plan(m):
    path = directory(m) / "plan.json"
    if path.is_symlink() or m.file_digest(path) != m.journal()["access_change"].get("sha256"):
        raise ValueError("Private access plan identity changed.")
    return m.load(path)


def phase(m, value, direction=None):
    state = dict(m.journal()["access_change"])
    state["phase"] = value
    if direction:
        state["direction"] = direction
    if value in ("configuring-api", "configuring-proxy"):
        state["api_recreation_started" if value == "configuring-api" else "proxy_recreation_started"] = True
    save(m, m.STATE / "installation.json", {
        **m.journal(), "phase": "access-" + value, "access_change": state})


def host_path(m, path):
    return str(Path(m.selected()["state"]) / path.relative_to(m.STATE))


def sources(m):
    result = {}
    for name in SOURCE_FILES:
        path = m.SOURCE / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("Access source must remain a regular file: " + name)
        result[name] = m.file_digest(path)
    if os.environ.get("LC_TEST_OVERRIDE"):
        result["selected-test-override"] = m.file_digest(m.TEST_COMPOSE_OVERRIDE)
    return result


def mounts(item):
    rows = item["Mounts"]
    result = {row["Destination"]: row for row in rows}
    if len(rows) != len(result):
        raise ValueError("Duplicate runtime mount destination.")
    return result


def network_ids(item):
    return {name: value["NetworkID"] for name, value in item["NetworkSettings"]["Networks"].items()}


def public_ports(service):
    result = {}
    for port in service.get("ports") or []:
        key = str(port["target"]) + "/" + port.get("protocol", "tcp")
        result.setdefault(key, []).append({
            "HostIp": port.get("host_ip", ""), "HostPort": str(port["published"])})
    return {key: sorted(rows, key=lambda row: (row["HostIp"], row["HostPort"]))
            for key, rows in result.items()}


def secret_tree(m):
    result = {}
    for path in sorted((m.DEPLOY / "secrets").rglob("*")):
        info = path.lstat()
        value = [stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid]
        if path.is_symlink():
            value.append(os.readlink(path))
        elif path.is_file():
            value.append(m.file_digest(path))
        result[str(path.relative_to(m.DEPLOY))] = value
    return result


def base_context(m):
    owner, journal = m.installation_owner(), m.journal()
    if not owner or not owner.get("established") or owner.get("binding") != m.selected():
        raise ValueError("Access requires this completed installation's established canonical owner.")
    if journal.get("binding") != m.selected() or not journal.get("resources"):
        raise ValueError("Access requires the original canonical resource receipt.")
    if journal.get("registration_restore") is not None or journal.get("model_download_pending"):
        raise ValueError("Finish registration/model recovery before changing access.")
    return journal


def prepare(m):
    state = base_context(m)
    if pending(m):
        raise ValueError("Access operation is pending; use access resume or access rollback.")
    if state.get("phase") != "ready":
        raise ValueError("Finish the original deployment first; all healthy is not a completed journal.")
    current = m.containers()
    if set(current) != set(m.SERVICES) or any(
            not item["State"].get("Running")
            or item["State"].get("Health", {}).get("Status") != "healthy"
            for item in current.values()):
        raise ValueError("Access requires all six original healthy services.")
    m.check_resources()
    m.validate_config()  # OLD protected configuration, without any exception.
    ip = address(os.environ["LC_ACCESS_IP"])
    assigned = (m.WORK / "tailscale-ip.txt").read_text().split()
    if assigned != [ip]:
        raise ValueError("Requested address differs from locally assigned Tailscale IPv4.")
    body = m.read_env()
    env = m.environment(body)
    info = (m.DEPLOY / ".env").stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("The protected environment must be owned and private.")
    if env.get("PROXY_BIND_IP", "127.0.0.1") != "127.0.0.1" or env.get("TLS_ISSUER", "internal") != "internal":
        raise ValueError("This transition preserves loopback listeners and requires internal TLS.")
    if set(m.config()["services"]) - {"backup"} != set(m.SERVICES):
        raise ValueError("Access requires the six-service published configuration.")
    if not os.environ.get("LC_TEST_OVERRIDE") and public_ports(m.config()["services"]["proxy"]) != {
            "80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "80"}],
            "443/tcp": [{"HostIp": "127.0.0.1", "HostPort": "443"}]} and not env.get("PROXY_LAN_IP"):
        raise ValueError("Access only adds to the original loopback 80/443 listeners.")
    if env.get("PROXY_LAN_IP"):
        if env["PROXY_LAN_IP"] == ip and state["overlays"][1]:
            (m.WORK / "access-noop").write_text("1\n")
            print("Tailnet access is already configured: https://" + ip + " (native :8443)")
            return
        raise ValueError("Existing LAN access differs; this command only adds access to a loopback installation.")
    if state["overlays"][1]:
        raise ValueError("Existing LAN overlay is not a loopback-only installation.")
    identifier = uuid.uuid4().hex
    root = m.STATE / "access" / identifier
    root.parent.mkdir(mode=0o700, exist_ok=True)
    if root.parent.is_symlink() or root.parent.stat().st_uid != os.getuid() or stat.S_IMODE(root.parent.stat().st_mode) & 0o077:
        raise ValueError("Access operation directory must be owned and private.")
    root.mkdir(mode=0o700)
    version = "access-" + identifier
    candidate = m.replace_setting(m.replace_setting(body, "PROXY_LAN_IP", ip), "STACK_VERSION", version)
    stage_file(root / "before.env", body, info)
    stage_file(root / "after.env", candidate, info)
    spec = {
        "id": identifier, "binding": m.selected(), "ip": ip, "version": version,
        "sources": sources(m), "before": current, "config": m.config(),
        "resources": state["resources"], "owner": m.installation_owner(),
        "overlays": state["overlays"], "before_env": m.digest(body.encode()),
        "after_env": m.digest(candidate.encode()), "env_metadata": [
            stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid],
        "ca": m.load(m.WORK / "access-ca.json"),
        "secrets": secret_tree(m),
    }
    if not spec["ca"] or set(spec["ca"]) != {"root.crt", "root.key"}:
        raise ValueError("Cannot checkpoint the existing CA certificate and private-key identity.")
    save(m, root / "plan.json", spec)
    save(m, m.STATE / "installation.json", {
        **state, "phase": "access-prepared",
        "access_change": {"id": identifier, "status": "pending", "phase": "prepared", "direction": "forward",
                          "sha256": m.file_digest(root / "plan.json")}})
    (m.WORK / "access-noop").write_text("0\n")
    instructions(m)


def instructions(m):
    spec = plan(m)
    rollback = m.journal()["access_change"].get("direction") == "rollback"
    (m.WORK / "external").write_text("1\n" if spec["overlays"][0] else "0\n")
    (m.WORK / "lan").write_text(("1" if spec["overlays"][1] else "0") + "\n" if rollback else "1\n")
    (m.WORK / "access-env").write_text(host_path(m, directory(m) / ("before.env" if rollback else "after.env")) + "\n")
    aliases = []
    for name in m.APPS:
        alias = m.PREFIXES[name] + ":" + spec["version"]
        aliases.append(spec["before"][name]["Image"] + "|" + alias)
    (m.WORK / "access-aliases").write_text("\n".join(aliases) + "\n")
    # Rollback uses the old references as well; their IDs must still resolve
    # exactly. New aliases are never a reason to overwrite an old mutable tag.
    (m.WORK / "access-target-images").write_text("\n".join(
        spec["before"][name]["Image"] + "|" + (
            spec["config"]["services"][name]["image"] if rollback else m.PREFIXES[name] + ":" + spec["version"])
        for name in m.APPS) + "\n")


def canonical_host(value):
    result = copy.deepcopy(value)
    if result.get("Binds") is not None:
        result["Binds"] = sorted(result["Binds"])
    if result.get("Mounts") is not None:
        result["Mounts"] = sorted(result["Mounts"], key=lambda v: json.dumps(v, sort_keys=True))
    if result.get("PortBindings"):
        result["PortBindings"] = {k: sorted(v, key=lambda row: (row["HostIp"], row["HostPort"]))
                                  for k, v in result["PortBindings"].items()}
    return result


def runtime_matches(m, name, item, old, cfg):
    service = cfg["services"][name]
    wanted = dict(entry.split("=", 1) for entry in old["Config"]["Env"])
    wanted.update({key: str(value) for key, value in service.get("environment", {}).items()})
    if dict(entry.split("=", 1) for entry in item["Config"]["Env"]) != wanted:
        return False
    expected_host = copy.deepcopy(old["HostConfig"])
    expected_mounts = copy.deepcopy(mounts(old))
    if name == "proxy":
        expected_host["PortBindings"] = public_ports(service) or old["HostConfig"].get("PortBindings")
        targets = ("/etc/caddy/Caddyfile", "/etc/caddy/Caddyfile.base")
        bindings = {v["target"]: v for v in service["volumes"]}
        actual_mounts = mounts(item)
        template = mounts(old)["/etc/caddy/Caddyfile"]
        for target in targets:
            if target not in bindings:
                expected_mounts.pop(target, None)
                continue
            value = actual_mounts.get(target)
            definition = bindings[target]
            if (not value or definition["type"] != "bind" or not definition.get("read_only")
                    or not m.bind_source_matches(value["Source"], definition["source"])):
                return False
            expected_mounts[target] = {**template, "Source": value["Source"], "Destination": target}
        if expected_host.get("Binds") is not None:
            original = expected_host["Binds"]
            expected_host["Binds"] = [v for v in original if v.rsplit(":", 2)[-2] not in targets]
            for target in targets:
                if target in bindings:
                    # Use the actual source only after the exact published
                    # Desktop /host_mnt alias check above, retaining raw receipts.
                    expected_host["Binds"].append(expected_mounts[target]["Source"] + ":" + target + ":ro")
        if expected_host.get("Mounts") is not None:
            original = expected_host["Mounts"]
            template_host = next((v for v in original if v.get("Target") == targets[0]), None)
            if template_host:
                expected_host["Mounts"] = [v for v in original if v.get("Target") not in targets]
                for target in targets:
                    if target in bindings:
                        actual = next((v for v in item["HostConfig"]["Mounts"] if v.get("Target") == target), None)
                        if not actual or not m.bind_source_matches(actual["Source"], bindings[target]["source"]):
                            return False
                        expected_host["Mounts"].append({
                            **template_host, "Source": actual["Source"], "Target": target})
    return (canonical_host(item["HostConfig"]) == canonical_host(expected_host)
            and mounts(item) == expected_mounts)


def trusted_proxy(m):
    """Authenticate only ownership, before fallible source/resource/address checks."""
    if not pending(m):
        raise ValueError("No pending access operation to close.")
    spec = plan(m)
    binding = m.selected()
    if (m.journal().get("binding") != binding or spec["binding"] != binding
            or spec["owner"].get("binding") != binding or not spec["owner"].get("established")):
        raise ValueError("Cannot authenticate this pending operation's canonical installation.")
    item = m.containers().get("proxy")
    if item is None:
        return ""
    old = spec["before"]["proxy"]
    if item["Id"] == old["Id"]:
        return item["Id"]
    labels = item["Config"].get("Labels") or {}
    expected_files = {str(Path(binding["root"]) / name) for name in (
        "deploy/docker-compose.yml", "deploy/docker-compose.external-ai.yml", "deploy/docker-compose.lan.yml")}
    if spec["overlays"][2]:
        expected_files.add(spec["overlays"][2])
    files = set(labels.get("com.docker.compose.project.config_files", "").split(","))
    if (not m.journal()["access_change"].get("proxy_recreation_started")
            or item["Image"] != old["Image"]
            or labels.get("com.docker.compose.project") != binding["project"]
            or labels.get("com.docker.compose.service") != "proxy"
            or str(Path(binding["root"]) / "deploy/docker-compose.yml") not in files
            or files - expected_files):
        raise ValueError("Refusing to stop an untrusted replacement proxy.")
    configs = [spec["config"], m.load(directory(m) / "forward-config.json", {})]
    if not any(cfg and runtime_matches(m, "proxy", item, old, cfg) for cfg in configs):
        raise ValueError("Refusing to stop an untrusted replacement proxy configuration.")
    return item["Id"]


def guard(m, require_address=True):
    base_context(m)
    if not pending(m):
        raise ValueError("No pending access operation to recover.")
    spec = plan(m)
    if require_address and m.journal()["access_change"].get("direction") != "rollback" and (
            m.WORK / "tailscale-ip.txt").read_text().split() != [spec["ip"]]:
        raise ValueError("The selected Tailscale address is no longer locally assigned.")
    if spec["binding"] != m.selected() or sources(m) != spec["sources"]:
        raise ValueError("Access source/binding changed; restore the reviewed source before recovery.")
    if m.installation_owner() != spec["owner"]:
        raise ValueError("Installation owner changed during access maintenance.")
    if m.volume_snapshot() != spec["resources"]["volumes"]:
        raise ValueError("Access volume identity is missing or changed; no writer may start.")
    current = m.containers()
    if set(current) - set(m.SERVICES) or not (set(m.SERVICES) - CHANGED).issubset(current):
        raise ValueError("An unaffected service is missing; access cannot repair it.")
    change = m.journal()["access_change"]
    for name, flag in (("assistant-api", "api_recreation_started"), ("proxy", "proxy_recreation_started")):
        if name not in current and not change.get(flag):
            raise ValueError("A service disappeared before its planned recreation: " + name)
    for name, item in current.items():
        old = spec["before"][name]
        if item["Image"] != old["Image"]:
            raise ValueError("An exact runtime image ID changed: " + name)
        networks = network_ids(item)
        prior_networks = network_ids(old)
        if networks != prior_networks:
            created = name in CHANGED and item["State"].get("Status") == "created"
            if not created or any(k not in prior_networks or v not in ("", prior_networks[k])
                                  for k, v in networks.items()):
                raise ValueError("Access may not replace network identity: " + name)
        if name not in CHANGED:
            if (item["Id"] != old["Id"] or item["Config"]["Env"] != old["Config"]["Env"]
                    or item["HostConfig"] != old["HostConfig"] or mounts(item) != mounts(old)
                    or item["State"].get("StartedAt") != old["State"].get("StartedAt")
                    or not item["State"].get("Running")
                    or item["State"].get("Health", {}).get("Status") != "healthy"):
                raise ValueError("Unaffected service changed: " + name)
        else:
            flag = "api_recreation_started" if name == "assistant-api" else "proxy_recreation_started"
            if not change.get(flag) and (
                    item["Id"] != old["Id"] or item["State"].get("StartedAt") != old["State"].get("StartedAt")):
                raise ValueError("A service was replaced/restarted before its planned recreation: " + name)
            configurations = [spec["config"]]
            target = m.load(directory(m) / "forward-config.json")
            if target:
                configurations.append(target)
            if not any(runtime_matches(m, name, item, old, cfg) for cfg in configurations):
                raise ValueError("Changed service is neither the witnessed old nor exact staged configuration: " + name)
        # All persistent and secret mounts remain identical, including every field.
        left, right = mounts(old), mounts(item)
        for target, mount in left.items():
            if name == "proxy" and target == "/etc/caddy/Caddyfile":
                continue
            if right.get(target) != mount:
                raise ValueError("Persistent/secret mount changed: " + name + ":" + target)
    if m.secret_fingerprints() != {k: v["sha256"] for k, v in spec["resources"]["secrets"].items()}:
        raise ValueError("Core key changed during access maintenance.")
    for name, value in spec["resources"]["secrets"].items():
        info = (m.DEPLOY / "secrets" / name).stat()
        if [stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid] != [value["mode"], value["uid"], value["gid"]]:
            raise ValueError("Core key metadata changed.")
    if secret_tree(m) != spec["secrets"]:
        raise ValueError("Core/provider secret tree changed.")
    env = m.read_env()
    if m.digest(env.encode()) not in (spec["before_env"], spec["after_env"]):
        raise ValueError("Protected environment changed outside this access operation.")
    info = (m.DEPLOY / ".env").stat()
    if [stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid] != spec["env_metadata"]:
        raise ValueError("Protected environment ownership/mode changed.")
    if m.environment(env).get("HOMEBOX_INSTANCE_ID") != spec["resources"]["instance_id"]:
        raise ValueError("Installation UUID changed.")
    for filename, expected in (("before.env", spec["before_env"]), ("after.env", spec["after_env"])):
        path = directory(m) / filename
        if path.is_symlink() or m.file_digest(path) != expected:
            raise ValueError("Private access environment snapshot changed.")
    return spec


def validate_delta(before, after, ip, version, prefixes, testing=False):
    expected = copy.deepcopy(before)
    for name, prefix in prefixes.items():
        expected["services"][name]["image"] = prefix + ":" + version
    proxy = expected["services"]["proxy"]
    proxy["environment"]["PROXY_LAN_IP"] = ip
    proxy["environment"]["CADDY_DEFAULT_SNI"] = ip
    expected["services"]["assistant-api"]["environment"]["HOMEBOX_PUBLIC_ORIGIN"] = "https://" + ip + ":8443"
    original = next(v for v in proxy["volumes"] if v["target"] == "/etc/caddy/Caddyfile")
    base = copy.deepcopy(original)
    original["source"] = str(Path(original["source"]).with_name("Caddyfile.lan"))
    base["target"] = "/etc/caddy/Caddyfile.base"
    proxy["volumes"].append(base)
    if not testing:
        proxy["ports"] = (proxy.get("ports") or []) + [
            {"mode": "ingress", "host_ip": ip, "target": port, "published": str(port), "protocol": "tcp"}
            for port in (443, 8443)]
    # Compose orders ports and mounts; order is not a configuration permission.
    for cfg in (expected, after):
        for service in cfg["services"].values():
            for key in ("ports", "volumes"):
                if key in service:
                    service[key] = sorted(service[key], key=lambda v: json.dumps(v, sort_keys=True))
    if expected != after:
        raise ValueError("Access candidate changes configuration beyond the approved IP/origin/listeners/Caddy mounts.")


def candidate(m):
    spec = guard(m)
    cfg = m.config()
    rollback = m.journal()["access_change"].get("direction") == "rollback"
    if rollback:
        if cfg != spec["config"]:
            raise ValueError("Rollback configuration differs from the exact original.")
    else:
        validate_delta(spec["config"], copy.deepcopy(cfg), spec["ip"], spec["version"], m.PREFIXES,
                       bool(os.environ.get("LC_TEST_OVERRIDE")))
        save(m, directory(m) / "forward-config.json", cfg)
    save(m, directory(m) / "target-config.json", cfg)
    change = {**m.journal()["access_change"], "attempt": uuid.uuid4().hex}
    save(m, m.STATE / "installation.json", {**m.journal(), "access_change": change})


def verify(m):
    spec = guard(m)
    cfg = m.load(directory(m) / "target-config.json")
    current = m.containers()
    if set(current) != set(m.SERVICES):
        raise ValueError("All six original services are required after access maintenance.")
    for name, item in current.items():
        old = spec["before"][name]
        if not item["State"].get("Running") or item["State"].get("Health", {}).get("Status") != "healthy":
            raise ValueError("Access service is not healthy: " + name)
        if not runtime_matches(m, name, item, old, cfg):
            raise ValueError("Unexpected runtime environment, HostConfig or mount configuration: " + name)
    if m.load(m.WORK / "access-ca.json") != spec["ca"]:
        raise ValueError("Existing CA certificate/private key identity changed.")
    save(m, m.WORK / "before.json", current)
    m.validate_config()  # Normal hardening/secret/semantic guards on the result.
    save(m, directory(m) / "verified.json", {"status": "PASS", "images": {
        name: value["Image"] for name, value in current.items()}})
    phase(m, "verified")


def finish(m):
    spec = guard(m)
    if m.journal()["access_change"]["phase"] != "verified":
        raise ValueError("Access must pass runtime/TLS verification before committing.")
    rollback = m.journal()["access_change"].get("direction") == "rollback"
    proof = m.load(directory(m) / "https-proof.json", {})
    if (proof.get("status") != "PASS" or not proof.get("tls_verified")
            or proof.get("access_config_sha256") != m.file_digest(directory(m) / "target-config.json")
            or proof.get("access_attempt") != m.journal()["access_change"].get("attempt")
            or proof.get("service_IDs") != {k: v["Id"] for k, v in m.containers().items()}):
        raise ValueError("Fresh CA/hostname HTTPS/static/auth verification is required.")
    if (not rollback or spec["overlays"][1]) and fixed_hosts(spec):
        origin = m.load(directory(m) / "origin-proof.json", {})
        if origin.get("status") != "passed" or origin.get("IP_SAN") != spec["ip"]:
            raise ValueError("The exact-IP Origin/TLS matrix is required.")
    body = m.read_env(directory(m) / ("before.env" if rollback else "after.env"))
    stage_file(m.DEPLOY / ".env", body, (m.DEPLOY / ".env").stat())
    state = m.journal()
    change = {**state["access_change"], "status": "rolled-back" if rollback else "complete", "phase": "ready"}
    save(m, m.STATE / "installation.json", {
        **state, "phase": "ready", "resources": m.resource_snapshot(),
        "overlays": spec["overlays"] if rollback else [spec["overlays"][0], True, spec["overlays"][2]],
        "access_change": change,
        "active_version": m.environment(body)["STACK_VERSION"],
    })
    print("Access configuration rolled back; data and images unchanged." if rollback else
          "Tailnet companion: https://" + spec["ip"] + "\nNative Homebox: https://" + spec["ip"] +
          ":8443\nTrust the existing public Caddy CA on the browser device. Tailnet reachability is client-verified.")


def dispatch(mode, m):
    if mode == "access-prepare":
        prepare(m)
    elif mode == "access-select":
        # Recovery closes its authenticated proxy before this full guard. Forward
        # assignment is refreshed by tailscale before candidate/writer guards.
        # Rollback must not require the lost address it is removing.
        guard(m, require_address=False)
        if os.environ["LC_ACCESS_ACTION"] == "rollback":
            phase(m, "recovering", "rollback")
        instructions(m)
    elif mode == "access-candidate":
        candidate(m)
    elif mode == "access-guard":
        guard(m)
    elif mode == "access-verify":
        verify(m)
    elif mode == "access-phase":
        guard(m)
        phase(m, os.environ["LC_ACCESS_PHASE"])
    elif mode == "access-finish":
        finish(m)
    elif mode == "access-smoke":
        path = directory(m) / "https-proof.json"
        m.smoke(proof_path=path)
        proof = m.load(path)
        proof["access_config_sha256"] = m.file_digest(directory(m) / "target-config.json")
        proof["access_attempt"] = m.journal()["access_change"]["attempt"]
        proof["service_IDs"] = {k: v["Id"] for k, v in m.containers().items()}
        save(m, path, proof)
    elif mode == "access-proof-paths":
        spec = guard(m)
        root = directory(m)
        # The checker creates its private evidence exclusively on each attempt.
        origin = root / "origin-proof.json"
        if origin.exists():
            origin.rename(root / ("origin-proof-" + uuid.uuid4().hex + ".json"))
        (m.WORK / "access-ip").write_text(spec["ip"] + "\n")
        (m.WORK / "access-origin-proof").write_text(str(origin) + "\n")
        (m.WORK / "access-api-image").write_text(spec["before"]["assistant-api"]["Image"] + "\n")
        (m.WORK / "access-fixed-hosts").write_text("1\n" if fixed_hosts(spec) else "0\n")
    elif mode == "access-is-pending":
        print("1" if pending(m) else "0")
    elif mode == "access-trusted-proxy":
        print(trusted_proxy(m))
    else:
        raise ValueError("Unknown access maintenance mode.")


def fixed_hosts(spec):
    env = spec["config"]["services"]["proxy"]["environment"]
    return env.get("HOMEBOX_HOST") == "homebox.localhost" and env.get("ASSISTANT_HOST") == "inventory.localhost"
