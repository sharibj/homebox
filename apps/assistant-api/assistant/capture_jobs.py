import asyncio
import fcntl
import hashlib
import json
import logging
import os
import secrets
import time
from contextlib import contextmanager, suppress
from datetime import datetime, timezone

from .clients import ServiceError
from .provider_runtime import operation_for, snapshot_for, vision_configuration

MODEL_LOCK = "_photo-analysis"
ACTIVE = {"queued", "running"}


def timestamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat() if value is not None else None


def draft_result(capture_id, answer=None, warning=None):
    item = answer.items[0] if answer else None
    warnings = [warning or "Model output is a draft. Check every field before saving."]
    if item and item.confidence < .6:
        warnings.append("The model is uncertain about this identification.")
    if answer and len(answer.items) > 1:
        warnings.append("Several objects were detected. This draft contains only the first; photograph the others separately.")
    return {
        "captureId": capture_id,
        "draft": {"name": item.name if item else "", "category": item.category if item else None,
                  "manufacturer": (item.manufacturer or "") if item else "",
                  "model": (item.model or "") if item else "", "serialNumber": (item.serialNumber or "") if item else "",
                  "quantity": item.quantity_estimate if item else 1, "notes": answer.image_notes if answer else "",
                  "tags": item.suggested_tags if item else []},
        "duplicates": [], "locations": [], "warnings": warnings,
    }


