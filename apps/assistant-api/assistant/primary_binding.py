"""One-time original-account binding, available only to a trusted local operator.

There is deliberately no HTTP enrollment or reassignment route. Native account
creation ordering must be checked by the deployment helper, and its original
account provenance must be confirmed by the operator before this command runs.
"""

import json
import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID


BINDING_NAME = "installation_original_account"


def can_manage_ai_settings(state, session):
    user_id = session.get("user", {}).get("id")
    if not user_id or user_id != session.get("actor"):
        return False
    row = state.one("SELECT value FROM metadata WHERE name=?", (BINDING_NAME,))
    if not row:
        return False
    try:
        binding = json.loads(row["value"])
        instance = state.one("SELECT value FROM metadata WHERE name='homebox_instance_id'")
        return (binding["version"] == 1 and binding["instanceId"] == instance["value"]
                and binding["userId"] == user_id
                and binding["provenance"] == "local-operator-confirmed-native-earliest-v1")
    except (ValueError, KeyError, TypeError):
        return False


def bind_original(state, proof):
    """Pin an operator-proven native identity. Existing different pins never move."""
    try:
        if (set(proof) != {"instanceId", "userId", "createdAt", "nativeProofVersion",
                          "operatorConfirmedOriginal"}
                or type(proof["nativeProofVersion"]) is not int or proof["nativeProofVersion"] != 1
                or proof["operatorConfirmedOriginal"] is not True):
            raise ValueError
        instance_id = str(UUID(proof["instanceId"]))
        user_id = str(UUID(proof["userId"]))
        created_at = datetime.fromisoformat(proof["createdAt"].replace("Z", "+00:00"))
        if created_at.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError("Original-account proof is missing or invalid. No binding was changed.") from None
    with state.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        instance = db.execute("SELECT value FROM metadata WHERE name='homebox_instance_id'").fetchone()
        if not instance or instance["value"] != instance_id:
            raise ValueError("Original-account proof belongs to another installation. No binding was changed.")
        existing = db.execute("SELECT value FROM metadata WHERE name=?", (BINDING_NAME,)).fetchone()
        if existing:
            try:
                previous = json.loads(existing["value"])
                same = (previous["version"] == 1 and previous["instanceId"] == instance_id
                        and previous["userId"] == user_id
                        and previous["createdAt"] == created_at.isoformat()
                        and previous["provenance"] == "local-operator-confirmed-native-earliest-v1")
            except (ValueError, KeyError, TypeError):
                same = False
            if not same:
                raise ValueError("An original-account binding already exists. It cannot be reassigned.")
            return False
        binding = {"version": 1, "instanceId": instance_id, "userId": user_id,
                   "createdAt": created_at.isoformat(),
                   "boundAt": datetime.now(timezone.utc).isoformat(),
                   "provenance": "local-operator-confirmed-native-earliest-v1"}
        db.execute("INSERT INTO metadata(name,value) VALUES(?,?)", (BINDING_NAME, json.dumps(binding)))
        return True


class _ExistingState:
    def __init__(self, path, *, readonly=False):
        self.path = Path(path)
        self.mode = "ro" if readonly else "rw"
        if self.path.is_symlink() or not self.path.is_file():
            raise ValueError("Existing assistant state is required. No database was created.")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path.resolve().as_uri() + "?mode=" + self.mode, uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()


def binding_info(state, instance_id):
    with state.connect() as db:
        instance = db.execute("SELECT value FROM metadata WHERE name='homebox_instance_id'").fetchone()
        if not instance or instance["value"] != instance_id:
            raise ValueError("Assistant state belongs to another installation.")
        row = db.execute("SELECT value FROM metadata WHERE name=?", (BINDING_NAME,)).fetchone()
    if not row:
        return {"bound": False, "instanceId": instance_id}
    binding = json.loads(row["value"])
    if (binding["version"] != 1 or binding["instanceId"] != instance_id
            or binding["provenance"] != "local-operator-confirmed-native-earliest-v1"):
        raise ValueError("Existing original-account binding is invalid.")
    user_id = str(UUID(binding["userId"]))
    created_at = datetime.fromisoformat(binding["createdAt"])
    if created_at.utcoffset() is None:
        raise ValueError("Existing original-account timestamp is invalid.")
    return {"bound": True, "instanceId": instance_id, "userId": user_id, "createdAt": binding["createdAt"]}


def main():
    try:
        instance_id = str(UUID(os.environ["HOMEBOX_INSTANCE_ID"]))
        path = os.environ.get("ASSISTANT_STATE_PATH", "/data/assistant.db")
        if sys.argv[1:] == ["--show"]:
            print(json.dumps(binding_info(_ExistingState(path, readonly=True), instance_id)))
            return
        if sys.argv[1:]:
            raise ValueError("Only --show is accepted; provisioning uses trusted stdin.")
        data = sys.stdin.buffer.read(8193)
        if len(data) > 8192:
            raise ValueError("Original-account proof is too large.")
        proof = json.loads(data)
        if proof["instanceId"] != instance_id:
            raise ValueError("Original-account proof belongs to another installation.")
        changed = bind_original(_ExistingState(path), proof)
        print("Original native account bound." if changed else "Original native account binding is unchanged.")
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error):
        print("Original-account binding refused. Check the installation and trusted original-account proof.",
              file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
