"""Read-only native evidence and explicit original-account attestation."""
import datetime as dt
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys
import uuid


class GuardRefusal(ValueError):
    """A fixed public reason, never a private exception or native row."""
    def __init__(self, code):
        self.code = code
        super().__init__(code)


GUARD_REASONS = {
    "installation-receipt": "The canonical installation owner/resource receipts are incomplete or do not match.",
    "deployment-not-ready": "The deployment journal is not ready. Review the unfinished deployment before creating a member.",
    "pending-operation": "A registration, model or access operation is still pending. Complete its reviewed recovery first.",
    "service-health": "All six original services must be running and healthy.",
    "resource-identity": "The original volume, mount, key or installation identity could not be verified.",
    "configuration": "The protected deployment configuration does not match the running installation.",
    "native-volume": "The original native data volume could not be identified safely.",
    "native-user": "The native container must declare a numeric UID/GID for the read-only database helper.",
}


def guarded(code, check):
    try:
        return check()
    except (ValueError, KeyError, OSError):
        raise GuardRefusal(code) from None


def refusal(argv, error):
    mode = argv[0] if argv else ""
    if mode in ("users-preflight", "users-recheck", "native-owners"):
        if isinstance(error, GuardRefusal):
            code, reason = error.code, GUARD_REASONS[error.code]
        elif mode == "native-owners":
            sqlite_code = getattr(error, "sqlite_errorcode", 0)
            sqlite_code = sqlite_code & 0xff if isinstance(sqlite_code, int) else 0
            if sqlite_code == sqlite3.SQLITE_ERROR:
                code = "native-owner-schema"
                reason = "The native household ownership schema could not be queried. Verify the running native Homebox version."
            else:
                code = "native-owner-read"
                reason = "Native household ownership could not be read safely. Check the native data volume, UID/GID and live WAL access."
        else:
            code, reason = "installation-check", "The installation readiness/identity check could not be completed safely."
        suffix = (" Account creation may already have completed; review its private receipt before retrying."
                  if mode == "users-recheck" else "")
        return ("New-member creation refused [" + code + "]: " + reason + suffix
                + " AI-primary binding is not a prerequisite for creating a member.")
    return ("Primary-account operation refused. Check canonical ready state, unique native creation evidence, "
            "the existing binding and explicit original-account UUID; no reassignment is allowed.")


def canonical_uuid(value):
    if not isinstance(value, str) or str(uuid.UUID(value)) != value or uuid.UUID(value).int == 0:
        raise ValueError("A canonical, nonzero UUID is required.")
    return value


