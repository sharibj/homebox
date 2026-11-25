"""Private lifecycle metadata and offline checks. Run through deploy/homebox."""

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import ssl
import stat
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import uuid


SOURCE = Path("/source")
DEPLOY = Path("/deployment")
STATE = Path("/state")
WORK = STATE / "work"
TEST_COMPOSE_OVERRIDE = Path("/selected-compose-override.yml")
APPS = ("homebox", "homebox-mcp", "assistant-api", "pwa")
SERVICES = (*APPS, "proxy", "ollama")
PREFIXES = {
    "homebox": "homebox-inventory",
    "homebox-mcp": "homebox-inventory-mcp",
    "assistant-api": "homebox-inventory-assistant",
    "pwa": "homebox-inventory-pwa",
}
SECRET_NAMES = ("homebox_api_pepper", "assistant_session_secret", "mcp_shared_secret")
DATA_MOUNTS = {"homebox": {"/data": "homebox_data"},
               "assistant-api": {"/data": "assistant_data"},
               "proxy": {"/data": "caddy_data", "/config": "caddy_config"},
               "ollama": {"/root/.ollama": "ollama_data"}}
RESERVED = {"ROOT", "DEPLOY", "PROJECT", "STATE", "PATH", "HOME", "PYTHON", "COMMAND",
            "DAEMON", "EXTERNAL", "LAN", "TEST_OVERRIDE", "YES", "RESUME", "INSTALL_LOCK",
            "REGISTRATION_RECOVERY", "MODEL_RECOVERY", "LOCKED", "PROXY_STOPPED",
            "CUTOVER", "OK", "BOOTSTRAP", "MODEL_DOWNLOAD", "SKIP_INIT", "CHANGES", "ENGINE_OS"}


def load(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as output:
        os.chmod(temporary, 0o600)
        json.dump(value, output, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def digest(data):
    return hashlib.sha256(data).hexdigest()

def file_digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def read_env(path=None):
    # Path.read_text() normalizes CRLF and would alter unrelated protected bytes.
    return (path or DEPLOY / ".env").read_bytes().decode("utf-8")


def environment(body):
    values = {}
    for line in body.splitlines():
        match = re.match(r"^([A-Z][A-Z0-9_]*)=(.*)$", line)
        if not match:
            if line.strip() and not line.lstrip().startswith("#"):
                raise ValueError("Use plain KEY=value deployment configuration.")
            continue
        key, value = match.groups()
        if key in values:
            raise ValueError("Duplicate deployment setting: " + key)
        if key in RESERVED or key.startswith("DOCKER_"):
            raise ValueError("Reserved deployment setting: " + key)
        values[key] = value.strip().strip("\"'")
    return values


def interpolation_keys(body):
    matches = re.findall(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)|([A-Za-z_][A-Za-z0-9_]*))",
                         body.replace("$$", ""))
    return {first or second for first, second in matches}


def environment_keys():
    body = read_env() if (DEPLOY / ".env").exists() else ""
    keys = set(environment(body)) | interpolation_keys(body)
    paths = [DEPLOY / "docker-compose.yml"]
    for flag, filename in (("external", "docker-compose.external-ai.yml"),
                           ("lan", "docker-compose.lan.yml")):
        if (WORK / flag).read_text().strip() == "1":
            paths.append(DEPLOY / filename)
    if os.environ.get("LC_TEST_OVERRIDE"):
        paths.append(TEST_COMPOSE_OVERRIDE)
    for path in paths:
        keys.update(interpolation_keys(path.read_text()))
    if keys & RESERVED or any(key.startswith("DOCKER_") for key in keys):
        raise ValueError("Compose interpolation cannot replace shell or Docker client controls.")
    return sorted(keys | {"HOMEBOX_DEPLOY_DIR", "HOMEBOX_ENV_FILE", "HOMEBOX_BACKUP_DIR"})


def replace_setting(body, key, value):
    pattern = re.compile(r"(?m)^" + re.escape(key) + r"=[^\r\n]*")
    if len(pattern.findall(body)) > 1:
        raise ValueError("Duplicate deployment setting: " + key)
    return pattern.sub(lambda _: key + "=" + value, body) if pattern.search(body) else (
        body + ("" if body.endswith("\n") else "\n") + key + "=" + value + "\n"
    )


def write_environment(body):
    path = DEPLOY / ".env"
    if path.is_symlink():
        raise ValueError("Refusing a symlink environment file.")
    temporary = DEPLOY / ".env.lifecycle-tmp"
    with temporary.open("x") as output:
        os.chmod(temporary, 0o600)
        output.write(body)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def containers():
    result = {}
    for item in load(WORK / "inspect.json", []):
        labels = item["Config"].get("Labels") or {}
        name = labels.get("com.docker.compose.service")
        if name in SERVICES and labels.get("com.docker.compose.oneoff", "False").lower() != "true":
            if name in result:
                raise ValueError("Multiple containers claim service " + name)
            result[name] = item
    return result


def config():
    return load(WORK / "config.json")


def journal():
    return load(STATE / "installation.json", {})


def update(**values):
    state = journal()
    state.update(values)
    save(STATE / "installation.json", state)
    return state


def selected():
    return {key: os.environ.get("LC_" + key.upper(), "") for key in ("root", "project", "daemon", "state")}


def installation_owner():
    path = DEPLOY / ".env.lifecycle-owner"
    if path.is_symlink():
        raise ValueError("Refusing a symlink installation owner receipt.")
    if path.exists():
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            raise ValueError("Installation owner receipt must be an owned private regular file.")
    return load(path)


def volume_snapshot():
    volumes = load(WORK / "volume-inspect.json", [])
    names = {item["Name"] for item in volumes}
    if len(names) != len(volumes):
        raise ValueError("Duplicate volume identities.")
    keys = ("Name", "Driver", "Scope", "CreatedAt", "Mountpoint", "Options", "Labels")
    return {item["Name"]: {key: item.get(key) for key in keys} for item in volumes}


