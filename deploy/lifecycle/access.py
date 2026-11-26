"""Explicit, image-preserving configuration maintenance for Tailnet access."""
import copy
import ipaddress
import json
import os
from pathlib import Path
import stat
import uuid
import local_lan

TAILNET = ipaddress.ip_network("100.64.0.0/10")
CHANGED = {"assistant-api", "proxy"}
SOURCE_FILES = (
    "deploy/homebox", "deploy/lifecycle/metadata.py", "deploy/lifecycle/access.py",
    "deploy/lifecycle/access.sh",
    "deploy/docker-compose.yml", "deploy/docker-compose.external-ai.yml",
    "deploy/docker-compose.lan.yml", "deploy/reverse-proxy/Caddyfile",
    "deploy/reverse-proxy/Caddyfile.lan", "deploy/tests/check_lan_access.py",
    "deploy/docker-compose.lan-local.yml", "deploy/reverse-proxy/Caddyfile.lan-local",
    "deploy/lifecycle/local_lan.py",
    "deploy/reverse-proxy/run-dual.sh", "deploy/docker-compose.lan-rescue.yml",
    "deploy/lifecycle/local_lan_evidence.sh",
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
    value = m.load(path)
    if value.get("schema", 1) not in (1, 2) or value.get("kind", "tailscale") not in ("tailscale", "lan"):
        raise ValueError("Unknown private access plan version.")
    if value.get("listener_mode", "address") not in ("address", "all-ipv4"):
        raise ValueError("Unknown access listener mode; restore the matching reviewed source.")
    return value


def changed(spec):
    return CHANGED | ({"homebox"} if spec.get("native_reconfigure") else set())


def listener_mode(env):
    """Only the exact paired HTTPS overrides form the explicit wildcard mode."""
    if env.get("PROXY_BIND_IP", "127.0.0.1") != "127.0.0.1":
        raise ValueError("Access preserves the original loopback HTTP listener.")
    https, lan = env.get("PROXY_HTTPS_BIND_IP", ""), env.get("PROXY_LAN_BIND_IP", "")
    if https == lan == "0.0.0.0":
        address(env.get("PROXY_LAN_IP", ""))
        if env.get("PROXY_LOCAL_LAN_IP"):
            raise ValueError("Dual LAN/Tailnet TLS requires precise listeners, not --listen-all.")
        return "all-ipv4"
    if https not in ("", "127.0.0.1") or lan not in ("", env.get("PROXY_LAN_IP", "")):
        raise ValueError("HTTPS bind overrides must be the exact paired all-IPv4 mode or private defaults.")
    local = env.get("PROXY_LOCAL_LAN_IP", "")
    if local:
        local_lan.address(local)
    return "address"


def listener_ports(ip, mode, local_ip=""):
    if local_ip and mode == "all-ipv4":
        raise ValueError("Dual LAN/Tailnet TLS requires precise listeners, not --listen-all.")
    ports = [{"mode": "ingress", "host_ip": "127.0.0.1", "target": 80,
              "published": "80", "protocol": "tcp"}]
    if mode == "all-ipv4":
        ports += [{"mode": "ingress", "host_ip": "0.0.0.0", "target": port,
                   "published": str(port), "protocol": "tcp"} for port in (443, 8443)]
    else:
        ports.append({"mode": "ingress", "host_ip": "127.0.0.1", "target": 443,
                      "published": "443", "protocol": "tcp"})
        if ip:
            ports += [{"mode": "ingress", "host_ip": ip, "target": port,
                       "published": str(port), "protocol": "tcp"} for port in (443, 8443)]
        if local_ip:
            local_lan.address(local_ip)
            ports += [{"mode": "ingress", "host_ip": local_ip, "target": target,
                       "published": str(port), "protocol": "tcp"}
                      for port, target in ((443, 14443), (8443, 18443))]
    return ports


def phase(m, value, direction=None):
    state = dict(m.journal()["access_change"])
    state["phase"] = value
    if direction:
        state["direction"] = direction
    flags = {"configuring-api": "api_recreation_started", "configuring-proxy": "proxy_recreation_started",
             "configuring-native": "native_recreation_started"}
    if value in flags:
        state[flags[value]] = True
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
    body = m.read_env()
    env = m.environment(body)
    is_local = os.environ.get("LC_ACCESS_ACTION") == "lan"
    local_ip = local_lan.address(os.environ["LC_ACCESS_IP"]) if is_local else env.get("PROXY_LOCAL_LAN_IP", "")
    ip = address(env.get("PROXY_LAN_IP", "")) if is_local else address(os.environ["LC_ACCESS_IP"])
    local_assignment = local_lan.evidence(m, local_ip) if local_ip else None
    assigned = (m.WORK / "tailscale-ip.txt").read_text().split()
    if assigned != [ip]:
        raise ValueError("Requested address differs from locally assigned Tailscale IPv4.")
    info = (m.DEPLOY / ".env").stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("The protected environment must be owned and private.")
    before_mode = listener_mode(env)
    flag = os.environ.get("LC_ACCESS_LISTEN_ALL", "0")
    if flag not in ("0", "1"):
        raise ValueError("Unknown explicit listener selection.")
    if local_ip and flag == "1":
        raise ValueError("Dual LAN/Tailnet TLS requires precise listeners; omit --listen-all.")
    mode = "address" if is_local else ("all-ipv4" if flag == "1" else "address")
    if env.get("TLS_ISSUER", "internal") != "internal":
        raise ValueError("This transition requires internal TLS.")
    if set(m.config()["services"]) - {"backup"} != set(m.SERVICES):
        raise ValueError("Access requires the six-service published configuration.")
    if not os.environ.get("LC_TEST_OVERRIDE") and public_ports(m.config()["services"]["proxy"]) != public_ports(
            {"ports": listener_ports(env.get("PROXY_LAN_IP"), before_mode, env.get("PROXY_LOCAL_LAN_IP", ""))}):
        raise ValueError("Access requires the exact original private or previously selected HTTPS listeners.")
    if env.get("PROXY_LAN_IP"):
        address(env["PROXY_LAN_IP"])
        if not state["overlays"][1]:
            raise ValueError("Existing canonical Tailnet address has no corresponding overlay receipt.")
        if env["PROXY_LAN_IP"] == ip and before_mode == mode and env.get("PROXY_LOCAL_LAN_IP", "") == local_ip:
            (m.WORK / "access-noop").write_text("1\n")
            print("Requested HTTPS identities are already configured (" + mode + ").")
            return
    elif state["overlays"][1]:
        raise ValueError("Existing LAN overlay is not a loopback-only installation.")
    identifier = uuid.uuid4().hex
    root = m.STATE / "access" / identifier
    root.parent.mkdir(mode=0o700, exist_ok=True)
    if root.parent.is_symlink() or root.parent.stat().st_uid != os.getuid() or stat.S_IMODE(root.parent.stat().st_mode) & 0o077:
        raise ValueError("Access operation directory must be owned and private.")
    root.mkdir(mode=0o700)
    version = "access-" + identifier
    candidate = m.replace_setting(m.replace_setting(body, "PROXY_LAN_IP", ip), "STACK_VERSION", version)
    candidate = m.replace_setting(candidate, "PROXY_HTTPS_BIND_IP", "0.0.0.0" if mode == "all-ipv4" else "")
    candidate = m.replace_setting(candidate, "PROXY_LAN_BIND_IP", "0.0.0.0" if mode == "all-ipv4" else "")
    if local_ip:
        if local_ip in (env.get("HOMEBOX_HOST"), env.get("ASSISTANT_HOST")):
            raise ValueError("The LAN IP identity must be distinct from the existing Native and Companion hostnames.")
        candidate = m.replace_setting(candidate, "PROXY_LOCAL_LAN_IP", local_ip)
    stage_file(root / "before.env", body, info)
    stage_file(root / "after.env", candidate, info)
    spec = {
        "schema": 2, "kind": "lan" if is_local else "tailscale",
        "id": identifier, "binding": m.selected(), "ip": ip, "version": version,
        "local_ip": local_ip, "before_local_ip": env.get("PROXY_LOCAL_LAN_IP", ""),
        "local_assignment": local_assignment,
        "native_reconfigure": bool(is_local and m.config()["services"]["homebox"]["environment"].get("HBOX_OPTIONS_HOSTNAME")),
        "listener_mode": mode, "before_listener_mode": before_mode,
        "sources": sources(m), "before": current, "config": m.config(),
        "resources": state["resources"], "owner": m.installation_owner(),
        "overlays": state["overlays"], "before_env": m.digest(body.encode()),
        "after_env": m.digest(candidate.encode()), "env_metadata": [
            stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid],
        "ca": m.load(m.WORK / "access-ca.json"),
        "secrets": secret_tree(m),
    }
    if spec["native_reconfigure"]:
        backup = m.load(m.WORK / "access-native-backup.json", {})
        path = m.DEPLOY / backup.get("relative", "")
        if (not backup or path.is_symlink() or not path.is_file()
                or m.file_digest(path) != backup.get("sha256")
                or path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) != 0o600):
            raise ValueError("First dual-address Native reconfiguration requires a fresh verified private backup.")
        spec["native_backup"] = backup
    if is_local and spec["before_local_ip"] and before_mode == "address":
        # The separate, fixed recovery overlay withdraws only the prior LAN
        # sockets. Keep the protected environment and identities unchanged.
        stage_file(root / "rescue.env", body, info)
        spec["rescue_env"] = m.digest(body.encode())
        rescue = copy.deepcopy(spec["config"])
        proxy = rescue["services"]["proxy"]
        if "ports" in proxy:
            proxy["ports"] = [
                value for value in proxy["ports"]
                if value.get("host_ip") != spec["before_local_ip"]]
        normalize(rescue)
        save(m, root / "rescue-expected-config.json", rescue)
        spec["rescue_config_sha256"] = m.file_digest(root / "rescue-expected-config.json")
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
    direction = m.journal()["access_change"].get("direction")
    rollback = direction in ("rollback", "rescue")
    (m.WORK / "external").write_text("1\n" if spec["overlays"][0] else "0\n")
    (m.WORK / "lan").write_text(("1" if spec["overlays"][1] else "0") + "\n" if rollback else "1\n")
    local = spec.get("before_local_ip", "") if rollback else spec.get("local_ip", "")
    (m.WORK / "local-lan").write_text("1\n" if local else "0\n")
    (m.WORK / "access-rescue").write_text("1\n" if direction == "rescue" else "0\n")
    selected_env = "rescue.env" if direction == "rescue" else ("before.env" if rollback else "after.env")
    (m.WORK / "access-env").write_text(host_path(m, directory(m) / selected_env) + "\n")
    (m.WORK / "access-native").write_text("1\n" if spec.get("native_reconfigure") else "0\n")
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


