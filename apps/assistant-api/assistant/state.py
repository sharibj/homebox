import asyncio
import fcntl
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path


class State:
    def __init__(self, path: Path, instance_id: str):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path
        self.locks = path.parent / (path.name + ".locks")
        self.locks.mkdir(mode=0o700, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (
                    name TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, token TEXT NOT NULL, actor TEXT NOT NULL,
                    collection TEXT NOT NULL, csrf TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, actor TEXT NOT NULL,
                    collection TEXT NOT NULL, data TEXT NOT NULL, status TEXT NOT NULL,
                    expires REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS captures (
                    id TEXT PRIMARY KEY, session TEXT NOT NULL, collection TEXT NOT NULL,
                    filename TEXT NOT NULL, expires REAL NOT NULL, action_id TEXT
                );
                CREATE TABLE IF NOT EXISTS mappings (
                    id TEXT PRIMARY KEY, collection TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
                    collection TEXT NOT NULL, action_id TEXT NOT NULL,
                    event TEXT NOT NULL, time REAL NOT NULL, data TEXT NOT NULL
                );
            """)
            bound = db.execute("SELECT value FROM metadata WHERE name='homebox_instance_id'").fetchone()
            if bound and bound["value"] != instance_id:
                raise RuntimeError("Assistant state belongs to a different Homebox instance. Restore matching data and HOMEBOX_INSTANCE_ID.")
            if not bound:
                has_data = any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                               for table in ("sessions", "actions", "captures", "mappings", "audit"))
                if has_data:
                    raise RuntimeError("Existing assistant state has no Homebox instance binding. Do not adopt it without verifying its source.")
                db.execute("INSERT INTO metadata VALUES('homebox_instance_id',?)", (instance_id,))
            # A killed process must not silently repeat a possibly committed write.
            rows = db.execute("SELECT id,data,collection FROM actions WHERE status='running'").fetchall()
            for row in rows:
                with (self.locks / row["collection"]).open("a") as lock:
                    try:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        continue  # Another worker still owns this live operation.
                    try:
                        current = db.execute(
                            "SELECT data FROM actions WHERE id=? AND status='running'", (row["id"],),
                        ).fetchone()
                        if not current:
                            continue
                        data = json.loads(current["data"])
                        data["status"] = "failed" if data.get("_createdId") else "uncertain"
                        data["retryable"] = bool(data.get("_createdId") and data.get("_enriched"))
                        data["error"] = "Execution was interrupted. Review the saved record before retrying."
                        db.execute("UPDATE actions SET status=?,data=? WHERE id=?",
                                   (data["status"], json.dumps(data), row["id"]))
                    finally:
                        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        os.chmod(path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def one(self, query, args=()):
        with self.connect() as db:
            row = db.execute(query, args).fetchone()
            return dict(row) if row else None

    def all(self, query, args=()):
        with self.connect() as db:
            return [dict(r) for r in db.execute(query, args).fetchall()]

    def execute(self, query, args=()):
        with self.connect() as db:
            return db.execute(query, args).rowcount

    def save_action(self, action):
        self.execute("UPDATE actions SET data=?,status=? WHERE id=?",
                     (json.dumps(action), action["status"], action["id"]))

    @asynccontextmanager
    async def collection_lock(self, collection):
        # flock is shared across workers/containers mounting the same local
        # SQLite state volume. Kernel ownership is released on process death.
        with (self.locks / collection).open("a") as lock:
            while True:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    await asyncio.sleep(.02)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def audit(self, session, action, event):
        data = {key: action.get(key) for key in ("type", "payload", "before", "result", "error")}
        self.execute(
            "INSERT INTO audit(actor,collection,action_id,event,time,data) VALUES(?,?,?,?,?,?)",
            (session["actor"], session["collection"], action["id"], event, time.time(), json.dumps(data)),
        )