def resource_snapshot():
    current = containers()
    if set(current) != set(SERVICES):
        raise ValueError("All six original service containers are required; no implicit replacement or adoption.")
    volume_state = volume_snapshot()
    mounts = {}
    for name, item in current.items():
        rows = [m for m in item["Mounts"] if m["Type"] in ("volume", "bind")]
        mounts[name] = {m["Destination"]: m for m in rows}
        if len(mounts[name]) != len(rows):
            raise ValueError("Duplicate runtime mount destination.")
    secrets = {name: {"sha256": value, "mode": stat.S_IMODE((DEPLOY / "secrets" / name).stat().st_mode),
                      "uid": (DEPLOY / "secrets" / name).stat().st_uid,
                      "gid": (DEPLOY / "secrets" / name).stat().st_gid}
               for name, value in secret_fingerprints().items()}
    return {"volumes": volume_state, "mounts": mounts, "secrets": secrets,
            "instance_id": environment(read_env()).get("HOMEBOX_INSTANCE_ID")}


def check_resources():
    prior = journal().get("resources")
    present = containers() or load(WORK / "volume-inspect.json", [])
    if prior is None:
        if present and os.environ["LC_COMMAND"] != "adopt":
            raise ValueError("Existing resources have no trusted receipt; explicitly adopt a complete healthy stack.")
        return
    if volume_snapshot() != prior["volumes"]:
        raise ValueError("Existing volume identity is missing or changed; no writer or initializer may start.")
    actual = resource_snapshot()
    if actual != prior:
        raise ValueError("Existing volume identity, data/secret mounts, key metadata or installation UUID changed; no writer may start.")


def bind_source_matches(actual, expected):
    # Desktop reports its VM-side /host_mnt alias; retain exact raw paths in receipts.
    return actual == expected or (
        os.environ.get("LC_ENGINE_OS") == "Docker Desktop" and actual == "/host_mnt" + expected)


def check_mount_bindings():
    actual = resource_snapshot()
    cfg = config()
    expected_names = {v["name"] for v in cfg["volumes"].values()}
    if set(actual["volumes"]) != expected_names:
        raise ValueError("Canonical data volumes are missing or unexpected volumes are present.")
    for logical, definition in cfg["volumes"].items():
        volume = actual["volumes"][definition["name"]]
        labels = volume["Labels"] or {}
        if (volume["Driver"] != "local" or labels.get("com.docker.compose.project") != selected()["project"]
                or labels.get("com.docker.compose.volume") != logical):
            raise ValueError("Existing data volume has different ownership or storage driver.")
    for name in SERVICES:
        service = cfg["services"][name]
        expected = {}
        for entry in service.get("volumes", []):
            kind, source = entry["type"], entry["source"]
            if kind == "volume":
                source = cfg["volumes"][source]["name"]
            expected[entry["target"]] = (kind, source, not entry.get("read_only", False))
        for entry in service.get("secrets", []):
            target = entry.get("target", entry["source"])
            if not target.startswith("/"):
                target = "/run/secrets/" + target
            expected[target] = ("bind", cfg["secrets"][entry["source"]]["file"], False)
        mounted = actual["mounts"][name]
        if set(mounted) != set(expected):
            raise ValueError("Actual data/secret mount destinations differ: " + name)
        for target, (kind, source, writable) in expected.items():
            mount = mounted[target]
            same_source = mount.get("Name") == source if kind == "volume" else bind_source_matches(mount["Source"], source)
            if mount["Type"] != kind or mount["RW"] != writable or not same_source:
                raise ValueError("Actual data/secret mount binding differs: " + name)
            if kind == "volume" and mount["Source"] != actual["volumes"][source]["Mountpoint"]:
                raise ValueError("Runtime data mount does not reference the inspected volume.")
    return actual


def bind_resources():
    resources = check_mount_bindings()
    owner = installation_owner() or {"binding": selected(), "installation": uuid.uuid4().hex}
    if owner["binding"] != selected():
        raise ValueError("Installation has another canonical state directory.")
    owner["established"] = True
    save(DEPLOY / ".env.lifecycle-owner", owner)
    update(resources=resources)