def normalize(cfg):
    for service in cfg["services"].values():
        for key in ("ports", "volumes"):
            if key in service:
                service[key] = sorted(service[key], key=lambda value: json.dumps(value, sort_keys=True))


def rescued_config(m, spec):
    path = directory(m) / "rescue-config.json"
    if not path.exists():
        return {}
    if (path.is_symlink() or not m.journal()["access_change"].get("rescue_authorized")
            or m.file_digest(path) != spec.get("rescue_config_sha256")):
        raise ValueError("The private Tailnet-only rescue configuration is not authorized or changed.")
    return m.load(path)


def runtime_matches(m, name, item, old, cfg):
    service = cfg["services"][name]
    wanted = dict(entry.split("=", 1) for entry in old["Config"]["Env"])
    wanted.update({key: str(value) for key, value in service.get("environment", {}).items()})
    if dict(entry.split("=", 1) for entry in item["Config"]["Env"]) != wanted:
        return False
    for key, setting in (("Cmd", "command"), ("Entrypoint", "entrypoint"),
                         ("User", "user"), ("WorkingDir", "working_dir")):
        configured = service.get(setting)
        if configured is None:
            configured = old["Config"].get(key)
        if key in old["Config"] and item["Config"].get(key) != configured:
            return False
    if "Healthcheck" in old["Config"] and item["Config"].get("Healthcheck") != old["Config"]["Healthcheck"]:
        return False
    expected_host = copy.deepcopy(old["HostConfig"])
    expected_mounts = copy.deepcopy(mounts(old))
    if name == "proxy":
        expected_host["PortBindings"] = public_ports(service) or old["HostConfig"].get("PortBindings")
        targets = ("/etc/caddy/Caddyfile", "/etc/caddy/Caddyfile.base", "/etc/caddy/run-dual.sh")
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
        "deploy/docker-compose.yml", "deploy/docker-compose.external-ai.yml", "deploy/docker-compose.lan.yml",
        "deploy/docker-compose.lan-local.yml", "deploy/docker-compose.lan-rescue.yml")}
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
    configs = [spec["config"], m.load(directory(m) / "forward-config.json", {}),
               rescued_config(m, spec)]
    if not any(cfg and runtime_matches(m, "proxy", item, old, cfg) for cfg in configs):
        raise ValueError("Refusing to stop an untrusted replacement proxy configuration.")
    return item["Id"]