def timestamp(value):
    """Order native nanoseconds exactly; the binding contract uses microseconds."""
    if not isinstance(value, str):
        raise ValueError("Native creation timestamps must be timezone-aware strings.")
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", value)
    if not match:
        raise ValueError("Native creation timestamps are missing or malformed.")
    day, clock, fraction, offset = match.groups()
    base = dt.datetime.fromisoformat(day + "T" + clock + ("+00:00" if offset == "Z" else offset))
    base = base.astimezone(dt.timezone.utc)
    delta = base - dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
    nanos = int((fraction or "").ljust(9, "0"))
    order = ((delta.days * 86400 + delta.seconds), nanos)
    canonical = base.replace(microsecond=nanos // 1000).isoformat(timespec="microseconds")
    return order, canonical


def native_rows(path, query):
    if path.is_symlink() or not path.is_file():
        raise ValueError("The original native SQLite database is missing or unsafe.")
    # Never use immutable=1 on a running WAL database: it can ignore live WAL data.
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        rows = connection.execute(query).fetchall()
    return rows


def native_owners(path):
    rows = native_rows(path, "SELECT ug.user_id,g.id,g.name FROM user_groups ug "
                       "JOIN groups g ON g.id=ug.group_id WHERE ug.role='owner' ORDER BY g.id")
    result = {}
    for actor, household, name in rows:
        canonical_uuid(actor)
        canonical_uuid(household)
        if not isinstance(name, str) or not name or any(x["id"] == household for x in result.get(actor, [])):
            raise ValueError("Native household ownership evidence is malformed.")
        result.setdefault(actor, []).append({"id": household, "name": name})
    return result


def native_candidate(path, instance):
    canonical_uuid(instance)
    rows = native_rows(path, "SELECT id,created_at FROM users")
    if not rows:
        raise ValueError("No current native user exists; original-account evidence is absent.")
    users = []
    ids = set()
    for identifier, created in rows:
        canonical_uuid(identifier)
        if identifier in ids:
            raise ValueError("Native user identity evidence is ambiguous.")
        ids.add(identifier)
        order, canonical = timestamp(created)
        users.append((order, identifier, canonical))
    users.sort()
    if len(users) > 1 and users[0][0] == users[1][0]:
        raise ValueError("The earliest current native creation timestamp is tied; refusing to guess.")
    return {"instanceId": instance, "userId": users[0][1], "createdAt": users[0][2], "nativeProofVersion": 1}


def private_json(m, path):
    if path.is_symlink():
        raise ValueError("Refusing a symlink primary-account receipt.")
    if not path.exists():
        return None
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077 or info.st_uid != os.getuid():
        raise ValueError("Primary-account receipts must remain owned private regular files.")
    value = m.load(path)
    if not isinstance(value, dict):
        raise ValueError("Primary-account receipts must contain a JSON object.")
    return value


def save(m, path, value):
    m.save(path, value)
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def guard(m):
    state = m.journal()
    owner = m.installation_owner()
    if (not owner or not owner.get("established") or owner.get("binding") != m.selected()
            or state.get("binding") != m.selected() or not state.get("resources")):
        raise GuardRefusal("installation-receipt")
    if state.get("phase") != "ready":
        raise GuardRefusal("deployment-not-ready")
    if (state.get("registration_restore") is not None
            or state.get("model_download_pending") or state.get("access_change", {}).get("status") == "pending"):
        raise GuardRefusal("pending-operation")
    current = m.containers()
    if set(current) != set(m.SERVICES) or any(not x["State"].get("Running")
            or x["State"].get("Health", {}).get("Status") != "healthy" for x in current.values()):
        raise GuardRefusal("service-health")
    guarded("resource-identity", m.check_resources)
    guarded("configuration", m.validate_config)
    return current


def identity(item):
    mounts = {value["Destination"]: value for value in item["Mounts"]}
    if len(mounts) != len(item["Mounts"]):
        raise ValueError("Duplicate native/runtime mount destinations are unsafe.")
    return {key: item[key] for key in ("Id", "Image", "HostConfig", "Config")} | {
        "Mounts": mounts,
        "StartedAt": item["State"]["StartedAt"],
        "networks": {key: value["NetworkID"] for key, value in item["NetworkSettings"]["Networks"].items()}}


def unchanged(m):
    current = guard(m)
    context = private_json(m, m.WORK / "primary-context.json")
    if (not context or context["binding"] != m.selected() or context["owner"] != m.installation_owner()
            or context["environment"] != m.file_digest(m.DEPLOY / ".env")
            or context["sources"] != sources(m)
            or context["runtime"] != {key: identity(value) for key, value in current.items()}):
        raise ValueError("Installation identity or configuration changed; no primary-account write is allowed.")
    return context


def sources(m):
    names = ("deploy/homebox", "deploy/lifecycle/metadata.py", "deploy/lifecycle/primary.py",
             "deploy/lifecycle/primary.sh", "deploy/lifecycle/create-user.sh",
             "deploy/lifecycle/accounts.py", "deploy/lifecycle/guided_users.py")
    return {name: m.file_digest(m.SOURCE / name) for name in names}


def binding(m, instance):
    value = m.load(m.WORK / "primary-binding.json")
    if not isinstance(value, dict) or value.get("instanceId") != instance or type(value.get("bound")) is not bool:
        raise ValueError("The API did not return a valid instance-bound primary-account status.")
    if not value["bound"]:
        if set(value) != {"bound", "instanceId"}:
            raise ValueError("Unexpected unbound primary-account status.")
        return None
    if set(value) != {"bound", "instanceId", "userId", "createdAt"}:
        raise ValueError("Unexpected bound primary-account status.")
    canonical_uuid(value["userId"])
    _, created = timestamp(value["createdAt"])
    # API datetime.isoformat() omits an all-zero fractional part. Compare the
    # normalized UTC instant, not that harmless serialization difference.
    return {"instanceId": instance, "userId": value["userId"], "createdAt": created, "nativeProofVersion": 1}


def review(m):
    context = unchanged(m)
    candidate = m.load(m.WORK / "primary-candidate.json")
    if not isinstance(candidate, dict) or set(candidate) != {"instanceId", "userId", "createdAt", "nativeProofVersion"}:
        raise ValueError("Native creation evidence is unavailable.")
    if candidate["instanceId"] != context["instanceId"] or type(candidate["nativeProofVersion"]) is not int or candidate["nativeProofVersion"] != 1:
        raise ValueError("Native creation evidence belongs to another instance or version.")
    canonical_uuid(candidate["userId"])
    if timestamp(candidate["createdAt"])[1] != candidate["createdAt"]:
        raise ValueError("Native creation proof is not canonical.")
    existing = binding(m, context["instanceId"])
    receipt = private_json(m, m.STATE / "ai-primary.json")
    if existing and existing != candidate:
        raise ValueError("The pinned original account differs or was deleted. Reassignment is forbidden.")
    if receipt:
        previous = {key: receipt.get(key) for key in candidate}
        if previous != candidate or (receipt.get("status") == "complete" and not existing):
            raise ValueError("The previous original-account binding is missing or changed. Reassignment is forbidden.")
    return candidate


def main(argv):
    if argv[0] == "native":
        print(json.dumps(native_candidate(Path("/native/homebox.db"), argv[1]), sort_keys=True))
        return
    if argv[0] == "native-owners":
        print(json.dumps(native_owners(Path("/native/homebox.db")), sort_keys=True))
        return
    import metadata as m
    mode = argv[0]
    if mode in ("preflight", "users-preflight"):
        current = guard(m)
        instance = canonical_uuid(m.environment(m.read_env())["HOMEBOX_INSTANCE_ID"])
        native = next(x for x in current["homebox"]["Mounts"] if x["Destination"] == "/data")
        if native["Type"] != "volume":
            raise GuardRefusal("native-volume")
        native_user = current["homebox"]["Config"].get("User", "")
        if not re.fullmatch(r"\d+(?::\d+)?", native_user):
            raise GuardRefusal("native-user")
        save(m, m.WORK / "primary-context.json", {"binding": m.selected(), "owner": m.installation_owner(),
            "environment": m.file_digest(m.DEPLOY / ".env"), "instanceId": instance,
            "sources": sources(m),
            "runtime": {key: identity(value) for key, value in current.items()}})
        for name, value in (("volume", native["Name"]), ("api", current["assistant-api"]["Id"]),
                            ("instance", instance), ("native-user", native_user),
                            ("native", current["homebox"]["Id"])):
            (m.WORK / ("primary-" + name)).write_text(value + "\n")
    elif mode in ("review", "attest"):
        candidate = review(m)
        if mode == "review":
            print("Earliest CURRENT native account: " + candidate["userId"])
            print("Created at: " + candidate["createdAt"])
            print("Current ordering does NOT prove ORIGINAL ownership: an earlier user may have been deleted.")
            print("Verify the original account's provenance, then explicitly pass --confirm-original-user-id UUID.")
            print("Global --yes is not an original-account attestation.")
            return
        if len(argv) != 2 or canonical_uuid(argv[1]) != candidate["userId"]:
            raise ValueError("Explicit original-account UUID must match the unique current earliest native user.")
        proof = {**candidate, "operatorConfirmedOriginal": True}
        save(m, m.STATE / "ai-primary.json", {**proof, "status": "pending"})
        save(m, m.WORK / "primary-proof.json", proof)
    elif mode in ("recheck", "users-recheck"):
        unchanged(m)
    elif mode == "complete":
        unchanged(m)
        proof = private_json(m, m.WORK / "primary-proof.json")
        actual = binding(m, proof["instanceId"])
        if not actual or actual != {key: proof[key] for key in actual}:
            raise ValueError("Primary binding was not verified. Keep the original identity; do not reassign.")
        save(m, m.STATE / "ai-primary.json", {**proof, "status": "complete"})
        print("Verified the explicitly attested original account. No native account, image, configuration or provider was changed.")
    else:
        raise ValueError("Unknown primary-account operation.")


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, KeyError, IndexError, OSError, sqlite3.Error) as error:
        # Native values and database diagnostics must not become log output.
        print(refusal(sys.argv[1:], error), file=sys.stderr)
        raise SystemExit(1) from None