def select():
    binding = selected()
    if (DEPLOY / ".env").is_symlink() or (DEPLOY / "secrets").is_symlink():
        raise ValueError("Refusing symlink deployment configuration or secret directory.")
    old = journal()
    command = os.environ["LC_COMMAND"]
    if old.get("binding") and old["binding"] != binding:
        legacy = {key: value for key, value in binding.items() if key != "state"}
        if command != "adopt" or old["binding"] != legacy:
            raise ValueError("State belongs to another source directory, project, Docker daemon or canonical state path.")
    owner = installation_owner()
    if owner and owner["binding"] != binding:
        raise ValueError("Use this installation's original canonical state directory; implicit adoption is forbidden.")
    if not owner and command not in ("fresh", "adopt"):
        raise ValueError("Unmanaged installation. Explicitly adopt its validated existing stack before maintenance.")
    if owner and owner.get("established") and not old.get("resources") and command != "adopt":
        raise ValueError("Trusted resource receipt is missing; no initialization or implicit adoption is allowed.")
    if old.get("registration_restore") is not None and command_for_restore() is False:
        raise ValueError("Interrupted first-owner bootstrap. Run users restore-registration to restore its prior flag.")
    if old.get("model_download_pending") and not (
            os.environ.get("LC_COMMAND") == "models" and os.environ.get("LC_MODEL_RECOVERY") == "1"):
        raise ValueError("Interrupted model download. Run models restore-network before continuing.")
    current = containers()
    volumes = (WORK / "volumes.txt").read_text().splitlines()
    resume = os.environ.get("LC_RESUME") == "1"
    if command == "fresh":
        if old.get("phase") == "ready":
            raise ValueError("This installation already exists. Use redeploy, not fresh.")
        if (current or volumes) and not (resume and old.get("binding") == binding):
            raise ValueError("Existing project/data volumes found. Use redeploy; fresh never replaces data.")
        if (DEPLOY / ".env").exists() and not old and not resume:
            raise ValueError("Existing configuration found. Review it and use fresh --resume only for unfinished setup.")
    elif not (DEPLOY / ".env").is_file():
        raise ValueError("No deployment configuration. Run fresh first.")
    if command == "redeploy" and not current and not old:
        raise ValueError("Cannot infer an installation from volumes alone. Use its original configuration/receipt.")
    check_resources()
    if command in ("redeploy", "backup", "adopt"):
        if set(current) != set(SERVICES) or any(not item["State"].get("Running")
                or item["State"].get("Health", {}).get("Status") != "healthy" for item in current.values()):
            raise ValueError("Maintenance requires all six original healthy services; recover stopped/missing services explicitly.")
    external = lan = False
    expected_base = str(Path(binding["root"]) / "deploy/docker-compose.yml")
    expected_files = {expected_base,
                      str(Path(binding["root"]) / "deploy/docker-compose.external-ai.yml"),
                      str(Path(binding["root"]) / "deploy/docker-compose.lan.yml")}
    test = os.environ.get("LC_TEST_OVERRIDE")
    if test:
        expected_files.add(test)
    if old.get("model_download_pending"):
        expected_files.add(str(Path(binding["root"]) / "deploy/docker-compose.model-pull.yml"))
    for item in current.values():
        files = (item["Config"].get("Labels") or {}).get(
            "com.docker.compose.project.config_files", "").split(",")
        if expected_base not in files or set(files) - expected_files:
            raise ValueError("Existing Compose paths differ. Refusing to drop an unknown overlay or adopt another checkout.")
        external |= any(p.endswith("/docker-compose.external-ai.yml") for p in files)
        lan |= any(p.endswith("/docker-compose.lan.yml") for p in files)
    if not current and (DEPLOY / ".env").exists():
        env = environment(read_env())
        external = any(env.get(key, "ollama") not in ("", "ollama") for key in (
            "AI_PROVIDER", "AI_CHAT_PROVIDER", "AI_VISION_PROVIDER"))
        external |= any(env.get(key, "") not in ("", "http://ollama:11434") for key in (
            "AI_BASE_URL", "AI_CHAT_BASE_URL", "AI_VISION_BASE_URL"))
        lan = bool(env.get("PROXY_LAN_IP"))
    if old.get("overlays") and current and old["overlays"] != [external, lan, test or ""]:
        raise ValueError("Overlay selection changed from the installation receipt.")
    (WORK / "external").write_text("1\n" if external else "0\n")
    (WORK / "lan").write_text("1\n" if lan else "0\n")
    save(WORK / "before.json", current)
    if command != "status":
        update(binding=binding, overlays=[external, lan, test or ""],
               phase=old.get("phase", "preparing"), bootstrap_pending=old.get(
                   "bootstrap_pending", command == "fresh"))
        if command == "fresh" and owner is None:
            save(DEPLOY / ".env.lifecycle-owner", {
                "binding": binding, "installation": uuid.uuid4().hex, "established": False})


def init_stage():
    old = journal()
    data = containers() or (WORK / "volumes.txt").read_text().strip()
    if data:
        if not all((DEPLOY / "secrets" / n).is_file() for n in SECRET_NAMES):
            raise ValueError("Data exists but an encryption secret is missing. Restore it; never regenerate.")
        if not environment(read_env()).get("HOMEBOX_INSTANCE_ID"):
            raise ValueError("Data exists without its installation UUID. Restore it.")
        (WORK / "skip-init").write_text("1\n")
        return
    (WORK / "skip-init").write_text("0\n")
    bootstrap = STATE / "bootstrap"
    bootstrap.mkdir(exist_ok=True, mode=0o700)
    example = (DEPLOY / ".env.example").read_text()
    (bootstrap / ".env.example").write_text(example)
    if (DEPLOY / ".env").exists():
        body = read_env()
    else:
        body = replace_setting(example, "HOMEBOX_HOST", "homebox.localhost")
        body = replace_setting(body, "ASSISTANT_HOST", "inventory.localhost")
        body = replace_setting(body, "HBOX_ALLOW_REGISTRATION", "false")
    environment(body)
    (bootstrap / ".env").write_text(body)
    os.chmod(bootstrap / ".env", 0o600)
    update(phase="initializing", bootstrap_pending=old.get("bootstrap_pending", True))


def init_install():
    generated = read_env(STATE / "bootstrap/.env")
    generated_values = environment(generated)
    path = DEPLOY / ".env"
    if path.exists():
        body = read_env(path)
        if not environment(body).get("HOMEBOX_INSTANCE_ID"):
            body = replace_setting(body, "HOMEBOX_INSTANCE_ID", generated_values["HOMEBOX_INSTANCE_ID"])
        if body != read_env(path):
            write_environment(body)
    else:
        write_environment(generated)
    uuid.UUID(environment(read_env(path))["HOMEBOX_INSTANCE_ID"])
    directory = DEPLOY / "secrets"
    if directory.is_symlink():
        raise ValueError("Refusing a symlink secret directory.")
    directory.mkdir(exist_ok=True, mode=0o700)
    for name in SECRET_NAMES:
        target = directory / name
        if target.is_symlink() or (target.exists() and not target.stat().st_size):
            raise ValueError("Existing core secret is empty or a symlink: " + name)
        if not target.exists():
            with target.open("xb") as output:
                output.write((STATE / "bootstrap/secrets" / name).read_bytes())
            os.chmod(target, 0o444)
    if (WORK / "external").read_text().strip() == "1" and not (directory / "ai").exists():
        (directory / "ai").mkdir(mode=0o755)
    update(phase="initialized")