def guard(m, require_address=True):
    base_context(m)
    if not pending(m):
        raise ValueError("No pending access operation to recover.")
    spec = plan(m)
    direction = m.journal()["access_change"].get("direction") if require_address else None
    if require_address and direction != "rollback" and (
            m.WORK / "tailscale-ip.txt").read_text().split() != [spec["ip"]]:
        raise ValueError("The selected Tailscale address is no longer locally assigned.")
    if require_address and direction not in ("rollback", "rescue") and spec.get("local_ip"):
        if local_lan.evidence(m, spec["local_ip"]) != spec.get("local_assignment"):
            raise ValueError("The selected physical LAN interface/address changed during access maintenance.")
    if spec["binding"] != m.selected() or sources(m) != spec["sources"]:
        raise ValueError("Access source/binding changed; restore the reviewed source before recovery.")
    if m.installation_owner() != spec["owner"]:
        raise ValueError("Installation owner changed during access maintenance.")
    if m.volume_snapshot() != spec["resources"]["volumes"]:
        raise ValueError("Access volume identity is missing or changed; no writer may start.")
    current = m.containers()
    changes = changed(spec)
    if set(current) - set(m.SERVICES) or not (set(m.SERVICES) - changes).issubset(current):
        raise ValueError("An unaffected service is missing; access cannot repair it.")
    change = m.journal()["access_change"]
    for name, flag in (("assistant-api", "api_recreation_started"), ("proxy", "proxy_recreation_started"),
                       ("homebox", "native_recreation_started")):
        if name not in current and not change.get(flag):
            raise ValueError("A service disappeared before its planned recreation: " + name)
    for name, item in current.items():
        old = spec["before"][name]
        if item["Image"] != old["Image"]:
            raise ValueError("An exact runtime image ID changed: " + name)
        networks = network_ids(item)
        prior_networks = network_ids(old)
        if networks != prior_networks:
            created = name in changes and item["State"].get("Status") == "created"
            if not created or any(k not in prior_networks or v not in ("", prior_networks[k])
                                  for k, v in networks.items()):
                raise ValueError("Access may not replace network identity: " + name)
        if name not in changes:
            if (item["Id"] != old["Id"] or item["Config"]["Env"] != old["Config"]["Env"]
                    or item["HostConfig"] != old["HostConfig"] or mounts(item) != mounts(old)
                    or item["State"].get("StartedAt") != old["State"].get("StartedAt")
                    or not item["State"].get("Running")
                    or item["State"].get("Health", {}).get("Status") != "healthy"):
                raise ValueError("Unaffected service changed: " + name)
        else:
            flag = {"assistant-api": "api_recreation_started", "proxy": "proxy_recreation_started",
                    "homebox": "native_recreation_started"}[name]
            if not change.get(flag) and (
                    item["Id"] != old["Id"] or item["State"].get("StartedAt") != old["State"].get("StartedAt")):
                raise ValueError("A service was replaced/restarted before its planned recreation: " + name)
            configurations = [spec["config"]]
            target = m.load(directory(m) / "forward-config.json")
            if target:
                configurations.append(target)
            rescue = rescued_config(m, spec)
            if rescue:
                configurations.append(rescue)
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
    if m.digest(env.encode()) not in (spec["before_env"], spec["after_env"], spec.get("rescue_env")):
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
    if spec.get("rescue_env") and m.file_digest(directory(m) / "rescue.env") != spec["rescue_env"]:
        raise ValueError("Private Tailnet rescue environment snapshot changed.")
    if spec.get("rescue_config_sha256"):
        expected = directory(m) / "rescue-expected-config.json"
        if expected.is_symlink() or m.file_digest(expected) != spec["rescue_config_sha256"]:
            raise ValueError("The precomputed Tailnet-only rescue configuration changed.")
    if change.get("rescue_authorized"):
        name = change.get("rescue_failure_file", "")
        token = name.removeprefix("rescue-failure-").removesuffix(".json")
        receipt = directory(m) / name
        if (name != "rescue-failure-" + token + ".json" or len(token) != 32
                or any(c not in "0123456789abcdef" for c in token)
                or receipt.is_symlink() or m.file_digest(receipt) != change.get("rescue_failure_sha256")):
            raise ValueError("The exact prior LAN bind-failure receipt changed.")
    return spec


