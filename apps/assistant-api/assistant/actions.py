import json
import secrets
import time

from pydantic import ValidationError

from .clients import ServiceError
from .schemas import PAYLOADS


def public(action):
    result = {k: v for k, v in action.items() if not k.startswith("_")}
    result["confirmationKey"] = action.get("_key", f"{action['id']}:{action['revision']}")
    return result


class Actions:
    def __init__(self, config, state, clients):
        self.config, self.state, self.clients = config, state, clients

    def load(self, session, action_id):
        row = self.state.one(
            "SELECT * FROM actions WHERE id=? AND actor=? AND session=? AND collection=?",
            (action_id, session["actor"], session["id"], session["collection"]),
        )
        if not row:
            raise ServiceError("not_found", "Action not found in this session and collection.", 404)
        return row, json.loads(row["data"])

    async def propose(self, session, request):
        try:
            payload = PAYLOADS[request.type].model_validate(request.payload).model_dump(mode="json", exclude_none=True)
        except ValidationError as exc:
            raise ServiceError("invalid_action", "Check the action fields and try again.", 422) from exc
        if "tags" in payload:
            tags = payload["tags"]
            if any(not value.strip() or len(value) > 100 for value in tags):
                raise ServiceError("invalid_tags", "Each tag must contain 1 to 100 characters.", 422)
            payload["tags"] = list(dict.fromkeys(value.strip() for value in tags))
        before = None
        if "entityId" in payload:
            before = await self.clients.tool(session, "get_entity", {"entity_id": payload["entityId"]})
            if before["isLocation"]:
                raise ServiceError("invalid_item", "This action requires an item, not a location.", 422)
        destination = None
        destination_id = payload.get("locationId") or payload.get("parentId")
        if destination_id:
            destination = await self.clients.tool(session, "get_entity", {"entity_id": destination_id})
        if request.type == "location.create" and destination and not destination["isLocation"]:
            raise ServiceError("invalid_parent", "A location's parent must also be a location.", 422)
        if request.type == "item.move" and payload["entityId"] == destination_id:
            raise ServiceError("invalid_move", "An item cannot contain itself.", 422)
        if request.type == "item.update" and len(payload) < 2:
            raise ServiceError("invalid_action", "Choose at least one field to update.", 422)
        action_id = "a_" + secrets.token_urlsafe(24)
        capture = None
        if request.captureId:
            if request.type != "item.create":
                raise ServiceError("invalid_capture", "Photos can only be attached to a create action.", 422)
            capture = self.state.one(
                "SELECT * FROM captures WHERE id=? AND session=? AND collection=? AND expires>?",
                (request.captureId, session["id"], session["collection"], time.time()),
            )
            if not capture or capture["action_id"]:
                raise ServiceError("capture_unavailable", "This photo has expired or already belongs to an action.", 409)
        label = payload.get("name") or (before or {}).get("name", "item")
        summary = {
            "item.create": f"Create {label}",
            "item.move": f"Move {label} to {(destination or {}).get('locationPath', '')} / {(destination or {}).get('name', '')}",
            "item.update": f"Update {label}",
            "item.tags": f"Replace tags on {label}",
            "location.create": f"Create location {label}",
        }[request.type]
        action = {
            "id": action_id, "type": request.type, "revision": 1, "status": "proposed",
            "summary": summary, "payload": payload, "before": before,
            "after": {**payload, **({"destination": destination} if destination else {}),
                      "photoIncluded": bool(request.captureId)},
            "result": None, "error": None, "retryable": False, "_destination": destination,
            "_captureId": request.captureId,
        }
        with self.state.connect() as db:
            if capture:
                changed = db.execute(
                    "UPDATE captures SET action_id=? WHERE id=? AND action_id IS NULL",
                    (action_id, request.captureId),
                ).rowcount
                if not changed:
                    raise ServiceError("capture_unavailable", "This photo already belongs to another action.", 409)
            db.execute("INSERT INTO actions VALUES(?,?,?,?,?,?,?)", (
                action_id, session["id"], session["actor"], session["collection"], json.dumps(action),
                "proposed", time.time() + self.config.action_ttl,
            ))
        self.state.audit(session, action, "proposed")
        return public(action)

    async def confirm(self, session, action_id, revision, key):
        async with self.state.collection_lock(session["collection"]):
            return await self._confirm_locked(session, action_id, revision, key)

    async def _confirm_locked(self, session, action_id, revision, key):
        row, action = self.load(session, action_id)
        if revision != action["revision"]:
            raise ServiceError("stale_revision", "The action changed. Review a new confirmation.", 409)
        if action.get("_key") and action["_key"] != key:
            raise ServiceError("idempotency_conflict", "Use the same retry key for this action.", 409)
        if action["status"] in {"succeeded", "running", "uncertain"}:
            return public(action)
        if action["status"] == "failed" and not action.get("retryable"):
            raise ServiceError("action_not_retryable", "This action cannot be safely retried. Review Homebox first.", 409)
        if action["status"] == "cancelled" or row["expires"] < time.time():
            raise ServiceError("action_expired", "This action was cancelled or expired. Propose it again.", 409)
        # Claim before asynchronous reads or writes so overlapping confirms cannot execute twice.
        if not self.state.execute(
            "UPDATE actions SET status='running' WHERE id=? AND status IN ('proposed','failed')", (action_id,),
        ):
            _, current = self.load(session, action_id)
            return public(current)
        action["status"], action["_key"], action["error"] = "running", key, None
        action["retryable"] = False
        self.state.save_action(action)
        mutation_started = False
        try:
            if not action.get("_createdId"):
                for old in (action["before"], action.get("_destination")):
                    if old:
                        current = await self.clients.tool(session, "get_entity", {"entity_id": old["id"]})
                        if current != old:
                            raise ServiceError("stale_action", "An item or destination changed. Review a new action.", 409)
            payload = action["payload"]
            kind = action["type"]
            self.state.audit(session, action, "confirmed")
            action["_confirmedAt"] = time.time()
            self.state.save_action(action)
            mutation_started = True
            if kind == "item.create":
                if not action.get("_createdId"):
                    basic = {k: payload[k] for k in (
                        "name", "locationId", "quantity", "description", "manufacturer", "model",
                    ) if k in payload}
                    created = await self.clients.tool(session, "create_item", {"payload": basic})
                    action["_createdId"] = created["id"]
                    self.state.save_action(action)
                eid = action["_createdId"]
                if not action.get("_enriched"):
                    changes = {k: payload[k] for k in ("serialNumber", "notes") if payload.get(k)}
                    if payload.get("category"):
                        note = f"Suggested category: {payload['category']}"
                        changes["notes"] = "\n".join(filter(None, [changes.get("notes"), note]))[:1000]
                    if changes:
                        await self.clients.tool(session, "update_item", {"entity_id": eid, "changes": changes})
                    if payload.get("tags"):
                        await self.clients.tool(session, "set_tags", {"entity_id": eid, "tags": payload["tags"]})
                    action["_enriched"] = True
                    self.state.save_action(action)
                if action.get("_captureId") and not action.get("_attached"):
                    capture = self.state.one("SELECT * FROM captures WHERE id=?", (action["_captureId"],))
                    if not capture:
                        raise ServiceError("capture_expired", "The photo expired. The item exists; attach a photo in Homebox.", 409)
                    data = (self.config.uploads / capture["filename"]).read_bytes()
                    await self.clients.upload(session, eid, f"capture-{action_id}.jpg", data)
                    action["_attached"] = True
                    self.state.save_action(action)
                result = await self.clients.tool(session, "get_entity", {"entity_id": eid})
            elif kind == "item.move":
                result = await self.clients.tool(session, "move_item", {
                    "entity_id": payload["entityId"], "destination_id": payload["locationId"],
                })
            elif kind == "item.update":
                result = await self.clients.tool(session, "update_item", {
                    "entity_id": payload["entityId"],
                    "changes": {k: v for k, v in payload.items() if k != "entityId"},
                })
            elif kind == "item.tags":
                result = await self.clients.tool(session, "set_tags", {
                    "entity_id": payload["entityId"], "tags": payload["tags"],
                })
            else:
                created = await self.clients.tool(session, "create_location", {
                    "name": payload["name"], "parent_id": payload.get("parentId"),
                    "description": payload.get("description", ""),
                })
                result = await self.clients.tool(session, "get_entity", {"entity_id": created["id"]})
            action["status"], action["result"] = "succeeded", result
            action["retryable"] = False
            self.state.save_action(action)
            if action.get("_captureId"):
                capture = self.state.one("SELECT * FROM captures WHERE id=?", (action["_captureId"],))
                if capture:
                    (self.config.uploads / capture["filename"]).unlink(missing_ok=True)
                    self.state.execute("DELETE FROM captures WHERE id=?", (capture["id"],))
            self.state.audit(session, action, "succeeded")
        except Exception as exc:
            # Created UUIDs are durably recorded before enrichment/upload. Retrying
            # them resumes those steps. Unknown creation outcomes never auto-repeat.
            action["status"] = "failed" if not mutation_started or action.get("_createdId") else "uncertain"
            action["retryable"] = bool(action.get("_createdId") and action.get("_enriched"))
            if action.get("_createdId"):
                action["result"] = {"id": action["_createdId"], "name": action["payload"]["name"]}
            action["error"] = exc.message if isinstance(exc, ServiceError) else "The action could not finish. Check Homebox before retrying."
            self.state.save_action(action)
            self.state.audit(session, action, action["status"])
            if isinstance(exc, ServiceError) and exc.code == "stale_action":
                action["status"] = "cancelled"
                self.state.save_action(action)
                raise
        return public(action)