def validate_environment_match(name, desired, actual):
    ignored = {"HBOX_OPTIONS_ALLOW_REGISTRATION"} if command_for_restore() and name == "homebox" else set()
    if any(str(value) != actual.get(key) for key, value in desired.items() if key not in ignored):
        raise ValueError("Runtime configuration differs for " + name +
                         "; review configuration maintenance separately from source redeploy/recovery.")


def verify_recovery_image():
    image = load(WORK / "registration-image.json", [])
    if len(image) != 1 or image[0]["Id"] != containers()["homebox"]["Image"]:
        raise ValueError("Registration recovery cannot change the native writer image.")


def validate_config():
    cfg = config()
    project = os.environ["LC_PROJECT"]
    if cfg.get("name") != project:
        raise ValueError("Compose project identity differs.")
    env = environment(read_env())
    if command_for_restore():
        (WORK / "registration-image.txt").write_text(cfg["services"]["homebox"]["image"] + "\n")
    if uuid.UUID(env["HOMEBOX_INSTANCE_ID"]).int == 0:
        raise ValueError("Installation UUID cannot be nil.")
    if env["HOMEBOX_HOST"] == env["ASSISTANT_HOST"]:
        raise ValueError("Homebox and companion need different hostnames.")
    if os.environ["LC_COMMAND"] == "fresh" and env.get("HBOX_ALLOW_REGISTRATION", "false") != "false":
        raise ValueError("Fresh keeps public registration closed. Use explicit first-owner bootstrap afterward.")
    if set(cfg.get("volumes", {})) != {v for mounts in DATA_MOUNTS.values() for v in mounts.values()}:
        raise ValueError("All five canonical persistent volumes are required.")
    for name, required in DATA_MOUNTS.items():
        mounted = {entry["target"]: entry for entry in cfg["services"][name].get("volumes", [])}
        if any(target not in mounted or mounted[target].get("type") != "volume"
               or mounted[target].get("source") != source or mounted[target].get("read_only", False)
               for target, source in required.items()):
            raise ValueError("Required writable persistent data mount missing: " + name)
    for name, volume in cfg.get("volumes", {}).items():
        if volume.get("external") or volume.get("name") != project + "_" + name:
            raise ValueError("Refusing external/renamed data volumes.")
    for name, service in cfg["services"].items():
        if name == "backup":
            continue
        if not service.get("read_only") or "no-new-privileges:true" not in service.get("security_opt", []):
            raise ValueError("Required runtime hardening missing: " + name)
        if name != "proxy" and service.get("ports"):
            raise ValueError("Only the proxy may publish ports.")
    if os.environ.get("LC_TEST_OVERRIDE"):
        if any(s.get("ports") for s in cfg["services"].values()):
            raise ValueError("Validation fixtures must not expose host ports.")
        if any(not n.get("internal") for n in cfg["networks"].values()):
            raise ValueError("Validation networks must be internal.")
    before = load(WORK / "before.json", {})
    if before:
        check_mount_bindings()
    for name, item in before.items():
        actual = dict(entry.split("=", 1) for entry in item["Config"]["Env"])
        service = cfg["services"][name]
        desired = service.get("environment", {})
        validate_environment_match(name, desired, actual)
        if name in ("proxy", "ollama") and cfg["services"][name]["image"] != item["Config"]["Image"]:
            raise ValueError("Pinned runtime image changed; review that maintenance explicitly: " + name)
        host = item["HostConfig"]
        expected_host = {
            "ReadonlyRootfs": service.get("read_only", False),
            "Memory": int(service.get("mem_limit", 0)),
            "NanoCpus": round(float(service.get("cpus", 0)) * 1_000_000_000),
            "PidsLimit": int(service.get("pids_limit", 0)),
        }
        if any(host.get(key) != value for key, value in expected_host.items()):
            raise ValueError("Runtime resource/read-only settings differ; review separately: " + name)
        if sorted(host.get("CapDrop") or []) != sorted(service.get("cap_drop") or []):
            raise ValueError("Runtime capabilities differ; review separately: " + name)
        if sorted(host.get("SecurityOpt") or []) != sorted(service.get("security_opt") or []):
            raise ValueError("Runtime security options differ; review separately: " + name)
        desired_ports = {}
        for port in service.get("ports") or []:
            key = str(port["target"]) + "/" + port.get("protocol", "tcp")
            desired_ports.setdefault(key, []).append({
                "HostIp": port.get("host_ip", ""), "HostPort": str(port.get("published", ""))})
        old_ports = host.get("PortBindings") or {}
        sort_ports = lambda mapping: {
            key: sorted(value, key=lambda p: (p["HostIp"], p["HostPort"])) for key, value in mapping.items()}
        if sort_ports(desired_ports) != sort_ports(old_ports):
            raise ValueError("Published listeners differ; review separately: " + name)
    # Candidate semantic validation reuses the real resolver and checks only its
    # selected key paths, as the image's API UID. Unused shared paths stay unused.
    for name in SECRET_NAMES:
        path = DEPLOY / "secrets" / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size < 32:
            raise ValueError("Missing/invalid core secret: " + name)
    api = cfg["services"]["assistant-api"]["environment"]
    save(WORK / "api-environment.json", api)
    (WORK / "lan-ip.txt").write_text(env.get("PROXY_LAN_IP", "") + "\n")
    (WORK / "fixed-localhost.txt").write_text("1\n" if (
        env["HOMEBOX_HOST"] == "homebox.localhost" and env["ASSISTANT_HOST"] == "inventory.localhost") else "0\n")
    if os.environ["LC_COMMAND"] != "status":
        update(config_sha256=digest(json.dumps(cfg, sort_keys=True).encode()))
        save(WORK / "preservation-guards.json", {
            "environment": digest(replace_setting(read_env(), "STACK_VERSION", "VERSION").encode()),
            "secrets": secret_fingerprints(),
        })