def validate_delta(before, after, ip, version, prefixes, testing=False, listener_mode="address", local_ip=""):
    if listener_mode not in ("address", "all-ipv4"):
        raise ValueError("Unknown access listener mode.")
    expected = copy.deepcopy(before)
    for name, prefix in prefixes.items():
        expected["services"][name]["image"] = prefix + ":" + version
    proxy = expected["services"]["proxy"]
    proxy["environment"]["PROXY_LAN_IP"] = ip
    proxy["environment"]["CADDY_DEFAULT_SNI"] = ip
    expected["services"]["assistant-api"]["environment"]["HOMEBOX_PUBLIC_ORIGIN"] = "https://" + ip + ":8443"
    original = next(v for v in proxy["volumes"] if v["target"] == "/etc/caddy/Caddyfile")
    if not any(v["target"] == "/etc/caddy/Caddyfile.base" for v in proxy["volumes"]):
        base = copy.deepcopy(original)
        original["source"] = str(Path(original["source"]).with_name("Caddyfile.lan"))
        base["target"] = "/etc/caddy/Caddyfile.base"
        proxy["volumes"].append(base)
    if local_ip:
        local_lan.address(local_ip)
        proxy["environment"]["PROXY_LOCAL_LAN_IP"] = local_ip
        expected["services"]["assistant-api"]["environment"]["ASSISTANT_LAN_ORIGIN"] = "https://" + local_ip
        expected["services"]["homebox"]["environment"]["HBOX_OPTIONS_HOSTNAME"] = ""
        original["source"] = str(Path(original["source"]).with_name("Caddyfile.lan-local"))
        proxy["command"] = ["/bin/sh", "/etc/caddy/run-dual.sh"]
        if not any(v["target"] == "/etc/caddy/run-dual.sh" for v in proxy["volumes"]):
            script = copy.deepcopy(original)
            script.update(source=str(Path(original["source"]).with_name("run-dual.sh")),
                          target="/etc/caddy/run-dual.sh")
            proxy["volumes"].append(script)
    if not testing:
        # Replace only the exact HTTPS set, never append wildcard bindings next
        # to loopback or the canonical IP on the same host ports.
        http = [v for v in proxy.get("ports", []) if v["target"] == 80]
        proxy["ports"] = http + [v for v in listener_ports(ip, listener_mode, local_ip) if v["target"] != 80]
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
    direction = m.journal()["access_change"].get("direction")
    if direction == "rescue":
        expected = m.load(directory(m) / "rescue-expected-config.json")
        normalize(cfg)
        if cfg != expected:
            raise ValueError("Tailnet rescue may withdraw only the proven vanished prior LAN sockets.")
        save(m, directory(m) / "rescue-config.json", cfg)
    elif direction == "rollback":
        if cfg != spec["config"]:
            raise ValueError("Rollback configuration differs from the exact original.")
    else:
        validate_delta(spec["config"], copy.deepcopy(cfg), spec["ip"], spec["version"], m.PREFIXES,
                       bool(os.environ.get("LC_TEST_OVERRIDE")), spec.get("listener_mode", "address"),
                       spec.get("local_ip", ""))
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
    direction = m.journal()["access_change"].get("direction")
    rollback = direction in ("rollback", "rescue")
    recovery = os.environ.get("LC_ACCESS_RECOVERY_ONLY", "0") == "1"
    proof = m.load(directory(m) / "https-proof.json", {})
    if (proof.get("status") != "PASS" or not proof.get("tls_verified")
            or proof.get("access_config_sha256") != m.file_digest(directory(m) / "target-config.json")
            or proof.get("access_attempt") != m.journal()["access_change"].get("attempt")
            or proof.get("service_IDs") != {k: v["Id"] for k, v in m.containers().items()}):
        raise ValueError("Fresh CA/hostname HTTPS/static/auth verification is required.")
    target = m.load(directory(m) / "target-config.json")
    for ip, local, name in origin_targets(target):
        origin = m.load(directory(m) / ("origin-" + name + ".json"), {})
        if (origin.get("status") != "passed" or origin.get("IP_SAN") != ip
                or origin.get("access_attempt") != m.journal()["access_change"].get("attempt")
                or origin.get("access_config_sha256") != m.file_digest(directory(m) / "target-config.json")
                or origin.get("publicCA_SHA256") != spec["ca"]["root.crt"]["sha256"]
                or origin.get("local_listener") != local
                or origin.get("invalidNonGETOriginCasesRejected") != 49):
            raise ValueError("The exact-IP Origin/TLS matrix is required.")
    selected_env = "rescue.env" if direction == "rescue" else ("before.env" if rollback else "after.env")
    body = m.read_env(directory(m) / selected_env)
    stage_file(m.DEPLOY / ".env", body, (m.DEPLOY / ".env").stat())
    state = m.journal()
    local = spec.get("before_local_ip", "") if rollback else spec.get("local_ip", "")
    pending_recovery = recovery or direction == "rescue"
    change = {**state["access_change"],
              "status": "pending" if pending_recovery else ("rolled-back" if rollback else "complete"),
              "phase": ("rescue-active" if direction == "rescue" else "prior-restored") if pending_recovery else "ready",
              "direction": "forward" if pending_recovery else direction}
    save(m, m.STATE / "installation.json", {
        **state, "phase": "access-" + change["phase"] if pending_recovery else "ready", "resources": m.resource_snapshot(),
        "overlays": spec["overlays"] if rollback else [spec["overlays"][0], True, spec["overlays"][2]],
        "local_lan_enabled": bool(local),
        "access_change": change,
        "access_listener_mode": spec.get("before_listener_mode", "address") if rollback
        else spec.get("listener_mode", "address"),
        "active_version": m.environment(body)["STACK_VERSION"],
    })
    if pending_recovery:
        print("Prior Tailnet HTTPS is verified and available; LAN maintenance remains PENDING (" +
              change["phase"] + "). Original before.env is retained. Use access resume or access rollback.")
        return
    print("Access configuration rolled back; data and images unchanged." if rollback else
          "Tailnet companion: https://" + spec["ip"] + "\nNative Homebox: https://" + spec["ip"] +
          ":8443" + (("\nLAN companion: https://" + local + "\nLAN Native: https://" + local + ":8443") if local else "") +
          "\nTrust the existing public Caddy CA on the browser device. Network reachability is client-verified.")


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
        spec = guard(m)
        if os.environ["LC_ACCESS_PHASE"] == "configuring-native":
            backup = spec.get("native_backup", {})
            if not backup or m.file_digest(m.DEPLOY / backup["relative"]) != backup["sha256"]:
                raise ValueError("The first Native dual-address change requires its unchanged private backup.")
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
        rows = []
        for ip, local, name in origin_targets(m.config()):
            origin = root / ("origin-" + name + ".json")
            if origin.exists():
                origin.rename(root / ("origin-" + name + "-" + uuid.uuid4().hex + ".json"))
            rows.append("|".join((ip, "1" if local else "0", str(origin))))
        (m.WORK / "access-origin-checks").write_text("\n".join(rows) + ("\n" if rows else ""))
        (m.WORK / "access-api-image").write_text(spec["before"]["assistant-api"]["Image"] + "\n")
        env = m.config()["services"]["proxy"]["environment"]
        (m.WORK / "access-homebox-host").write_text(env["HOMEBOX_HOST"] + "\n")
        (m.WORK / "access-assistant-host").write_text(env["ASSISTANT_HOST"] + "\n")
        (m.WORK / "access-proof-attempt").write_text(m.journal()["access_change"]["attempt"] + "\n")
        (m.WORK / "access-proof-config").write_text(m.file_digest(root / "target-config.json") + "\n")
    elif mode == "access-is-pending":
        print("1" if pending(m) else "0")
    elif mode == "access-is-local":
        print("1" if plan(m).get("kind") == "lan" else "0")
    elif mode == "access-local-preflight":
        state = base_context(m)
        if pending(m) or state.get("phase") != "ready":
            raise ValueError("Finish existing deployment/access recovery before adding a LAN identity.")
        env = m.environment(m.read_env())
        address(env.get("PROXY_LAN_IP", ""))
        local_lan.evidence(m, os.environ["LC_ACCESS_IP"])
        current = m.containers()
        if set(current) != set(m.SERVICES) or any(
                not row["State"].get("Running") or row["State"].get("Health", {}).get("Status") != "healthy"
                for row in current.values()):
            raise ValueError("LAN access requires all six original healthy services.")
        m.check_resources()
        m.validate_config()
        save(m, m.WORK / "access-native-backup.json", {})
        (m.WORK / "access-needs-native").write_text(
            "1\n" if m.config()["services"]["homebox"]["environment"].get("HBOX_OPTIONS_HOSTNAME") else "0\n")
    elif mode == "access-backup-record":
        name = (m.WORK / "access-backup-name").read_text().strip()
        if not name.startswith("access-native-") or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in name):
            raise ValueError("Unexpected private Native backup name.")
        path = m.DEPLOY / "backups" / name
        if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise ValueError("Native backup must remain a private operator-owned archive.")
        save(m, m.WORK / "access-native-backup.json", {
            "relative": "backups/" + name, "sha256": m.file_digest(path)})
    elif mode == "access-auto-restore":
        if guard(m, require_address=False).get("kind") != "lan":
            raise ValueError("Automatic prior-Tailnet recovery is only for the explicit LAN transaction.")
        phase(m, "recovering", "rollback")
        attempt = uuid.uuid4().hex
        save(m, m.STATE / "installation.json", {
            **m.journal(), "access_change": {
                **m.journal()["access_change"], "restore_attempt": attempt}})
        (m.WORK / "access-restore-attempt").write_text(attempt + "\n")
        instructions(m)
    elif mode == "access-rescue":
        spec = guard(m, require_address=False)
        prior = spec.get("before_local_ip", "")
        log = m.WORK / "access-exact-restore.log"
        if log.is_symlink():
            raise ValueError("Refusing a symlink prior LAN bind-failure log.")
        error = log.read_text(errors="replace")
        change = m.journal()["access_change"]
        attempt = change.get("restore_attempt", "")
        if (spec.get("kind") != "lan" or not spec.get("rescue_env") or not prior
                or local_lan.assigned(m, prior) or prior not in error
                or not attempt or not error.startswith("access-restore " + attempt + "\n")
                or change.get("phase") != "configuring-proxy"
                or "cannot assign requested address" not in error.lower()):
            raise ValueError("Tailnet rescue requires OS-proven lost prior LAN assignment and its exact bind failure.")
        receipt = directory(m) / ("rescue-failure-" + attempt + ".json")
        save(m, receipt, {
            "operation": spec["id"], "restore_attempt": attempt,
            "prior_lan": prior, "restore_log_sha256": m.file_digest(m.WORK / "access-exact-restore.log"),
            "os_addresses_sha256": m.file_digest(m.WORK / "local-lan-addresses.json")})
        save(m, m.STATE / "installation.json", {
            **m.journal(), "access_change": {
                **change, "rescue_authorized": True,
                "rescue_failure_file": receipt.name, "rescue_failure_sha256": m.file_digest(receipt)}})
        phase(m, "recovering", "rescue")
        instructions(m)
    elif mode == "access-trusted-proxy":
        print(trusted_proxy(m))
    else:
        raise ValueError("Unknown access maintenance mode.")


def fixed_hosts(spec):
    env = spec["config"]["services"]["proxy"]["environment"]
    return env.get("HOMEBOX_HOST") == "homebox.localhost" and env.get("ASSISTANT_HOST") == "inventory.localhost"


def origin_targets(cfg):
    env = cfg["services"]["proxy"]["environment"]
    targets = [(env["PROXY_LAN_IP"], False, "tailnet")] if env.get("PROXY_LAN_IP") else []
    if env.get("PROXY_LOCAL_LAN_IP"):
        targets.append((env["PROXY_LOCAL_LAN_IP"], True, "lan"))
    return targets
