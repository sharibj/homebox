"""Offline, disposable-volume compatibility check, run by current then old image."""
import asyncio
import json
import sys
import time
from pathlib import Path

import conftest  # Disposable test configuration only.
from assistant.actions import Actions
from assistant.config import Config
from assistant.state import State


class NoInventory:
    async def tool(self, *args, **kwargs):
        raise AssertionError("Rollback compatibility must not dispatch inventory calls")


def main():
    mode, root = sys.argv[1], Path(sys.argv[2])
    cfg = Config.load()
    cfg.state, cfg.uploads = root / "assistant.db", root / "uploads"
    cfg.uploads.mkdir(exist_ok=True)
    store = State(cfg.state, cfg.instance_id)
    session = {"id": "synthetic-session", "actor": "synthetic-actor",
               "collection": "b2222222-2222-4222-8222-222222222222", "credential": "synthetic-unused"}
    action_id = "a_synthetic_uncertain"
    if mode == "prepare":
        store.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)",
                      (session["id"], "synthetic-encrypted-placeholder", session["actor"], session["collection"], "synthetic-csrf", time.time() + 3600))
        action = {"id": action_id, "type": "item.create", "revision": 1, "status": "uncertain",
                  "summary": "Synthetic migration fixture", "payload": {"name": "Synthetic fixture"},
                  "before": None, "after": {"name": "Synthetic fixture", "photoIncluded": True},
                  "result": None, "error": "Synthetic interrupted response", "retryable": False,
                  "_key": action_id + ":1", "_captureId": "c_synthetic"}
        store.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?)", (
            action_id, session["id"], session["actor"], session["collection"], json.dumps(action), "uncertain", time.time() + 900))
        (cfg.uploads / "synthetic.jpg").write_bytes(b"synthetic-retained-photo")
        store.execute("INSERT INTO captures VALUES(?,?,?,?,?,?)",
                      ("c_synthetic", session["id"], session["collection"], "synthetic.jpg", time.time() + 3600, action_id))
        context = store.chat_context(session)
        encrypted = cfg.cipher.encrypt(b'{"draft":null,"pending":null,"pages":{}}').decode()
        store.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?)", (
            "cv_synthetic", session["id"], session["actor"], session["collection"], cfg.instance_id, context["version"], 0, time.time() + 1800, encrypted))
        store.execute("INSERT INTO chat_turns VALUES(?,?,?,?,?,?,?)",
                      ("turn_synthetic", "cv_synthetic", "synthetic-key", "synthetic-fingerprint", "running", None, time.time()))
        store.execute("INSERT INTO chat_action_links VALUES(?,?,?)", ("turn_synthetic", "cv_synthetic", action_id))
        print(json.dumps({"status": "prepared", "legacy_tables_unchanged": True}))
    elif mode == "probe":
        actions = Actions(cfg, store, NoInventory())
        _, action = actions.load(session, action_id)
        assert action["status"] == "uncertain" and action["retryable"] is False
        result = asyncio.run(actions.confirm(session, action_id, 1, action_id + ":1"))
        assert result["status"] == "uncertain"
        assert len(store.one("SELECT * FROM captures")) == 6
        assert (cfg.uploads / "synthetic.jpg").read_bytes() == b"synthetic-retained-photo"
        assert store.one("SELECT action_id FROM chat_action_links")["action_id"] == action_id
        assert store.one("SELECT count(*) AS n FROM conversations")["n"] == 1
        print(json.dumps({"status": "passed", "uncertain_action": "preserved", "photo": "preserved",
                          "chat_tables": "preserved", "inventory_calls": 0}))
    else:
        raise ValueError("Use prepare or probe")


if __name__ == "__main__":
    main()