def secret_fingerprints():
    directory = DEPLOY / "secrets"
    result = {}
    for path in directory.rglob("*"):
        if path.is_symlink():
            raise ValueError("Refusing symlink secret state.")
        if path.is_file():
            result[str(path.relative_to(directory))] = file_digest(path)
    return result


def source_files(service):
    if service == "homebox":
        roots = ("backend", "frontend", "deploy/Dockerfile.homebox", "deploy/Dockerfile.homebox.dockerignore")
    elif service == "assistant-api":
        roots = tuple("apps/assistant-api/" + p for p in ("Dockerfile", "requirements.lock", "assistant", "tests"))
    elif service == "homebox-mcp":
        roots = tuple("services/homebox-mcp/" + p for p in ("Dockerfile", "requirements.lock", "adapter", "tests"))
    else:
        roots = ("apps/pwa",)
    result = {}
    ignored = {"node_modules", "dist", "test-results", ".git", ".nuxt", ".output",
               ".data", "__pycache__", ".pytest_cache"}
    for root in roots:
        source = SOURCE / root
        if source.is_dir():
            candidates = []
            for folder, dirs, files in os.walk(source, followlinks=False):
                dirs[:] = [name for name in dirs if name not in ignored and not name.startswith(".env")]
                if any((Path(folder) / name).is_symlink() for name in dirs):
                    raise ValueError("Source snapshots do not follow directory symlinks.")
                candidates.extend(Path(folder) / name for name in files)
        else:
            candidates = [source]
        for path in candidates:
            rel = path.relative_to(SOURCE)
            if any(p in ignored for p in rel.parts) or any(p.startswith(".env") for p in rel.parts):
                continue
            if path.is_symlink():
                raise ValueError("Source snapshots do not follow symlinks: " + str(rel))
            if path.is_file() and path.suffix not in (".pyc", ".log", ".db") and not str(path).endswith((".db-wal", ".db-shm")):
                result[str(rel)] = path
    return result


def snapshot():
    version = "local-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dt%H%M%Sz").lower() + "-" + uuid.uuid4().hex[:8]
    release = STATE / "releases" / version
    release.mkdir(parents=True, mode=0o700)
    old = journal()
    source_hashes = {}
    manifests = {}
    lines = []
    cfg = config()
    for name in APPS:
        files = source_files(name)
        manifest = {}
        for rel, path in sorted(files.items()):
            body = path.read_bytes()
            manifest[rel] = {"bytes": len(body), "sha256": digest(body)}
        source_hashes[name] = digest(json.dumps(manifest, sort_keys=True).encode())
        manifests[name] = manifest
        if (old.get("source_hashes", {}).get(name) == source_hashes[name]
                and old.get("active_images", {}).get(name) == containers().get(name, {}).get("Image")):
            continue
        for rel, path in files.items():
            target = release / "source" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
            if digest(target.read_bytes()) != manifest[rel]["sha256"]:
                raise ValueError("Source changed while snapshotting. Retry after edits finish.")
            os.chmod(target, 0o444)
        host_source = Path(os.environ["LC_STATE"]) / "releases" / version / "source"
        build = cfg["services"][name]["build"]
        if name == "homebox":
            context = host_source
            dockerfile = host_source / "deploy/Dockerfile.homebox"
        else:
            sub = "services/homebox-mcp" if name == "homebox-mcp" else "apps/" + name.replace("assistant-api", "assistant-api")
            context = host_source / sub
            dockerfile = context / "Dockerfile"
        commit = str(build.get("args", {}).get("COMMIT", "unknown"))
        lines.append("|".join((name, PREFIXES[name] + ":" + version, str(dockerfile), str(context), commit)))
    save(release / "source-manifests.json", manifests)
    (WORK / "builds.txt").write_text("\n".join(lines) + "\n")
    (release / "previous.env").write_bytes((DEPLOY / ".env").read_bytes())
    os.chmod(release / "previous.env", 0o600)
    save(release / "previous-images.json", {n: item["Image"] for n, item in containers().items()})
    update(candidate=version, candidate_source_hashes=source_hashes, phase="building",
           recovery_receipt=str(Path(os.environ["LC_STATE"]) / "releases" / version))


def plan():
    state = journal()
    before = containers()
    changes = []
    images = {}
    aliases = []
    for name in APPS:
        path = WORK / ("image-" + name + ".json")
        if name not in [line.split("|")[0] for line in (WORK / "builds.txt").read_text().splitlines() if line]:
            images[name] = before[name]["Image"]
            aliases.append(images[name] + "|" + PREFIXES[name] + ":" + state["candidate"])
            aliases.append(images[name] + "|" + PREFIXES[name] + ":rollback-" + state["candidate"])
            if (not before[name]["State"].get("Running") or
                    before[name]["State"].get("Health", {}).get("Status") != "healthy"):
                changes.append(name)
            continue
        candidate = load(path)[0]
        expected_uid = {"homebox": "65532", "homebox-mcp": "10001", "assistant-api": "10001", "pwa": "101"}[name]
        if candidate["Config"].get("User", "").split(":")[0] != expected_uid:
            raise ValueError("Candidate does not match the delivered non-root UID contract: " + name)
        images[name] = candidate["Id"]
        if (name not in before or candidate["Id"] != before[name]["Image"] or
                not before[name]["State"].get("Running") or
                before[name]["State"].get("Health", {}).get("Status") != "healthy"):
            changes.append(name)
        if name in before and candidate["Config"].get("User", "") != before[name]["Config"].get("User", ""):
            raise ValueError("Candidate changes runtime UID; review migration explicitly.")
        aliases.append(images[name] + "|" + PREFIXES[name] + ":" + state["candidate"])
        if name in before:
            aliases.append(before[name]["Image"] + "|" + PREFIXES[name] + ":rollback-" + state["candidate"])
    if os.environ["LC_COMMAND"] == "fresh" and any(
            n not in before or not before[n]["State"].get("Running") or
            before[n]["State"].get("Health", {}).get("Status") != "healthy" for n in SERVICES):
        changes = list(APPS)
    elif os.environ["LC_COMMAND"] == "redeploy":
        changes += [name for name in ("ollama", "proxy")
                    if name not in before or not before[name]["State"].get("Running") or
                    before[name]["State"].get("Health", {}).get("Status") != "healthy"]
    (WORK / "aliases.txt").write_text("\n".join(aliases) + "\n")
    (WORK / "api-image.txt").write_text(images["assistant-api"] + "\n")
    (WORK / "changes.txt").write_text("\n".join(changes) + "\n")
    (WORK / "has-changes").write_text("1\n" if changes else "0\n")
    update(candidate_images=images, changed_services=changes, phase="built")


