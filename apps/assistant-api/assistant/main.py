import asyncio
import hashlib
import hmac
import json
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .actions import Actions, public
from .clients import Clients, ServiceError
from .config import Config
from .images import VisionClient, normalize
from .schemas import ActionRequest, Collection, Confirm, Intent, Login, MappingCreate, MappingUpdate, Message
from .state import State

PREFIX = "/api/assistant/v1"
COOKIE = "hb_assistant"


def create_app(config=None, clients=None):
    cfg = config or Config.load()
    state = State(cfg.state, cfg.instance_id)
    cfg.uploads.mkdir(mode=0o700, parents=True, exist_ok=True)
    services = clients or Clients(cfg)
    actions = Actions(cfg, state, services)
    vision = VisionClient(cfg)
    limits = defaultdict(deque)

    def cleanup():
        now = time.time()
        for capture in state.all(
            "SELECT c.* FROM captures c LEFT JOIN actions a ON c.action_id=a.id "
            "WHERE c.expires<? AND (a.id IS NULL OR a.status NOT IN ('running','failed'))", (now,),
        ):
            (cfg.uploads / capture["filename"]).unlink(missing_ok=True)
            state.execute("DELETE FROM captures WHERE id=?", (capture["id"],))
        state.execute("DELETE FROM sessions WHERE expires<?", (now,))

    @asynccontextmanager
    async def lifespan(app):
        cleanup()
        yield

    app = FastAPI(title="Homebox Companion", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config, app.state.store, app.state.clients = cfg, state, services
    app.state.actions, app.state.vision = actions, vision

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
        return row

    async def session_response(value):
        groups = await services.homebox("GET", "/groups/all", {**value, "collection": ""})
        user = value["user"]
        return {
            "user": {k: user.get(k, "") for k in ("id", "name", "email")},
            "collections": [{"id": g["id"], "name": g["name"]} for g in groups],
            "collectionId": value["collection"], "csrfToken": value["csrf"],
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
        temporary["csrf"] = csrf
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
        s["collection"] = group
        await services.homebox("GET", "/groups", s)
        state.execute("UPDATE sessions SET collection=? WHERE id=?", (group, s["id"]))
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

    @app.post(PREFIX + "/captures")
    async def capture(request: Request, image: UploadFile = File(...), hint: str = Form(""),
                      locationId: str | None = Form(None), s=Depends(session)):
        rate(request, "capture", 4, 60, s["actor"])
        cleanup()
        if locationId:
            try:
                locationId = str(UUID(locationId))
            except ValueError:
                raise ServiceError("invalid_location", "Choose a valid location.", 422)
            await services.tool(s, "get_entity", {"entity_id": locationId})
        data = await image.read(cfg.max_upload + 1)
        await image.close()
        if len(data) > cfg.max_upload:
            raise ServiceError("image_too_large", "The image exceeds the upload limit.", 413)
        normalized = await asyncio.to_thread(normalize, data, cfg)
        warning = None
        try:
            answer = await vision.analyze(normalized, hint)
            item = answer.items[0]
        except ServiceError as exc:
            if exc.code not in {"model_unavailable", "invalid_model_output"}:
                raise
            item, answer, warning = None, None, exc.message
        capture_id = "c_" + secrets.token_urlsafe(24)
        filename = capture_id + ".jpg"
        path = cfg.uploads / filename
        path.write_bytes(normalized)
        path.chmod(0o600)
        state.execute("INSERT INTO captures VALUES(?,?,?,?,?,NULL)",
                      (capture_id, s["id"], s["collection"], filename, time.time() + cfg.capture_ttl))
        warnings = [warning] if warning else ["Model output is a draft. Check every field before saving."]
        if item and item.confidence < .6:
            warnings.append("The model is uncertain about this identification.")
        if answer and len(answer.items) > 1:
            warnings.append("Several objects were detected. This draft contains only the first; photograph the others separately.")
        duplicates = await services.tool(s, "search_items", {"query": item.model or item.name}) if item else {"items": []}
        location_list = await services.tool(s, "list_locations")
        return {
            "captureId": capture_id,
            "draft": {"name": item.name if item else "", "category": item.category if item else None,
                      "manufacturer": (item.manufacturer or "") if item else "",
                      "model": (item.model or "") if item else "", "serialNumber": (item.serialNumber or "") if item else "",
                      "quantity": item.quantity_estimate if item else 1, "notes": answer.image_notes if answer else "",
                      "tags": item.suggested_tags if item else []},
            "duplicates": duplicates["items"], "locations": location_list["locations"], "warnings": warnings,
        }

    @app.post(PREFIX + "/chat")
    async def chat(body: Message, request: Request, s=Depends(session)):
        rate(request, "chat", 20, 60, s["actor"])
        text = body.message.strip().rstrip(".?!")
        move = re.fullmatch(r"(?:i\s+(?:moved|put)|move)\s+(?:the\s+)?(.+?)\s+(?:into|to|in)\s+(?:the\s+)?(.+)", text, re.I)
        contents = re.fullmatch(r"(?:what(?:'s| is)|show (?:me )?what(?:'s| is))\s+in\s+(?:the\s+)?(.+)", text, re.I)
        search = re.match(r"^(?:where (?:is|are)|find|search for|look for)\s+(?:(?:my|the)\s+)?", text, re.I)
        if move:
            intent = Intent(kind="move", query=move[1], destination=move[2])
        elif contents:
            intent = Intent(kind="contents", query=contents[1])
        elif search:
            intent = Intent(kind="search", query=text[search.end():])
        else:
            try:
                intent = await vision.intent(text)
            except ServiceError as exc:
                result = await services.tool(s, "search_items", {"query": text[:500]})
                return {"message": exc.message, "items": result["items"]}

        def matches(value, query):
            def normalized(text):
                return re.sub(r"\s*/\s*", "/", text.strip()).casefold()
            return normalized(query) in {
                normalized(value.get("name", "")), normalized(value.get("path", "")),
                normalized(" / ".join(filter(None, [value.get("locationPath"), value.get("name")]))),
            }

        async def destinations(query):
            locations = (await services.tool(s, "list_locations"))["locations"]
            choices = [value for value in locations if matches(value, query)]
            containers = (await services.tool(s, "search_items", {"query": query.split("/")[-1].strip()}))["items"]
            choices += [value for value in containers if matches(value, query)]
            return choices

        if intent.kind == "unsupported":
            return {"message": "I can search, show contents, or propose reviewed inventory changes. Deletion and merging are not available in chat.", "items": []}
        if intent.kind == "contents":
            choices = await destinations(intent.query)
            if len(choices) == 1:
                result = await services.tool(s, "location_contents", {"location_id": choices[0]["id"]})
                return {"message": f"{choices[0].get('path', choices[0]['name'])}: {result['total']} item(s).", "items": result["items"]}
            return {"message": "Choose the location you mean.", "items": [],
                    "choices": {"kind": "location", "candidates": choices}}
        if intent.kind == "search":
            result = await services.tool(s, "search_items", {"query": intent.query})
            message = "No matching inventory records." if not result["items"] else "\n".join(
                f"{v['name']} — {v['locationPath'] or 'No location assigned'}" for v in result["items"][:10]
            )
            return {"message": message, "items": result["items"]}

        selected = []
        if intent.kind in {"move", "update", "tags"}:
            found = (await services.tool(s, "search_items", {"query": intent.query.split("/")[-1].strip()}))["items"]
            selected = [value for value in found if matches(value, intent.query)]
            if len(selected) != 1:
                return {"message": "Choose the exact item before making a change.", "items": found,
                        "choices": {"kind": "item", "candidates": found}}
        payload = {"entityId": selected[0]["id"]} if selected else {}
        if intent.kind == "move" or intent.destination:
            choices = await destinations(intent.destination or "")
            if len(choices) != 1:
                return {"message": "Choose an unambiguous destination before making a change.", "items": selected,
                        "choices": {"kind": "destination", "candidates": choices}}
            payload["parentId" if intent.kind == "location.create" else "locationId"] = choices[0]["id"]
        kind = {"move": "item.move", "update": "item.update", "tags": "item.tags"}.get(intent.kind, intent.kind)
        if intent.kind in {"item.create", "location.create"}:
            payload["name"] = intent.query
        if intent.kind in {"update", "item.create"} and intent.changes:
            payload.update(intent.changes.model_dump(exclude_none=True))
        if intent.kind == "tags":
            existing = selected[0].get("tags", [])
            payload["tags"] = (
                list(dict.fromkeys(existing + intent.tags)) if intent.tagsMode == "add"
                else [tag for tag in existing if tag not in intent.tags] if intent.tagsMode == "remove"
                else intent.tags
            )
        action = await actions.propose(s, ActionRequest(type=kind, payload=payload))
        return {"message": "Review this proposed change. Nothing has changed yet.", "items": selected, "action": action}

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
