import asyncio
import hashlib
import hmac
import json
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .actions import Actions, public
from .clients import Clients, ServiceError
from .capture_jobs import CaptureJobs, MODEL_LOCK, draft_result
from .config import Config
from .conversations import Conversations
from .images import VisionClient, normalize
from .schemas import ActionRequest, Collection, Confirm, Login, MappingCreate, MappingUpdate, Message
from .state import State

PREFIX = "/api/assistant/v1"
COOKIE = "hb_assistant"
LEGACY_REQUEST_TIMEOUT = 150


class CaptureDeadline:
    """Cancel the entire legacy ASGI request, including middleware child tasks."""
    def __init__(self, app, chat_timeout=60):
        self.app = app
        self.chat_timeout = chat_timeout

    async def __call__(self, scope, receive, send):
        legacy = scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == PREFIX + "/captures"
        chat = scope["type"] == "http" and scope["method"] == "POST" and scope["path"] == PREFIX + "/chat"
        if not legacy and not chat:
            return await self.app(scope, receive, send)
        started = False

        async def track_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            async with asyncio.timeout(LEGACY_REQUEST_TIMEOUT if legacy else self.chat_timeout):
                await self.app(scope, receive, track_send)
        except TimeoutError:
            if started:
                return
            retained = scope.get("state", {}).get("legacy_capture_id")
            if chat:
                response = JSONResponse({"error": {"code": "chat_timed_out",
                                                    "message": "This turn reached its time limit. Recover the conversation before retrying."}},
                                        status_code=503)
            elif retained:
                response = JSONResponse(draft_result(
                    retained, warning="Photo preparation or analysis reached its request time limit. Your photo is retained; enter the details manually.",
                ))
            else:
                response = JSONResponse({"error": {
                    "code": "capture_request_timed_out",
                    "message": "Photo preparation reached its request time limit. No inventory change was made.",
                }}, status_code=504)
            response.headers["Cache-Control"] = "no-store"
            response.headers["X-Content-Type-Options"] = "nosniff"
            await response(scope, receive, send)