def cutover():
    state = journal()
    before = read_env()
    guards = load(WORK / "preservation-guards.json")
    if (guards["environment"] != digest(replace_setting(before, "STACK_VERSION", "VERSION").encode())
            or guards["secrets"] != secret_fingerprints()):
        raise ValueError("Configuration/keys changed during the build. No cutover; review and retry.")
    after = replace_setting(before, "STACK_VERSION", state["candidate"])
    if environment(after) | {"STACK_VERSION": ""} != environment(before) | {"STACK_VERSION": ""}:
        raise ValueError("Unexpected deployment configuration change.")
    write_environment(after)
    update(phase="cutover")


def verify():
    current = containers()
    before = load(WORK / "before.json", {})
    guards = load(WORK / "preservation-guards.json")
    if (guards["environment"] != digest(replace_setting(read_env(), "STACK_VERSION", "VERSION").encode())
            or guards["secrets"] != secret_fingerprints()):
        raise ValueError("Protected environment or secret contents changed.")
    for name in SERVICES:
        item = current.get(name)
        if not item or not item["State"].get("Running") or item["State"].get("Health", {}).get("Status") != "healthy":
            raise ValueError("Service is not healthy: " + name)
        if name in APPS and item["Image"] != journal()["candidate_images"][name]:
            raise ValueError("Actual image differs: " + name)
        if name in before:
            previous = before[name]
            if name not in journal()["changed_services"]:
                if item["Id"] != previous["Id"] or item["Image"] != previous["Image"]:
                    raise ValueError("Unchanged service was recreated: " + name)
            if item["HostConfig"] != previous["HostConfig"]:
                raise ValueError("Host security/resource configuration changed: " + name)
            old_mounts = {m["Destination"]: m for m in previous["Mounts"]}
            new_mounts = {m["Destination"]: m for m in item["Mounts"]}
            if len(old_mounts) != len(previous["Mounts"]) or len(new_mounts) != len(item["Mounts"]) or old_mounts != new_mounts:
                raise ValueError("Persistent/secret mounts changed: " + name)
            old_networks = {k: v["NetworkID"] for k, v in previous["NetworkSettings"]["Networks"].items()}
            new_networks = {k: v["NetworkID"] for k, v in item["NetworkSettings"]["Networks"].items()}
            if old_networks != new_networks:
                raise ValueError("Network identity changed: " + name)
    save(STATE / "releases" / journal()["candidate"] / "verification.json", {
        "services": {name: {"id": item["Id"], "image": item["Image"], "healthy": True}
                     for name, item in current.items()}, "preservation": "PASS"})


def smoke():
    cfg = config()
    api = cfg["services"]["assistant-api"]["environment"]
    context = ssl.create_default_context()
    ca = WORK / "public-ca.crt"
    if ca.exists():
        context.load_verify_locations(str(ca))
        previous = WORK / "previous-public-ca.crt"
        if previous.exists() and previous.read_bytes() != ca.read_bytes():
            raise ValueError("Existing public CA fingerprint changed.")
    original = socket.getaddrinfo
    origins = [api["ASSISTANT_PUBLIC_ORIGIN"], api["HOMEBOX_PUBLIC_ORIGIN"]]
    origins += ["https://" + cfg["services"]["proxy"]["environment"]["HOMEBOX_HOST"]]
    if cfg["services"]["proxy"]["environment"].get("PROXY_LAN_IP"):
        origins += ["https://" + cfg["services"]["proxy"]["environment"]["PROXY_LAN_IP"]]
    hosts = {urllib.parse.urlsplit(value).hostname for value in origins}
    def lookup(host, port, *args, **kwargs):
        return original("127.0.0.1" if host in hosts else host, port, *args, **kwargs)
    socket.getaddrinfo = lookup
    handler = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                         urllib.request.HTTPSHandler(context=context))
    checks = []
    def get(url, expected=200, *, method="GET", origin=None):
        headers = {"Content-Type": "application/json"} if method == "POST" else {}
        if origin is not None:
            headers["Origin"] = origin
        request = urllib.request.Request(url, method=method, headers=headers,
                                         data=b"{}" if method == "POST" else None)
        try:
            response = handler.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            if response.status != expected:
                raise ValueError("Public smoke status differs; no authentication was attempted.")
            body = response.read()
            checks.append({"status": response.status, "sha256": digest(body), "bytes": len(body)})
            return body
    companion = api["ASSISTANT_PUBLIC_ORIGIN"]
    companions = [companion]
    if cfg["services"]["proxy"]["environment"].get("PROXY_LAN_IP"):
        companions.append("https://" + cfg["services"]["proxy"]["environment"]["PROXY_LAN_IP"])
    native = api["HOMEBOX_PUBLIC_ORIGIN"]
    for origin in set(origins):
        get(origin + "/")
    get(native + "/api/v1/status")
    for companion_origin in companions:
        get(companion_origin + "/api/assistant/v1/session", 401)
        action = companion_origin + "/api/assistant/v1/actions"
        get(action, 401, method="POST", origin=companion_origin)
        get(action, 403, method="POST")
        get(action, 403, method="POST", origin="https://foreign.example.invalid")
        for path in sorted((WORK / "static").rglob("*")):
            if path.is_file() and path.name != "50x.html":
                rel = str(path.relative_to(WORK / "static"))
                if get(companion_origin + "/" + rel) != path.read_bytes():
                    raise ValueError("Served static bytes do not match the running image.")
    get(native + "/api/v1/users/logout", 401, method="POST", origin=native)
    if len(companions) > 1:
        get(native + "/api/v1/users/logout", 403, method="POST")
        get(native + "/api/v1/users/logout", 403, method="POST", origin="https://foreign.example.invalid")
    save(STATE / "releases" / journal()["candidate"] / "https-proof.json", {
        "status": "PASS", "tls_verified": True, "checks": checks,
        "no_login_or_inference": True, "cached_service_worker_upgrade": "UNVERIFIED"})
    print("PASS: CA/hostname-verified HTTPS, native status, unauthenticated session and exact static bytes")