class CaptureJobs:
    def __init__(self, config, state, clients, vision):
        self.cfg, self.state, self.clients, self.vision = config, state, clients, vision
        self.wake = asyncio.Event()

    def configuration(self):
        return vision_configuration(snapshot_for(self.vision, self.cfg))

    def persist_photo(self, session, normalized):
        capture_id = "c_" + secrets.token_urlsafe(24)
        filename = capture_id + ".jpg"
        path = self.cfg.uploads / filename
        try:
            with path.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(normalized)
                output.flush()
                os.fsync(output.fileno())
            directory = os.open(self.cfg.uploads, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
            return capture_id, filename
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def submit(self, session, key, normalized, hint, location_id):
        with operation_for(self.vision):
            return self._submit(session, key, normalized, hint, location_id)

    def _submit(self, session, key, normalized, hint, location_id):
        fingerprint = hashlib.sha256(normalized + json.dumps(
            {"hint": hint, "locationId": location_id}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        capture_id = filename = None
        try:
            with self.state.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                existing = db.execute("SELECT * FROM capture_jobs WHERE session=? AND submission_key=?",
                                      (session["id"], key)).fetchone()
                if existing:
                    if (existing["actor"] != session["actor"] or existing["collection"] != session["collection"]
                            or existing["instance"] != self.cfg.instance_id or existing["fingerprint"] != fingerprint):
                        raise ServiceError("idempotency_conflict", "This submission key already belongs to a different photo or collection.", 409)
                    return dict(existing)
                capture_id, filename = self.persist_photo(session, normalized)
                now = time.time()
                expires = now + self.cfg.capture_ttl
                db.execute("INSERT INTO captures VALUES(?,?,?,?,?,NULL)",
                           (capture_id, session["id"], session["collection"], filename, expires))
                full = db.execute("SELECT count(*) FROM capture_jobs WHERE status='queued'").fetchone()[0] >= self.cfg.analysis_queue_limit
                warning = "Photo analysis is busy and its queue is full. Your photo is retained; enter the details manually."
                status = "manual" if full else "queued"
                db.execute(
                    "INSERT INTO capture_jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("j_" + secrets.token_urlsafe(24), session["id"], session["actor"], session["collection"],
                     self.cfg.instance_id, key, fingerprint, capture_id, self.cfg.cipher.encrypt(hint.encode()).decode(),
                     location_id, status, now, None, now if full else None, expires,
                     json.dumps(draft_result(capture_id, warning=warning)) if full else None, None,
                     json.dumps(["analysis_queue_full"] if full else []), self.configuration()),
                )
                row = dict(db.execute("SELECT * FROM capture_jobs WHERE session=? AND submission_key=?",
                                      (session["id"], key)).fetchone())
            self.wake.set()
            return row
        except BaseException:
            if filename:
                (self.cfg.uploads / filename).unlink(missing_ok=True)
            raise

    def public(self, row):
        status = row["status"]
        result = json.loads(row["result"]) if row["result"] else None
        error = json.loads(row["error"]) if row["error"] else None
        codes = json.loads(row["warning_codes"])
        if result is not None and status not in ACTIVE:
            capture = self.state.one(
                "SELECT filename,expires FROM captures WHERE id=? AND session=? AND collection=?",
                (row["capture_id"], row["session"], row["collection"]),
            )
            now = time.time()
            available = capture is not None and capture["expires"] > now and row["expires"] > now
            if available:
                try:
                    available = (self.cfg.uploads / capture["filename"]).is_file()
                except OSError:
                    available = False
            if not available:
                # A completed action can consume the photo before its job's
                # deduplication tombstone expires. Never resurrect that draft.
                status, result, codes = "expired", None, []
                error = {"code": "capture_expired", "message": "This photo is no longer available. Take a new photo."}
        return {
            "id": row["id"], "status": status, "submissionKey": row["submission_key"],
            "captureId": row["capture_id"], "createdAt": timestamp(row["created"]),
            "startedAt": timestamp(row["started"]), "finishedAt": timestamp(row["finished"]),
            "expiresAt": timestamp(row["expires"]), "result": result,
            "error": error, "warningCodes": codes,
        }

    def load(self, session, job_id):
        row = self.state.one(
            "SELECT * FROM capture_jobs WHERE id=? AND session=? AND actor=? AND collection=? AND instance=?",
            (job_id, session["id"], session["actor"], session["collection"], self.cfg.instance_id),
        )
        if not row:
            raise ServiceError("capture_job_not_found", "Photo analysis not found in this session and collection.", 404)
        return row

    def finish(self, row, status, result=None, error=None, codes=None, expected="running"):
        self.state.execute(
            "UPDATE capture_jobs SET status=?,finished=?,result=?,error=?,warning_codes=? WHERE id=? AND status=?",
            (status, time.time(), json.dumps(result) if result else None, json.dumps(error) if error else None,
             json.dumps(codes or []), row["id"], expected),
        )

    def manual(self, row, code, message, expected="running"):
        self.finish(row, "manual", draft_result(row["capture_id"], warning=message), codes=[code], expected=expected)

    def refresh(self):
        now = time.time()
        for row in self.state.all("SELECT * FROM capture_jobs WHERE status='queued' AND created<=?",
                                  (now - self.cfg.analysis_queue_timeout,)):
            self.manual(row, "analysis_queue_timed_out",
                        "Photo analysis could not start within its queue time limit. Your photo is retained; enter the details manually.", expected="queued")
        self.state.execute(
            "UPDATE capture_jobs SET status='expired',result=NULL,error=NULL,warning_codes='[]',hint='' "
            "WHERE expires<=? AND status!='expired'", (now,),
        )
        # Keep opaque deduplication tombstones for the browser-session lifetime.
        self.state.execute("DELETE FROM capture_jobs WHERE expires<? AND session NOT IN (SELECT id FROM sessions)",
                           (now,))

    @contextmanager
    def try_model_lock(self):
        with (self.state.locks / MODEL_LOCK).open("a") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def local_context(self, row):
        session = self.state.one("SELECT * FROM sessions WHERE id=?", (row["session"],))
        if not session or session["expires"] <= time.time() or session["actor"] != row["actor"]:
            raise ServiceError("unauthorized", "This photo-analysis session ended. Sign in again.", 401)
        if session["collection"] != row["collection"]:
            raise ServiceError("COLLECTION_CONTEXT_CHANGED", "The selected collection changed during photo analysis.", 409)
        session["credential"] = self.cfg.cipher.decrypt(session["token"].encode()).decode()
        return session

    async def authorized_context(self, row):
        session = self.local_context(row)
        own = (await self.clients.homebox("GET", "/users/self", {**session, "collection": ""}))["item"]
        if own["id"] != row["actor"] or row["collection"] not in own["groupIds"]:
            raise ServiceError("collection_denied", "You no longer have access to this photo's collection.", 403)
        return session

    async def analyze_photo(self, session, capture_id, normalized, hint):
        with operation_for(self.vision):
            answer = await self.vision.analyze(normalized, hint)
            result = draft_result(capture_id, answer)
            item = answer.items[0]
            result["duplicates"] = (await self.clients.tool(session, "search_items", {"query": item.model or item.name}))["items"]
            result["locations"] = (await self.clients.tool(session, "list_locations"))["locations"]
            return result

    async def process(self, row):
        try:
            with operation_for(self.vision):
                if row["configuration"] != self.configuration():
                    self.manual(row, "analysis_configuration_changed",
                                "The configured photo-analysis service or model changed. Your photo was not sent to the new configuration; enter its details manually.")
                    return

                async def analyze():
                    session = await self.authorized_context(row)
                    capture = self.state.one("SELECT filename FROM captures WHERE id=?", (row["capture_id"],))
                    if not capture:
                        raise ServiceError("capture_expired", "The temporary photo expired. Take a new photo.", 410)
                    normalized = await asyncio.to_thread((self.cfg.uploads / capture["filename"]).read_bytes)
                    hint = self.cfg.cipher.decrypt(row["hint"].encode()).decode()
                    result = await self.analyze_photo(session, row["capture_id"], normalized, hint)
                    await self.authorized_context(row)
                    return result

                async def watch_context():
                    while True:
                        await asyncio.sleep(.5)
                        self.local_context(row)

                work = guard = None
                try:
                    async with asyncio.timeout(self.cfg.analysis_timeout):
                        work, guard = asyncio.create_task(analyze()), asyncio.create_task(watch_context())
                        done, _ = await asyncio.wait((work, guard), return_when=asyncio.FIRST_COMPLETED)
                        if guard in done:
                            await guard
                        result = await work
                        self.finish(row, "succeeded", result)
                except TimeoutError:
                    self.manual(row, "analysis_timed_out",
                                "Photo analysis reached its time limit. Your photo is retained; enter the details manually.")
                except ServiceError as exc:
                    if exc.status in {401, 403, 409} or exc.code == "capture_expired":
                        self.finish(row, "failed", error={"code": exc.code, "message": exc.message})
                    else:
                        self.manual(row, exc.code, exc.message)
                except (OSError, ValueError):
                    self.manual(row, "analysis_interrupted",
                                "Photo analysis was interrupted. If the photo is still available, enter its details manually.")
                finally:
                    tasks = [task for task in (work, guard) if task is not None]
                    for task in tasks:
                        task.cancel()
                    # Collect child cancellation/errors without suppressing cancellation
                    # of this worker while it awaits their cleanup.
                    await asyncio.gather(*tasks, return_exceptions=True)
        except ServiceError as exc:
            self.manual(row, exc.code, exc.message)

    async def step(self):
        self.refresh()
        with self.try_model_lock() as acquired:
            if not acquired:
                return False
            # Re-read only after locking. Another worker may have finished while
            # we waited; never overwrite a committed result with interruption.
            for row in self.state.all("SELECT * FROM capture_jobs WHERE status='running'"):
                self.manual(row, "analysis_interrupted",
                            "Photo analysis was interrupted by a restart. Your photo is retained; enter the details manually.")
            row = self.state.one("SELECT * FROM capture_jobs WHERE status='queued' ORDER BY created,rowid LIMIT 1")
            if not row:
                return False
            changed = self.state.execute("UPDATE capture_jobs SET status='running',started=? WHERE id=? AND status='queued'",
                                         (time.time(), row["id"]))
            if changed:
                await self.process(row)
            return bool(changed)

    async def run(self):
        while True:
            try:
                self.wake.clear()
                if await self.step():
                    continue
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.wake.wait(), .25)
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger(__name__).error("Photo-analysis worker encountered a local state error.")
                await asyncio.sleep(1)