def create_app(config=None, clients=None):
    cfg = config or Config.load()
    state = State(cfg.state, cfg.instance_id)
    cfg.uploads.mkdir(mode=0o700, parents=True, exist_ok=True)
    services = clients or Clients(cfg)
    actions = Actions(cfg, state, services)
    vision = VisionClient(cfg)
    jobs = CaptureJobs(cfg, state, services, vision)
    conversations = Conversations(cfg, state, services, actions, vision)
    limits = defaultdict(deque)

    def cleanup():
        now = time.time()
        jobs.refresh()
        for capture in state.all(
            "SELECT c.* FROM captures c LEFT JOIN actions a ON c.action_id=a.id "
            "WHERE c.expires<? AND (a.id IS NULL OR a.status NOT IN ('running','failed'))", (now,),
        ):
            (cfg.uploads / capture["filename"]).unlink(missing_ok=True)
            state.execute("DELETE FROM captures WHERE id=?", (capture["id"],))
        state.execute("DELETE FROM sessions WHERE expires<?", (now,))
        conversations.cleanup()

    @asynccontextmanager
    async def lifespan(app):
        cleanup()
        worker = asyncio.create_task(jobs.run())
        chat_cleanup = asyncio.create_task(conversations.run_cleanup())
        try:
            yield
        finally:
            worker.cancel()
            chat_cleanup.cancel()
            with suppress(asyncio.CancelledError):
                await worker
            with suppress(asyncio.CancelledError):
                await chat_cleanup
            await vision.aclose()

    app = FastAPI(title="Homebox Companion", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config, app.state.store, app.state.clients = cfg, state, services
    app.state.actions, app.state.vision = actions, vision
    app.state.capture_jobs = jobs
    app.state.conversations = conversations

    def rate(request, bucket, maximum, window=60, actor=None):
        now = time.monotonic()
        key = (bucket, actor or (request.client.host if request.client else "unknown"))
        queue = limits[key]
        while queue and queue[0] < now - window:
            queue.popleft()
        if len(queue) >= maximum:
            raise ServiceError("rate_limited", "Too many requests. Please wait and try again.", 429)
        queue.append(now)
        if len(limits) > 2000:
            for old_key in list(limits):
                if not limits[old_key] or limits[old_key][-1] < now - 300:
                    del limits[old_key]

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        request.state.started_at = time.monotonic()
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") != cfg.origin:
                return JSONResponse({"error": {"code": "origin_denied", "message": "A same-origin request is required."}}, 403)
        length = request.headers.get("content-length")
        if length and (not length.isdecimal() or int(length) > cfg.max_upload + 131072):
            return JSONResponse({"error": {"code": "body_too_large", "message": "Request is too large."}}, 413)
        consumed = 0
        receive = request._receive

        async def bounded_receive():
            nonlocal consumed
            message = await receive()
            consumed += len(message.get("body", b""))
            if consumed > cfg.max_upload + 131072:
                raise ServiceError("body_too_large", "Request is too large.", 413)
            return message

        request._receive = bounded_receive
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    app.add_middleware(CaptureDeadline, chat_timeout=cfg.chat_timeout)

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, "details": exc.details}}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse({"error": {"code": "invalid_request", "message": "Check the request fields.", "details": {}}}, 422)

    def local_session(request: Request, allow_expired=False):
        raw = request.cookies.get(COOKIE, "")
        row = state.one("SELECT * FROM sessions WHERE id=?", (hashlib.sha256(raw.encode()).hexdigest(),))
        if row and not allow_expired and row["expires"] <= time.time():
            row = None
        if not row:
            raise ServiceError("unauthorized", "Sign in with your Homebox account.", 401)
        row["credential"] = cfg.cipher.decrypt(row["token"].encode()).decode()
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), row["csrf"]):
                raise ServiceError("csrf_denied", "Refresh the page before submitting this request.", 403)
        return row

    async def session(request: Request):
        row = local_session(request)
        self_response = await services.homebox("GET", "/users/self", {**row, "collection": ""})
        row["user"] = self_response["item"]
        recovering = request.url.path in {PREFIX + "/session", PREFIX + "/session/collection"}
        if not recovering and row["collection"] not in row["user"]["groupIds"]:
            raise ServiceError("collection_denied", "You no longer have access to this collection.", 403)
        if request.url.path not in {PREFIX + "/session", PREFIX + "/auth/logout"}:
            if request.headers.get("x-collection-id") != row["collection"]:
                raise ServiceError(
                    "COLLECTION_CONTEXT_CHANGED",
                    "The selected collection changed in another tab. Reload the session before continuing.",
                    409, {"sessionEndpoint": PREFIX + "/session"},
                )
        context = state.chat_context(row)
        supplied_version = request.headers.get("x-context-version")
        if supplied_version is not None and supplied_version != context["version"]:
            raise ServiceError("COLLECTION_CONTEXT_CHANGED", "The session context changed. Reload the session.", 409)
        row["_contextVersion"] = context["version"]
        return row

    async def session_response(value):
        groups = await services.homebox("GET", "/groups/all", {**value, "collection": ""})
        user = value["user"]
        context = state.chat_context(value)
        selected = next((g for g in groups if g["id"] == value["collection"]), None)
        return {
            "user": {k: user.get(k, "") for k in ("id", "name", "email")},
            "collections": [{"id": g["id"], "name": g["name"]} for g in groups],
            "collectionId": value["collection"], "csrfToken": value["csrf"],
            "scope": {"instanceId": cfg.instance_id, "instanceLabel": cfg.instance_label or "Homebox " + cfg.instance_id[:8],
                      "collectionId": value["collection"], "collectionName": selected["name"] if selected else "Unavailable collection",
                      "contextVersion": context["version"]},
            "ai": {"chat": {"provider": cfg.chat_provider_settings.kind, "model": cfg.chat_provider_settings.model},
                   "vision": {"provider": cfg.vision_provider_settings.kind, "model": cfg.vision_provider_settings.model}},
        }

    @app.get("/healthz")
    async def health():
        return {"status": "ok"}

    @app.get("/readyz")
    async def ready():
        try:
            state.one("SELECT 1")
            async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                hb = await client.get(cfg.homebox + "/api/v1/status")
                mcp = await client.get(cfg.mcp.removesuffix("/mcp") + "/readyz")
                hb.raise_for_status()
                mcp.raise_for_status()
            return {"status": "ready"}
        except Exception:
            return JSONResponse({"status": "unavailable"}, 503)

    @app.post(PREFIX + "/auth/login")
    async def login(body: Login, request: Request):
        rate(request, "login", 5, 60)
        cleanup()
        token = await services.homebox("POST", "/users/login", json=body.model_dump())
        temporary = {"credential": token["token"], "collection": ""}
        own = await services.homebox("GET", "/users/self", temporary)
        user = own["item"]
        temporary["collection"] = user["defaultGroupId"]
        temporary["user"] = user
        raw, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        sid = hashlib.sha256(raw.encode()).hexdigest()
        ttl = cfg.session_ttl
        if token.get("expiresAt"):
            expiry = datetime.fromisoformat(token["expiresAt"].replace("Z", "+00:00")).timestamp()
            ttl = min(ttl, max(0, int(expiry - time.time())))
        state.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?)", (
            sid, cfg.cipher.encrypt(token["token"].encode()).decode(),
            user["id"], temporary["collection"], csrf, time.time() + ttl,
        ))
        temporary["csrf"], temporary["id"] = csrf, sid
        response = JSONResponse(await session_response(temporary))
        response.set_cookie(COOKIE, raw, max_age=ttl, path=PREFIX, secure=cfg.secure,
                            httponly=True, samesite="strict")
        return response

    @app.get(PREFIX + "/session")
    async def get_session(s=Depends(session)):
        return await session_response(s)

    @app.post(PREFIX + "/auth/logout")
    async def logout(request: Request, background: BackgroundTasks):
        raw = request.cookies.get(COOKIE, "")
        existing = state.one("SELECT * FROM sessions WHERE id=?", (hashlib.sha256(raw.encode()).hexdigest(),))
        if existing:
            s = local_session(request, allow_expired=True)
            state.execute("UPDATE conversations SET expires=0 WHERE session=?", (s["id"],))
            state.execute("DELETE FROM chat_contexts WHERE session=?", (s["id"],))
            state.execute("DELETE FROM sessions WHERE id=?", (s["id"],))

            async def revoke():
                try:
                    await services.homebox("POST", "/users/logout", {**s, "collection": ""})
                except ServiceError:
                    pass
            background.add_task(revoke)
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIE, path=PREFIX, secure=cfg.secure, httponly=True, samesite="strict")
        return response

    @app.post(PREFIX + "/session/collection")
    async def select_collection(body: Collection, s=Depends(session)):
        group = str(body.collectionId)
        if group not in s["user"]["groupIds"]:
            raise ServiceError("collection_denied", "You do not have access to this collection.", 403)
        await services.homebox("GET", "/groups", {**s, "collection": group})
        with state.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state.require_context(s, db)
            db.execute("UPDATE sessions SET collection=? WHERE id=?", (group, s["id"]))
            db.execute("INSERT INTO chat_contexts VALUES(?,?,?,NULL) ON CONFLICT(session) DO UPDATE SET "
                       "collection=excluded.collection,version=excluded.version,conversation_id=NULL",
                       (s["id"], group, secrets.token_urlsafe(24)))
            db.execute("UPDATE conversations SET expires=0 WHERE session=?", (s["id"],))
        s["collection"] = group
        return await session_response(s)

    @app.get(PREFIX + "/locations")
    async def locations(s=Depends(session)):
        return await services.tool(s, "list_locations")

    @app.get(PREFIX + "/items")
    async def items(q: str = "", locationId: UUID | None = None, s=Depends(session)):
        if len(q) > 500:
            raise ServiceError("query_too_long", "Shorten your search.", 422)
        return await services.tool(s, "search_items", {"query": q, "location_id": str(locationId) if locationId else None})

    @app.get(PREFIX + "/entities/{entity_id}")
    async def entity(entity_id: UUID, s=Depends(session)):
        return await services.tool(s, "get_entity", {"entity_id": str(entity_id)})

    @app.post(PREFIX + "/actions")
    async def propose(body: ActionRequest, s=Depends(session)):
        return await actions.propose(s, body)

    @app.get(PREFIX + "/actions/{action_id}")
    async def get_action(action_id: str, s=Depends(session)):
        return public(actions.load(s, action_id)[1])

    @app.get(PREFIX + "/actions")
    async def recover_actions(s=Depends(session)):
        rows = state.all(
            "SELECT data FROM actions WHERE actor=? AND session=? AND collection=? ORDER BY rowid DESC LIMIT 50",
            (s["actor"], s["id"], s["collection"]),
        )
        return {"actions": [public(json.loads(row["data"])) for row in rows]}

    @app.post(PREFIX + "/actions/{action_id}/confirm")
    async def confirm(action_id: str, body: Confirm, request: Request, s=Depends(session)):
        key = request.headers.get("idempotency-key", "")
        if not 8 <= len(key) <= 128:
            raise ServiceError("idempotency_key_required", "Supply a stable retry key of 8 to 128 characters.", 400)
        return await actions.confirm(s, action_id, body.revision, key)

    @app.post(PREFIX + "/actions/{action_id}/cancel")
    async def cancel(action_id: str, s=Depends(session)):
        _, action = actions.load(s, action_id)
        if action["status"] != "proposed":
            raise ServiceError("action_not_cancellable", "Only an unexecuted proposal can be cancelled.", 409)
        action["status"] = "cancelled"
        with state.connect() as db:
            changed = db.execute(
                "UPDATE actions SET status=?,data=? WHERE id=? AND status='proposed'",
                ("cancelled", json.dumps(action), action_id),
            ).rowcount
            if not changed:
                raise ServiceError("action_running", "This action already started.", 409)
            db.execute(
                "UPDATE captures SET action_id=NULL WHERE action_id=? AND session=? AND collection=?",
                (action_id, s["id"], s["collection"]),
            )
        state.audit(s, action, "cancelled")
        return public(action)

    async def read_photo(image, hint, location_id, s):
        if len(hint) > 500:
            raise ServiceError("hint_too_long", "Keep the photo hint to 500 characters.", 422)
        if location_id:
            try:
                location_id = str(UUID(location_id))
            except ValueError:
                raise ServiceError("invalid_location", "Choose a valid location.", 422)
            await services.tool(s, "get_entity", {"entity_id": location_id})
        try:
            data = await image.read(cfg.max_upload + 1)
        finally:
            await image.close()
        if len(data) > cfg.max_upload:
            raise ServiceError("image_too_large", "The image exceeds the upload limit.", 413)
        normalized = await asyncio.to_thread(normalize, data, cfg)
        return normalized, location_id

    @app.post(PREFIX + "/capture-jobs")
    async def submit_capture_job(request: Request, image: UploadFile = File(...), hint: str = Form(""),
                                 locationId: str | None = Form(None), s=Depends(session)):
        key = request.headers.get("idempotency-key", "")
        if not 8 <= len(key) <= 128:
            raise ServiceError("idempotency_key_required", "Supply a stable retry key of 8 to 128 characters.", 400)
        cleanup()
        if not state.one("SELECT id FROM capture_jobs WHERE session=? AND submission_key=?", (s["id"], key)):
            rate(request, "capture", 4, 60, s["actor"])
        normalized, location_id = await read_photo(image, hint, locationId, s)
        row = jobs.submit(s, key, normalized, hint, location_id)
        return JSONResponse(jobs.public(row), status_code=202)

    @app.get(PREFIX + "/capture-jobs")
    async def recover_capture_jobs(submissionKey: str | None = None, s=Depends(session)):
        jobs.refresh()
        query = "SELECT * FROM capture_jobs WHERE actor=? AND session=? AND collection=? AND instance=?"
        args = [s["actor"], s["id"], s["collection"], cfg.instance_id]
        if submissionKey is not None:
            if not 8 <= len(submissionKey) <= 128:
                raise ServiceError("invalid_submission_key", "Supply a valid photo submission key.", 422)
            query += " AND submission_key=?"
            args.append(submissionKey)
        return {"jobs": [jobs.public(row) for row in state.all(query + " ORDER BY created DESC,rowid DESC LIMIT 20", args)]}

    @app.get(PREFIX + "/capture-jobs/{job_id}")
    async def get_capture_job(job_id: str, s=Depends(session)):
        jobs.refresh()
        return jobs.public(jobs.load(s, job_id))

    @app.post(PREFIX + "/captures")
    async def capture(request: Request, image: UploadFile = File(...), hint: str = Form(""),
                      locationId: str | None = Form(None), s=Depends(session)):
        rate(request, "capture", 4, 60, s["actor"])
        cleanup()
        normalized, _ = await read_photo(image, hint, locationId, s)
        capture_id, filename = jobs.persist_photo(s, normalized)
        try:
            state.execute("INSERT INTO captures VALUES(?,?,?,?,?,NULL)",
                          (capture_id, s["id"], s["collection"], filename, time.time() + cfg.capture_ttl))
            request.state.legacy_capture_id = capture_id
        except BaseException:
            (cfg.uploads / filename).unlink(missing_ok=True)
            raise
        remaining = max(0, cfg.capture_sync_timeout - (time.monotonic() - request.state.started_at))
        try:
            async with asyncio.timeout(remaining):
                async with state.collection_lock(MODEL_LOCK):
                    return await jobs.analyze_photo(s, capture_id, normalized, hint)
        except TimeoutError:
            return draft_result(capture_id, warning="Photo analysis reached the synchronous request time limit. Your photo is retained; enter the details manually.")
        except ServiceError as exc:
            if exc.status in {401, 403, 409}:
                raise
            return draft_result(capture_id, warning=exc.message)

    @app.post(PREFIX + "/chat")
    async def chat(body: Message, request: Request, s=Depends(session)):
        rate(request, "chat", 20, 60, s["actor"])
        return await conversations.handle(s, body)

    @app.get(PREFIX + "/conversations/current")
    async def current_conversation(s=Depends(session)):
        return await conversations.current(s)

    @app.post(PREFIX + "/conversations/reset")
    async def reset_conversation(s=Depends(session)):
        return await conversations.reset(s)

    def mapping_row(tag_id, s):
        row = state.one("SELECT * FROM mappings WHERE id=? AND collection=?", (tag_id, s["collection"]))
        if not row:
            raise ServiceError("tag_not_found", "Tag not found in this collection.", 404)
        return json.loads(row["data"])

    async def validate_target(s, mapping):
        record = await services.tool(s, "get_entity", {"entity_id": mapping["entityId"]})
        if record["isLocation"] != (mapping["targetType"] == "location"):
            raise ServiceError("target_type_mismatch", "The selected record does not match this tag type.", 422)
        return record

    @app.get(PREFIX + "/tags/mappings")
    async def mappings(s=Depends(session)):
        rows = state.all("SELECT data FROM mappings WHERE collection=?", (s["collection"],))
        return {"mappings": [json.loads(v["data"]) for v in rows]}

    @app.post(PREFIX + "/tags/mappings")
    async def create_mapping(body: MappingCreate, s=Depends(session)):
        value = body.model_dump(mode="json")
        await validate_target(s, value)
        tag_id = "t_" + secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc).isoformat()
        value.update(tagId=tag_id, status="active", url=f"{cfg.origin}/t/{tag_id}", createdAt=now, updatedAt=now)
        state.execute("INSERT INTO mappings VALUES(?,?,?)", (tag_id, s["collection"], json.dumps(value)))
        state.audit(s, {"id": tag_id, "type": "tag.create", "payload": value}, "succeeded")
        return value

    @app.patch(PREFIX + "/tags/mappings/{tag_id}")
    async def update_mapping(tag_id: str, body: MappingUpdate, s=Depends(session)):
        value = mapping_row(tag_id, s)
        before = value.copy()
        value.update(body.model_dump(mode="json", exclude_none=True))
        await validate_target(s, value)
        value["updatedAt"] = datetime.now(timezone.utc).isoformat()
        state.execute("UPDATE mappings SET data=? WHERE id=? AND collection=?",
                      (json.dumps(value), tag_id, s["collection"]))
        state.audit(s, {"id": tag_id, "type": "tag.update", "before": before, "result": value}, "succeeded")
        return value

    @app.get(PREFIX + "/tags/resolve/{tag_id}")
    async def resolve_tag(tag_id: str, request: Request, s=Depends(session)):
        rate(request, "tag", 60, 60, s["actor"])
        value = mapping_row(tag_id, s)
        if value["status"] != "active":
            raise ServiceError("tag_not_found", "This tag is unknown or revoked.", 404)
        return {"mapping": value, "entity": await validate_target(s, value)}

    return app


app = create_app()