def status():
    api = config()["services"]["assistant-api"]["environment"]
    kinds = [api.get("AI_" + r + "_PROVIDER") or api.get("AI_PROVIDER", "ollama") for r in ("CHAT", "VISION")]
    print("Service health is not AI inference readiness.")
    if "ollama" in kinds:
        models = local_models(api)
        listing = (WORK / "model-list.txt").read_text() if (WORK / "model-list.txt").exists() else ""
        installed = {line.split()[0] for line in listing.splitlines()[1:] if line.split()}
        for model in models:
            available = model in installed or model + ":latest" in installed
            print("Local model", model + ":", "installed (inference untested)" if available else "MISSING or unavailable")
        if any(model not in installed and model + ":latest" not in installed for model in models):
            print("AI setup incomplete. Explicit download: deploy/homebox models pull --confirm-download (same global project/state flags).")
        if not models:
            print("Selected Ollama endpoint is remote; model availability is UNVERIFIED.")
    if any(kind != "ollama" for kind in kinds):
        print("External AI configuration/key-file metadata checked. Reachability, billing and inference are NOT tested.")
    print("Registration:", environment(read_env()).get("HBOX_ALLOW_REGISTRATION", "false"))
    if journal().get("bootstrap_pending"):
        print("First owner: run deploy/homebox users bootstrap explicitly; normal fresh keeps public signup closed.")
    print("CA trust is a separate explicit client action; no operating-system trust setting was changed.")

def local_models(api):
    result = set()
    shared_kind = api.get("AI_PROVIDER") or "ollama"
    for role in ("CHAT", "VISION"):
        prefix = "AI_" + role + "_"
        kind = api.get(prefix + "PROVIDER") or shared_kind
        base = api.get(prefix + "BASE_URL") or (api.get("AI_BASE_URL") if kind == shared_kind else None)
        if kind != "ollama" or (base or "http://ollama:11434").rstrip("/") != "http://ollama:11434":
            continue
        model = api.get(prefix + "MODEL") or api.get("AI_MODEL") or api.get("OLLAMA_MODEL", "qwen3-vl:2b")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:-]{0,254}", model):
            raise ValueError("Invalid local model name.")
        result.add(model)
    return sorted(result)

def command_for_restore():
    # Restoring is a separate explicit recovery action, never another signup.
    return os.environ.get("LC_COMMAND") == "users" and os.environ.get("LC_REGISTRATION_RECOVERY") == "1"


def restore_registration():
    body = journal().get("registration_restore")
    if body is None:
        raise ValueError("No interrupted registration window is recorded.")
    current = read_env()
    key = "HBOX_ALLOW_REGISTRATION"
    if replace_setting(current, key, "PRIOR") != replace_setting(body, key, "PRIOR"):
        pattern = re.compile(r"(?m)^" + key + r"=[^\r\n]*(?:\r\n|\n|$)")
        prior = pattern.search(body)
        body, count = pattern.subn(lambda _: prior.group() if prior else "", current)
        if prior and not count:
            body += ("" if not body or body.endswith("\n") else "\n") + prior.group()
    write_environment(body)
    update(registration_nonce=uuid.uuid4().hex, registration_verified=None)


def verify_registration():
    state = journal()
    if state.get("registration_restore") is None or not state.get("registration_nonce"):
        raise ValueError("Registration recovery needs a current durable receipt.")
    truth = {"true": True, "t": True, "1": True, "false": False, "f": False, "0": False}
    def flag(values, key):
        value = (values.get(key) or "false").lower()
        if value not in truth:
            raise ValueError("Cannot verify an unsupported native registration flag.")
        return truth[value]
    prior = flag(environment(state["registration_restore"]), "HBOX_ALLOW_REGISTRATION")
    body = read_env()
    protected = environment(body)
    if flag(protected, "HBOX_ALLOW_REGISTRATION") != prior:
        raise ValueError("The protected registration flag has not been restored.")
    inspected = load(WORK / "registration-container.json", [])
    if len(inspected) != 1:
        raise ValueError("Exactly one native Homebox container must be verified.")
    native = inspected[0]
    labels = native["Config"]["Labels"]
    if labels.get("com.docker.compose.project") != state["binding"]["project"]:
        raise ValueError("Registration verification selected a different project.")
    if not native["State"]["Running"] or native["State"].get("Health", {}).get("Status") != "healthy":
        raise ValueError("Native Homebox must be healthy before clearing recovery.")
    active = dict(value.split("=", 1) for value in native["Config"]["Env"])
    if flag(active, "HBOX_OPTIONS_ALLOW_REGISTRATION") != prior:
        raise ValueError("Actual native registration does not match the recorded prior state.")
    if protected.get("HOMEBOX_HOST") and active.get("HBOX_OPTIONS_HOSTNAME") != protected["HOMEBOX_HOST"]:
        raise ValueError("Actual native hostname does not match protected configuration.")
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with client.open("http://127.0.0.1:7745/api/v1/status", timeout=10) as response:
        status = json.load(response)
    if status.get("allowRegistration") is not prior:
        raise ValueError("The native public status has not restored the recorded registration state.")
    update(registration_verified={"nonce": state["registration_nonce"], "container": native["Id"],
                                 "environment_sha256": digest(body.encode()), "allowRegistration": prior})
    print("PASS: actual healthy native environment and public registration status match the prior state")


def finish_registration_restore():
    state = journal()
    verified = state.get("registration_verified") or {}
    if (state.get("registration_restore") is None or not state.get("registration_nonce")
            or verified.get("nonce") != state["registration_nonce"]
            or verified.get("environment_sha256") != digest(read_env().encode())):
        raise ValueError("Recovery remains pending until the current native state is verified.")
    update(registration_restore=None, registration_nonce=None, registration_verified=None)


def semantic():
    from assistant import config as module
    api = json.load(sys.stdin)
    os.environ.update({k: str(v) for k, v in api.items()})
    original = module.resolve_provider_roles
    module.read_secret = lambda _: "synthetic-offline-check-only-32-characters"
    def key_metadata(path):
        if not path.startswith("/run/secrets/ai/") or "/" in path[len("/run/secrets/ai/"):]:
            raise ValueError("Selected key must be inside the mounted provider directory.")
        selected = Path(path)
        if selected.is_symlink() or not selected.is_file() or not os.access(selected, os.R_OK):
            raise ValueError("Selected provider key is missing or unreadable by the API UID.")
        return None
    module.resolve_provider_roles = lambda env, **kwargs: original(
        env, **{**kwargs, "key_reader": key_metadata})
    module.Config.load()
    print("PASS: candidate provider/origin/budget semantics, network disabled, zero key reads")


def main():
    mode = sys.argv[1]
    if mode == "select": select()
    elif mode == "init-stage": init_stage()
    elif mode == "init-install": init_install()
    elif mode == "env-keys": print("\n".join(environment_keys()))
    elif mode == "config": validate_config()
    elif mode == "resources": check_resources()
    elif mode == "bind-resources": bind_resources()
    elif mode == "adopt":
        bind_resources()
        update(phase="ready", bootstrap_pending=False, source_hashes={},
               active_images={name: containers()[name]["Image"] for name in APPS},
               active_version=environment(read_env())["STACK_VERSION"])
    elif mode == "snapshot": snapshot()
    elif mode == "plan": plan()
    elif mode == "cutover": cutover()
    elif mode == "verify": verify()
    elif mode == "smoke": smoke()
    elif mode == "status": status()
    elif mode == "model-plan":
        models = local_models(config()["services"]["assistant-api"]["environment"])
        if not models:
            raise ValueError("No selected role uses this local Ollama. No model was downloaded.")
        (WORK / "models.txt").write_text("\n".join(models) + "\n")
    elif mode == "semantic": semantic()
    elif mode == "prepare-static":
        # Only this helper's generated static copy, never credentials/backups/data.
        directory = WORK / "static"
        if directory.exists():
            if directory.is_symlink():
                raise ValueError("Refusing a symlink generated-static directory.")
            shutil.rmtree(directory)
        directory.mkdir()
    elif mode == "idle":
        counts = load(WORK / "counts.json")
        if counts != [0, 0, 0, 0]:
            raise ValueError("Waiting for queued/running captures, actions and chat turns to drain.")
    elif mode == "backup":
        path = DEPLOY / "backups" / sys.argv[2]
        with tarfile.open(path, "r:gz") as archive:
            roots = {Path(m.name).parts[0] for m in archive.getmembers()}
            if not {"homebox", "assistant", "proxy-data", "proxy-config", "secrets", "config.env"}.issubset(roots):
                raise ValueError("Backup is missing required state.")
        update(backup={"file": path.name, "bytes": path.stat().st_size, "sha256": file_digest(path)})
    elif mode == "phase": update(phase=sys.argv[2])
    elif mode == "model-download-begin": update(model_download_pending=True)
    elif mode == "model-download-end": update(model_download_pending=False)
    elif mode == "finish":
        state = journal()
        update(phase="ready", source_hashes=state["candidate_source_hashes"],
               active_images=state["candidate_images"], active_version=environment(read_env())["STACK_VERSION"])
    elif mode == "bootstrap-begin":
        if not journal().get("bootstrap_pending"):
            raise ValueError("Bootstrap is only for an unfinished fresh managed installation. Use invitations for existing users.")
        if load(STATE / "bootstrap-attempt.json", {}).get("state") in ("started", "created"):
            raise ValueError("An earlier signup may have committed. Verify that account instead of creating another owner.")
        body = read_env()
        update(registration_restore=body)
        write_environment(replace_setting(body, "HBOX_ALLOW_REGISTRATION", "true"))
    elif mode == "bootstrap-restore": restore_registration()
    elif mode == "bootstrap-image": verify_recovery_image()
    elif mode == "bootstrap-verify": verify_registration()
    elif mode == "bootstrap-restored": finish_registration_restore()
    elif mode == "bootstrap-done": update(bootstrap_pending=False)
    else: raise ValueError("Unknown internal lifecycle mode.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, tarfile.TarError, ssl.SSLError) as error:
        print("Lifecycle check failed: " + str(error), file=sys.stderr)
        raise SystemExit(1) from None
