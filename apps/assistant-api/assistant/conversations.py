import asyncio
import copy
import hashlib
import json
import re
import secrets
import time

from .actions import public
from .clients import ServiceError
from .provider_runtime import chat_configuration, operation_for, snapshot_for
from .schemas import ActionRequest, Intent
from pydantic import ValidationError


def opaque(prefix):
    return prefix + secrets.token_urlsafe(18)


def normalized(value):
    return re.sub(r"\s*/\s*", "/", value.strip()).casefold()


def exhaustive_page(result, values):
    return (result.get("complete") is True and result.get("page") == 1
            and not result.get("hasMore") and len(values) == result.get("total"))


def bounded_choices(data, reply):
    candidates = (reply.get("clarification") or {}).get("candidates", [])
    ids = {candidate["entityId"] for candidate in candidates if candidate.get("entityId")}
    for field in ("destinations", "items"):
        values = reply.get(field)
        if values and {value["id"] for value in values} == ids:
            if field == "items":
                reply[field] = []
            else:
                reply.pop(field)
    count = len(reply.get("items", [])) + len(reply.get("destinations", [])) + len(candidates)
    if count > 20 and candidates:
        data["pending"] = None
        reply.pop("clarification", None)
        resume = ("Your draft is retained. Browse destinations again for fresh choices."
                  if data.get("draft") else "Repeat the earlier browse for fresh choices.")
        reply["message"] += " " + resume
    return reply


class Conversations:
    def __init__(self, config, state, clients, actions, vision):
        self.cfg, self.state, self.clients = config, state, clients
        self.actions, self.vision = actions, vision

    def encode(self, value):
        return self.cfg.cipher.encrypt(json.dumps(value, ensure_ascii=False).encode()).decode()

    def decode(self, value):
        return json.loads(self.cfg.cipher.decrypt(value.encode()))

    def cleanup(self):
        # Allow an in-flight bounded turn to finish before deleting its rows.
        cutoff = time.time() - 2 * self.cfg.chat_timeout
        with self.state.connect() as db:
            db.execute("DELETE FROM chat_turns WHERE conversation IN (SELECT id FROM conversations WHERE expires<?)", (cutoff,))
            db.execute("DELETE FROM chat_action_links WHERE conversation_id IN (SELECT id FROM conversations WHERE expires<?)", (cutoff,))
            db.execute("DELETE FROM conversations WHERE expires<?", (cutoff,))
            db.execute("DELETE FROM chat_initial_keys WHERE session NOT IN (SELECT id FROM sessions WHERE expires>?)", (time.time(),))
            db.execute("DELETE FROM chat_contexts WHERE session NOT IN (SELECT id FROM sessions WHERE expires>?)", (time.time(),))

    async def run_cleanup(self):
        while True:
            await asyncio.sleep(30)
            self.cleanup()

    async def scope(self, session):
        self.state.require_context(session)
        groups = await self.clients.homebox("GET", "/groups/all", {**session, "collection": ""})
        group = next((g for g in groups if g["id"] == session["collection"]), None)
        if not group:
            raise ServiceError("collection_denied", "This collection is no longer accessible.", 403)
        self.state.require_context(session)
        session["_groups"] = [{"id": g["id"], "name": g["name"], "current": g["id"] == session["collection"]} for g in groups]
        return {"instanceId": self.cfg.instance_id,
                "instanceLabel": self.cfg.instance_label or "Homebox " + self.cfg.instance_id[:8],
                "collectionId": session["collection"], "collectionName": group["name"],
                "contextVersion": session["_contextVersion"]}

    def active_action(self, session):
        row = self.actions.pending(session)
        return public(json.loads(row["data"])) if row else None

    def action_draft(self, session, action):
        draft = {"id": opaque("op_"), "type": action["type"], "payload": action["payload"],
                 "actionId": action["id"], "status": action["status"]}
        raw = self.actions.load(session, action["id"])[1]
        if raw.get("_captureId"):
            draft["captureId"] = raw["_captureId"]
        return draft

    def fresh_data(self, session):
        data = {"draft": None, "pending": None, "pages": {}, "lastEntities": [],
                "provider": chat_configuration(snapshot_for(self.vision, self.cfg))}
        action = self.active_action(session)
        if action:
            data["draft"] = self.action_draft(session, action)
        return data

    def load(self, session, conversation_id=None, create=False):
        context = self.state.chat_context(session)
        conversation_id = conversation_id or context["conversation_id"]
        row = self.state.one(
            "SELECT * FROM conversations WHERE id=? AND session=? AND actor=? AND collection=? AND instance=? AND context_version=?",
            (conversation_id, session["id"], session["actor"], session["collection"], self.cfg.instance_id,
             session["_contextVersion"]),
        ) if conversation_id else None
        expired = bool(row and row["expires"] <= time.time())
        if row and not expired:
            return row, self.decode(row["data"]), False
        if not create:
            return None, None, expired
        data, cid = self.fresh_data(session), opaque("cv_")
        with self.state.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.state.require_context(session, db)
            db.execute("INSERT INTO conversations VALUES(?,?,?,?,?,?,?,?,?)",
                       (cid, session["id"], session["actor"], session["collection"], self.cfg.instance_id,
                        session["_contextVersion"], 0, time.time() + self.cfg.conversation_ttl, self.encode(data)))
            db.execute("UPDATE chat_contexts SET conversation_id=? WHERE session=?", (cid, session["id"]))
        return self.load(session, cid)

    def sync_action(self, session, data):
        draft = data.get("draft")
        if draft and draft.get("actionId"):
            try:
                _, raw = self.actions.load(session, draft["actionId"])
            except ServiceError as exc:
                if exc.status != 404:
                    raise
                data["draft"], data["pending"] = None, None
                return
            status = raw["status"]
            if status == "succeeded":
                if raw.get("result"):
                    data["lastEntities"] = [raw["result"]]
                data["draft"], data["pending"] = None, None
            elif status == "cancelled":
                data["draft"], data["pending"] = None, None
            else:
                draft["status"] = status
        elif not draft:
            active = self.active_action(session)
            if active:
                data["draft"] = self.action_draft(session, active)

    def response(self, session, row, data, scope, message, outcome="answered", **fields):
        self.sync_action(session, data)
        result = {"conversationId": row["id"], "turnId": session.get("_chatTurnId", ""),
                  "revision": row["revision"], "outcome": outcome, "scope": scope, "message": message, "items": []}
        if data.get("draft"):
            result["pendingOperation"] = copy.deepcopy(data["draft"])
            if data["draft"].get("actionId"):
                result["action"] = public(self.actions.load(session, data["draft"]["actionId"])[1])
        result.update(fields)
        return bounded_choices(data, result)

    async def current(self, session):
        try:
            async with asyncio.timeout(min(10, self.cfg.chat_timeout)):
                with operation_for(self.vision):
                    async with self.state.collection_lock("chat-session-" + session["id"]):
                        return await self.current_locked(session)
        except TimeoutError as exc:
            raise ServiceError("conversation_busy", "A conversation turn is still being checked. Wait, then check the conversation again before confirming.", 503) from exc

    def require_settled(self, conversation_id):
        if self.state.one("SELECT id FROM chat_turns WHERE conversation=? AND status='running' AND response IS NULL LIMIT 1",
                          (conversation_id,)):
            raise ServiceError("chat_turn_interrupted",
                               "An earlier turn was interrupted. Check that turn or explicitly restore the saved review before continuing.", 409)

    async def current_locked(self, session):
        scope = await self.scope(session)
        context = self.state.chat_context(session)
        if context["conversation_id"]:
            self.require_settled(context["conversation_id"])
        row, data, expired = self.load(session)
        if not row and self.active_action(session):
            row, data, _ = self.load(session, create=True)
        if not row:
            return {"conversation": None, "expired": expired}
        self.require_settled(row["id"])
        self.sync_action(session, data)
        restored_message = ("Restored the saved review. Check its status and fields before continuing. No request was repeated."
                            if (data.get("draft") or {}).get("actionId") else "Continue your current draft or ask about this collection.")
        reply = data.get("lastReply") or self.response(session, row, data, scope, restored_message)
        reply = {**reply, "scope": scope, "revision": row["revision"]}
        reply.pop("pendingOperation", None)
        reply.pop("action", None)
        if data.get("draft"):
            reply["pendingOperation"] = data["draft"]
            if data["draft"].get("actionId"):
                reply["action"] = public(self.actions.load(session, data["draft"]["actionId"])[1])
        elif reply.get("outcome") == "proposal":
            reply.update(outcome="answered", message="The previous review is resolved. Ask about its saved record or start another request.",
                         items=data.get("lastEntities", []))
        if data.get("pending"):
            reply["clarification"] = copy.deepcopy(data["pending"])
        else:
            reply.pop("clarification", None)
        return {"conversation": bounded_choices(data, reply), "expired": False}

    async def reset(self, session):
        with operation_for(self.vision):
            async with self.state.collection_lock("chat-session-" + session["id"]):
                with self.state.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    self.state.require_context(session, db)
                    db.execute("UPDATE conversations SET expires=0 WHERE session=?", (session["id"],))
                    db.execute("UPDATE chat_contexts SET conversation_id=NULL WHERE session=?", (session["id"],))
                return await self.current_locked(session)

    async def read(self, session, name, arguments=None):
        if name not in {"inventory_overview", "list_items_page", "list_destinations_page", "get_entity_card"}:
            raise ServiceError("unsupported_read", "This inventory read is not supported.", 422)
        session["_chatReads"] = session.get("_chatReads", 0) + 1
        if session["_chatReads"] > 4:
            raise ServiceError("chat_read_limit", "This question needs too many reads. Please narrow it down.", 422)
        self.state.require_context(session)
        result = await self.clients.tool(session, name, arguments or {})
        self.state.require_context(session)
        if name != "get_entity_card" and result.get("complete") is not True:
            raise ServiceError("incomplete_inventory_read", "The inventory read was incomplete. Please retry; no empty result was assumed.", 503)
        if name == "inventory_overview":
            items, count, locations = result.get("items"), result.get("itemRecordCount"), result.get("locationCount")
            if (not isinstance(items, list) or type(count) is not int or type(locations) is not int
                    or count < 0 or locations < 0 or result.get("includeArchived") is not False
                    or len(items) != min(count, (arguments or {}).get("preview_limit", 10))):
                raise ServiceError("incomplete_inventory_read", "The inventory overview or its totals were inconsistent.", 503)
        if name in {"list_items_page", "list_destinations_page"}:
            values = result.get("items" if name == "list_items_page" else "destinations")
            page, size, total = result.get("page"), result.get("pageSize"), result.get("total")
            if (not isinstance(values, list) or type(page) is not int or type(size) is not int or type(total) is not int
                    or page != (arguments or {}).get("page", 1) or size != (arguments or {}).get("page_size", 20)
                    or not 1 <= size <= 20 or total < 0 or type(result.get("hasMore")) is not bool
                    or result["hasMore"] != (page * size < total)
                    or len(values) != min(size, max(0, total - (page - 1) * size))):
                raise ServiceError("incomplete_inventory_read", "The inventory page or total was inconsistent. No empty result was assumed.", 503)
        return result

    def continuation(self, data, tool, args):
        token = opaque("pg_")
        pages = data.setdefault("pages", {})
        while len(pages) >= 5:
            pages.pop(next(iter(pages)))
        pages[token] = {"tool": tool, "args": args}
        return token

    def page(self, data, result, tool, args):
        token = None
        if result["hasMore"]:
            token = self.continuation(data, tool, {**args, "page": result["page"] + 1})
        return {"continuationId": token, "totalRecords": result["total"], "page": result["page"],
                "pageSize": result["pageSize"], "hasMore": result["hasMore"], "complete": result["complete"]}

    def clarification(self, data, slot, reason, values, unassigned=False):
        candidates = [{"id": opaque("ch_"), "entityId": value["id"], "label": value["name"],
                       "path": value.get("path") or " / ".join(filter(None, [value.get("locationPath"), value["name"]])),
                       "kind": value.get("kind") or ("location" if value.get("isLocation") else "item")}
                      for value in values[:20]]
        if unassigned and len(candidates) < 20:
            candidates.append({"id": opaque("ch_"), "entityId": None, "label": "Leave unassigned",
                               "path": "", "kind": "unassigned"})
        pending = {"id": opaque("cl_"), "slot": slot, "reason": reason, "candidates": candidates}
        data["pending"] = pending
        return copy.deepcopy(pending)

    async def destinations(self, session, row, data, scope, query="", reason="missing", kind="locations", page=1, slot="destination"):
        args = {"kind": kind, "query": query, "page": page, "page_size": 20}
        result = await self.read(session, "list_destinations_page", args)
        values = result["destinations"]
        clarification = None
        draft = data.get("draft")
        if draft or slot == "contents":
            clarification = self.clarification(data, slot, reason, values,
                                                unassigned=slot == "destination" and draft["type"] == "item.create")
        if slot == "contents":
            args["_slot"] = slot
        message = ("Choose a destination for your pending draft." if clarification else
                   "Storage destinations in " + scope["collectionName"] + ".") if values else (
                   ("No matching destinations in this collection. Browse locations, create a location explicitly, or choose unassigned."
                    if result["total"] == 0 else "This page is empty, but other matching destinations exist. Browse from the first page."))
        return self.response(session, row, data, scope, message,
                             "clarification" if clarification else "answered",
                             destinations=values, pagination=self.page(data, result, "list_destinations_page", args),
                             **({"clarification": clarification} if clarification else {}))

    async def find_destination(self, session, row, data, scope, query, parent_only=False):
        values, exhaustive, pages = [], True, []
        for kind in (["locations"] if parent_only else ["locations", "items"]):
            result = await self.read(session, "list_destinations_page",
                                     {"kind": kind, "query": query.split("/")[-1].strip(), "page": 1, "page_size": 20})
            values.extend(result["destinations"])
            pages.append((kind, result))
            exhaustive = exhaustive and exhaustive_page(result, result["destinations"])
        exact = [v for v in values if normalized(query) in {normalized(v["name"]), normalized(v["path"])}]
        if len(exact) == 1 and exhaustive:
            return exact[0], None
        reason = "ambiguous" if len(exact) > 1 or not exhaustive else "not_found"
        # Show a truthful single-kind page when matching populations do not
        # fit. Its continuation always creates new explicit choices, never an
        # automatic resolution from the final page.
        paged = next(((kind, result) for kind, result in pages if not exhaustive_page(result, result["destinations"])), None)
        if paged or len(values) > 20:
            kind, result = paged or pages[0]
            values = result["destinations"]
            args = {"kind": kind, "query": query.split("/")[-1].strip(), "page": 1, "page_size": 20}
            clarification = self.clarification(data, "destination", "ambiguous", values,
                                                unassigned=data["draft"]["type"] == "item.create")
            return None, self.response(session, row, data, scope,
                                       "Choose an exact path from these matching " + kind + ". Browse item containers or locations separately for other destinations.",
                                       "clarification", clarification=clarification, destinations=values,
                                       pagination=self.page(data, result, "list_destinations_page", args))
        if not values:
            return None, await self.destinations(session, row, data, scope, reason=reason)
        clarification = self.clarification(data, "destination", reason, values,
                                            unassigned=data["draft"]["type"] == "item.create")
        return None, self.response(session, row, data, scope,
                                   "Choose the exact destination path. No inventory change has been made.",
                                   "clarification", clarification=clarification, destinations=values[:20])

    async def cancel_preview(self, session, data):
        draft = data.get("draft") or {}
        if not draft.get("actionId"):
            return
        async with self.state.collection_lock(session["collection"]):
            _, action = self.actions.load(session, draft["actionId"])
            if action["status"] != "proposed":
                raise ServiceError("write_pending", "This action already started or needs recovery. Check its status before editing.", 409)
            action["status"] = "cancelled"
            with self.state.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                self.state.require_context(session, db)
                changed = db.execute("UPDATE actions SET status='cancelled',data=? WHERE id=? AND status='proposed'",
                                     (json.dumps(action), action["id"])).rowcount
                if not changed:
                    raise ServiceError("write_pending", "This action already started. Recover its status.", 409)
                db.execute("UPDATE captures SET action_id=NULL WHERE action_id=?", (action["id"],))
            self.state.audit(session, action, "cancelled")
        draft.pop("actionId", None)
        draft["status"] = "draft"

    async def propose(self, session, row, data, scope):
        draft = data["draft"]
        if session["_chatMode"] == "read_only":
            return self.response(session, row, data, scope, "This is a read-only turn. Continue the draft explicitly to review a change.", "unsupported")
        if session.get("_chatReads", 0) >= 4:
            clarification = self.clarification(data, "review", "missing", [])
            return self.response(session, row, data, scope,
                                 "Your draft is retained. Say 'review draft' to validate its full preview in the next turn.",
                                 "clarification", clarification=clarification)
        action = await self.actions.propose(session, ActionRequest(type=draft["type"], payload=draft["payload"],
                                                                  captureId=draft.get("captureId")))
        draft.update(actionId=action["id"], status=action["status"])
        destination = action["after"].get("destination")
        if destination:
            draft["destination"] = {"id": destination["id"], "name": destination["name"],
                                    "path": destination.get("path") or " / ".join(filter(None, [destination.get("locationPath"), destination["name"]])),
                                    "kind": "location" if destination["isLocation"] else "item"}
        data["pending"] = None
        return self.response(session, row, data, scope, "Review this proposed change. Nothing has changed yet.", "proposal", action=action)

    async def select(self, session, row, data, scope, selection):
        pending = data.get("pending")
        if not pending or pending["id"] != selection.clarificationId:
            raise ServiceError("clarification_expired", "These choices were replaced. Use the current question.", 409)
        candidate = next((c for c in pending["candidates"] if c["id"] == selection.candidateId), None)
        if not candidate:
            raise ServiceError("clarification_expired", "Choose one of the options shown in this conversation.", 409)
        if pending["slot"] == "contents":
            value = await self.read(session, "get_entity_card", {"entity_id": candidate["entityId"]})
            data["pending"] = None
            if not value["isLocation"]:
                return self.response(session, row, data, scope, "Bounded contents browsing supports physical locations, not item containers.", "unsupported")
            return await self.page_read(session, row, data, scope, "list_items_page",
                                        {"query": "", "location_id": value["id"], "page": 1, "page_size": 20})
        if not data.get("draft"):
            if candidate["entityId"]:
                value = await self.read(session, "get_entity_card", {"entity_id": candidate["entityId"]})
                data["lastEntities"] = [value]
                data["pending"] = None
                return self.response(session, row, data, scope, value["name"] + " — " + (value.get("locationPath") or "No location assigned"), items=[value])
            raise ServiceError("clarification_expired", "There is no draft for this selection.", 409)
        if session["_chatMode"] == "read_only":
            raise ServiceError("read_only_turn", "Continue the draft in normal mode to select its destination.", 409)
        draft = data["draft"]
        if pending["slot"] == "item":
            value = await self.read(session, "get_entity_card", {"entity_id": candidate["entityId"]})
            if value["isLocation"]:
                raise ServiceError("invalid_item", "Choose an item rather than a location.", 422)
            draft["payload"]["entityId"] = value["id"]
            data["lastEntities"] = [value]
            if draft["type"] == "item.tags":
                tags, mode = draft.pop("tags"), draft.pop("tagsMode")
                old = value.get("tags", [])
                draft["payload"]["tags"] = (list(dict.fromkeys(old + tags)) if mode == "add"
                                            else [v for v in old if v not in tags] if mode == "remove" else tags)
            if draft.get("destinationQuery"):
                target, reply = await self.find_destination(session, row, data, scope, draft["destinationQuery"])
                if reply:
                    return reply
                draft["payload"]["locationId"] = target["id"]
                draft["destination"] = target
            elif draft["type"] == "item.move":
                return await self.destinations(session, row, data, scope)
        else:
            await self.cancel_preview(session, data)
            key = "parentId" if draft["type"] == "location.create" else "locationId"
            if candidate["entityId"]:
                value = await self.read(session, "get_entity_card", {"entity_id": candidate["entityId"]})
                draft["payload"][key] = value["id"]
                draft["destination"] = {"id": value["id"], "name": value["name"], "path": candidate["path"], "kind": candidate["kind"]}
            else:
                if draft["type"] != "item.create":
                    raise ServiceError("invalid_destination", "This operation needs a destination.", 422)
                draft["payload"].pop(key, None)
                draft["destination"] = None
        return await self.propose(session, row, data, scope)

    def local_intent(self, text, data):
        lower = text.strip().rstrip(".?!").casefold()
        if re.search(r"\b(collections|inventories)\b", lower) and re.match(r"(which|what|show|list)", lower):
            return Intent(kind="collections.list")
        if (re.search(r"\b(destinations|locations|places)\b", lower) and re.match(r"(which|what|show|list|browse)", lower)
                or re.match(r"where can i (put|store)", lower)):
            return Intent(kind="destinations.list")
        if re.search(r"\b(?:item containers|containers)\b", lower) and re.match(r"(which|what|show|list|browse)", lower):
            return Intent(kind="destinations.list", destinationKind="items")
        if (re.fullmatch(r"(which inventory do i have|what do i own|what (?:items|inventory) do i have|what(?:'s| is) in my inventory)", lower)
                or re.fullmatch(r"(?:show|list|browse)(?: me)?(?: all)?(?: my)? (?:inventory|items)", lower)
                or re.match(r"how many (?:items|records)", lower)):
            return Intent(kind="inventory.overview")
        quantity = re.fullmatch(r"(?:make (?:that|it)|change (?:that|it|quantity) to|quantity(?: is)?)\s+(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten)", lower)
        if quantity:
            words = "one two three four five six seven eight nine ten".split()
            value = words.index(quantity[1]) + 1 if quantity[1] in words else float(quantity[1])
            return Intent(kind="draft.edit", changes={"quantity": value})
        if lower in {"where is it", "where is that", "where's it", "where's that", "that one"}:
            return Intent(kind="reference")
        if data.get("draft") and lower in {"review draft", "review the draft", "continue draft", "show review", "continue"}:
            return Intent(kind="draft.edit")
        create = re.fullmatch(r"add\s+(?:an?\s+)?(.+?)\s+(?:to|in|into)\s+(?:the\s+)?(.+)", text.strip().rstrip(".?!"), re.I)
        if create and not create[1].casefold().startswith("tag "):
            return Intent(kind="item.create", query=create[1], destination=create[2])
        move = re.fullmatch(r"(?:i\s+(?:moved|put)|move)\s+(?:the\s+)?(.+?)\s+(?:into|to|in)\s+(?:the\s+)?(.+)", text.strip().rstrip(".?!"), re.I)
        if move:
            return Intent(kind="move", query=move[1], destination=move[2])
        contents = re.fullmatch(r"(?:what(?:'s| is)|show (?:me )?what(?:'s| is))\s+in\s+(?:the\s+)?(.+)", lower)
        if contents:
            return Intent(kind="contents", query=contents[1])
        search = re.match(r"^(?:where (?:is|are)|find|search for|look for)\s+(?:(?:my|the)\s+)?", text, re.I)
        if search:
            return Intent(kind="search", query=text[search.end():].rstrip(".?!"))
        return None

    async def label_selection(self, session, row, data, scope, text):
        slot, draft = data["pending"]["slot"], data.get("draft")
        if slot == "destination" and draft:
            if session["_chatMode"] == "read_only":
                return self.response(session, row, data, scope, "Read-only browsing cannot change the destination. Use a current choice in normal mode.", "unsupported")
            await self.cancel_preview(session, data)
            target, reply = await self.find_destination(session, row, data, scope, text, draft["type"] == "location.create")
            if reply:
                return reply
            draft["payload"]["parentId" if draft["type"] == "location.create" else "locationId"] = target["id"]
            draft["destination"] = target
            return await self.propose(session, row, data, scope)
        if slot == "contents":
            target, reply = await self.find_contents(session, row, data, scope, text)
            if reply:
                return reply
            return await self.page_read(session, row, data, scope, "list_items_page",
                                        {"query": "", "location_id": target["id"], "page": 1, "page_size": 20})
        args = {"query": text.split("/")[-1].strip(), "page": 1, "page_size": 20}
        result = await self.read(session, "list_items_page", args)
        exact = [v for v in result["items"] if normalized(text) in {
            normalized(v["name"]), normalized(v.get("path") or " / ".join(filter(None, [v.get("locationPath"), v["name"]]))) }]
        clarification = self.clarification(data, "item", "ambiguous" if result["total"] else "not_found", result["items"])
        if len(exact) != 1 or not exhaustive_page(result, result["items"]):
            return self.response(session, row, data, scope, "Choose the exact item using its current option or ordinal.",
                                 "clarification", items=result["items"], clarification=clarification,
                                 pagination=self.page(data, result, "list_items_page", {**args, "_slot": "item"}))
        from .schemas import ChatSelection
        candidate = next(c for c in clarification["candidates"] if c["entityId"] == exact[0]["id"])
        return await self.select(session, row, data, scope,
                                 ChatSelection(clarificationId=clarification["id"], candidateId=candidate["id"]))

    async def dispatch(self, session, row, data, scope, body):
        self.sync_action(session, data)
        if body.selection:
            return await self.select(session, row, data, scope, body.selection)
        if body.continuation:
            page = data.get("pages", {}).get(body.continuation.id)
            if not page:
                raise ServiceError("clarification_expired", "This page control expired. Repeat the browse request.", 409)
            return await self.page_read(session, row, data, scope, page["tool"], page["args"])
        text = body.message.strip()
        lower = text.rstrip(".?!").casefold()
        if lower in {"yes", "ok", "okay", "confirm", "save it"}:
            return self.response(session, row, data, scope,
                                 "Use the explicit Confirm control on the full preview to save. A chat reply does not confirm changes.",
                                 "proposal" if (data.get("draft") or {}).get("actionId") else "clarification")
        if lower in {"cancel draft", "cancel the draft", "cancel this draft"}:
            if body.mode == "read_only":
                raise ServiceError("read_only_turn", "A read-only turn cannot cancel the draft.", 409)
            await self.cancel_preview(session, data)
            data["draft"], data["pending"] = None, None
            return self.response(session, row, data, scope, "Draft cancelled. No inventory change was made.")
        pending = data.get("pending")
        if pending and lower in {"that one", "the first", "the second", "the third", "first", "second", "third"}:
            ordinal = {"the first": 0, "first": 0, "the second": 1, "second": 1, "the third": 2, "third": 2}
            candidates = pending["candidates"]
            index = 0 if lower == "that one" and len(candidates) == 1 else ordinal.get(lower)
            if index is not None and index < len(candidates):
                from .schemas import ChatSelection
                return await self.select(session, row, data, scope,
                                         ChatSelection(clarificationId=pending["id"], candidateId=candidates[index]["id"]))
            return self.response(session, row, data, scope, "Which exact path do you mean?", "clarification",
                                 clarification=pending)
        try:
            intent = self.local_intent(text, data)
        except ValidationError as exc:
            raise ServiceError("invalid_intent", "Check the name, destination, or quantity and try again.", 422) from exc
        if intent is None and pending:
            matches = [c for c in pending["candidates"] if normalized(text) in {normalized(c["label"]), normalized(c["path"])}]
            if len(matches) == 1 and matches[0]["kind"] == "unassigned":
                from .schemas import ChatSelection
                return await self.select(session, row, data, scope,
                                         ChatSelection(clarificationId=pending["id"], candidateId=matches[0]["id"]))
            if matches:
                return await self.label_selection(session, row, data, scope, text)
        if intent is None:
            draft = data.get("draft")
            context = {"pending": {"type": draft["type"], "payload": draft["payload"]} if draft else None,
                       "awaiting": (pending or {}).get("slot"), "lastItems": [
                           {"name": v["name"]} for v in data.get("lastEntities", [])[:3]]}
            intent = await self.vision.intent(text, context=context)
        kind = intent.kind
        if kind in {"inventory.overview", "locations.list", "destinations.list", "collections.list", "search"}:
            if kind == "inventory.overview":
                result = await self.read(session, "inventory_overview", {"preview_limit": 20})
                data["lastEntities"] = result["items"]
                overview = {k: result[k] for k in ("itemRecordCount", "locationCount", "includeArchived", "complete")}
                message = (f"{scope['collectionName']} has {result['itemRecordCount']} active item records and "
                           f"{result['locationCount']} active locations.")
                if not result["itemRecordCount"]:
                    message += " This collection has no active item records. You can add an item or browse locations."
                # Browsing continuation starts after the preview without claiming
                # that the preview is the entire catalogue.
                pagination = None
                if result["itemRecordCount"] > len(result["items"]):
                    token = self.continuation(data, "list_items_page", {"query": "", "page": 2, "page_size": 20})
                    pagination = {"continuationId": token, "totalRecords": result["itemRecordCount"], "page": 1,
                                  "pageSize": 20, "hasMore": True, "complete": True}
                return self.response(session, row, data, scope, message, overview=overview,
                                     items=result["items"], **({"pagination": pagination} if pagination else {}))
            if kind == "collections.list":
                return self.response(session, row, data, scope,
                                     "These are your accessible collections. Inventory reads remain in the selected collection.",
                                     collections=session["_groups"])
            if kind in {"locations.list", "destinations.list"}:
                return await self.destinations(session, row, data, scope, query=intent.query,
                                               kind=intent.destinationKind or "locations")
            return await self.page_read(session, row, data, scope, "list_items_page",
                                        {"query": intent.query, "page": 1, "page_size": 20})
        if kind == "reference":
            draft = data.get("draft")
            if draft:
                destination = (draft.get("destination") or {}).get("path")
                return self.response(session, row, data, scope,
                                     "This is still an unsaved draft." + (f" Its proposed destination is {destination}." if destination else " Choose its destination before review."))
            values = data.get("lastEntities", [])
            if len(values) == 1:
                card = await self.read(session, "get_entity_card", {"entity_id": values[0]["id"]})
                data["lastEntities"] = [card]
                return self.response(session, row, data, scope, card["name"] + " — " + (card.get("locationPath") or "No location assigned"), items=[card])
            clarification = self.clarification(data, "item", "ambiguous" if values else "missing", values)
            return self.response(session, row, data, scope, "Which item do you mean? Choose an item or name it.", "clarification", clarification=clarification)
        if kind == "contents":
            target, reply = await self.find_contents(session, row, data, scope, intent.query)
            if reply:
                return reply
            return await self.page_read(session, row, data, scope, "list_items_page",
                                        {"query": "", "location_id": target["id"], "page": 1, "page_size": 20})
        if kind == "unsupported":
            return self.response(session, row, data, scope, "I can show this collection, browse destinations, find items, or prepare a reviewed change. Please name the item or choose a supported browse action.", "unsupported")
        if body.mode == "read_only":
            return self.response(session, row, data, scope, "Read-only browsing cannot change the draft. Use Edit or finish the current review first.", "unsupported")
        if kind == "draft.edit":
            if not data.get("draft"):
                return self.response(session, row, data, scope, "There is no pending draft to edit. Tell me what you want to add or change.", "clarification")
            if intent.changes and data["draft"]["type"] not in {"item.create", "item.update"}:
                return self.response(session, row, data, scope, "This draft only changes a destination or tags. Finish it before requesting a different field change.", "unsupported")
            await self.cancel_preview(session, data)
            data["draft"]["payload"].update(intent.changes.model_dump(exclude_none=True) if intent.changes else {})
            if intent.destination:
                data["draft"]["destinationQuery"] = intent.destination
                target, reply = await self.find_destination(session, row, data, scope, intent.destination)
                if reply:
                    return reply
                data["draft"]["payload"]["locationId"] = target["id"]
                data["draft"]["destination"] = target
            if (data.get("pending") or {}).get("slot") == "destination" and not (
                    data["draft"]["payload"].get("locationId") or data["draft"]["payload"].get("parentId")):
                return await self.destinations(session, row, data, scope)
            return await self.propose(session, row, data, scope)
        if data.get("draft") or self.active_action(session):
            return self.response(session, row, data, scope, "Finish or cancel the current draft before starting another change. You can still browse inventory and destinations.", "clarification")
        action_type = {"move": "item.move", "update": "item.update", "tags": "item.tags"}.get(kind, kind)
        payload = {}
        data["draft"] = {"id": opaque("op_"), "type": action_type, "payload": payload, "status": "draft"}
        if intent.changes and kind in {"item.create", "update"}:
            payload.update(intent.changes.model_dump(exclude_none=True))
        if kind == "tags":
            data["draft"].update(tags=intent.tags, tagsMode=intent.tagsMode)
        if kind in {"item.create", "location.create"}:
            if not intent.query:
                data["draft"] = None
                return self.response(session, row, data, scope, "What name should the new record have?", "clarification")
            payload["name"] = intent.query
            if kind == "item.create":
                payload["quantity"] = 1
        else:
            result = await self.read(session, "list_items_page",
                                     {"query": intent.query.split("/")[-1].strip(), "page": 1, "page_size": 20})
            exact = [v for v in result["items"] if normalized(intent.query) in {
                normalized(v["name"]), normalized(" / ".join(filter(None, [v.get("locationPath"), v["name"]]))) }]
            if len(exact) != 1 or not exhaustive_page(result, result["items"]):
                data["draft"]["destinationQuery"] = intent.destination
                clarification = self.clarification(data, "item", "ambiguous" if result["items"] else "not_found", result["items"])
                return self.response(session, row, data, scope, "Choose the exact item before preparing a change.",
                                     "clarification", clarification=clarification, items=result["items"],
                                     pagination=self.page(data, result, "list_items_page",
                                                           {"query": intent.query.split("/")[-1].strip(), "page": 1,
                                                            "page_size": 20, "_slot": "item"}))
            payload["entityId"] = exact[0]["id"]
            data["lastEntities"] = exact
            if kind == "tags":
                old = exact[0].get("tags", [])
                payload["tags"] = (list(dict.fromkeys(old + intent.tags)) if intent.tagsMode == "add"
                                   else [v for v in old if v not in intent.tags] if intent.tagsMode == "remove" else intent.tags)
        if intent.changes and kind in {"item.create", "update"}:
            payload.update(intent.changes.model_dump(exclude_none=True))
        if intent.destination:
            data["draft"]["destinationQuery"] = intent.destination
            target, reply = await self.find_destination(session, row, data, scope, intent.destination, kind == "location.create")
            if reply:
                return reply
            payload["parentId" if kind == "location.create" else "locationId"] = target["id"]
            data["draft"]["destination"] = target
        elif kind in {"move", "item.create"}:
            return await self.destinations(session, row, data, scope)
        return await self.propose(session, row, data, scope)

    async def find_contents(self, session, row, data, scope, query):
        result = await self.read(session, "list_destinations_page",
                                 {"kind": "locations", "query": query.split("/")[-1].strip(), "page": 1, "page_size": 20})
        exact = [v for v in result["destinations"] if normalized(query) in {normalized(v["name"]), normalized(v["path"])}]
        if len(exact) == 1 and exhaustive_page(result, result["destinations"]):
            return exact[0], None
        if not result["total"]:
            containers = await self.read(session, "list_destinations_page",
                                         {"kind": "items", "query": query.split("/")[-1].strip(), "page": 1, "page_size": 20})
            if any(normalized(query) in {normalized(v["name"]), normalized(v["path"])} for v in containers["destinations"]):
                return None, self.response(session, row, data, scope,
                                           "Bounded contents browsing supports physical locations, not item containers.", "unsupported")
        clarification = self.clarification(data, "contents", "ambiguous" if result["total"] else "not_found", result["destinations"])
        return None, self.response(session, row, data, scope, "Choose a location by its full path to show its contents.",
                                    "clarification", destinations=result["destinations"], clarification=clarification,
                                    pagination=self.page(data, result, "list_destinations_page",
                                                         {"kind": "locations", "query": query.split("/")[-1].strip(),
                                                          "page": 1, "page_size": 20, "_slot": "contents"}))

    async def page_read(self, session, row, data, scope, tool, args):
        if tool == "list_destinations_page":
            return await self.destinations(session, row, data, scope, query=args.get("query", ""),
                                           kind=args["kind"], page=args["page"], slot=args.get("_slot", "destination"))
        result = await self.read(session, "list_items_page", {k: v for k, v in args.items() if not k.startswith("_")})
        data["lastEntities"] = result["items"]
        message = (("No matching item records in this collection." if result["total"] == 0
                    else "This page has no item records. Return to the first page to browse existing matches.") if not result["items"] else
                   "\n".join(f"{v['name']} — {v.get('locationPath') or 'No location assigned'}" for v in result["items"]))
        clarification = self.clarification(data, "item", "ambiguous" if result["total"] else "not_found", result["items"]) if args.get("_slot") == "item" else None
        return self.response(session, row, data, scope, message, "clarification" if clarification else "answered", items=result["items"],
                             pagination=self.page(data, result, tool, args),
                             **({"clarification": clarification} if clarification else {}))

    async def handle(self, session, body):
        try:
            async with asyncio.timeout(self.cfg.chat_timeout):
                with operation_for(self.vision):
                    async with self.state.collection_lock("chat-session-" + session["id"]):
                        return await self._handle(session, body)
        except TimeoutError as exc:
            raise ServiceError("chat_timed_out", "This turn reached its time limit. Recover the current conversation before retrying.", 503) from exc

    async def _handle(self, original, body):
        session = {**original, "_chatMode": body.mode, "_chatReads": 0}
        scope = await self.scope(session)
        if not body.conversationId and body.clientTurnId:
            initial = self.state.one("SELECT conversation_id FROM chat_initial_keys WHERE session=? AND context_version=? AND client_key=?",
                                     (session["id"], session["_contextVersion"], body.clientTurnId))
            if initial:
                body = body.model_copy(update={"conversationId": initial["conversation_id"]})
        if body.conversationId:
            row, data, expired = self.load(session, body.conversationId)
            if not row:
                raise ServiceError("conversation_changed", "This conversation ended. Recover the current conversation.", 409)
        else:
            context = self.state.chat_context(session)
            if context["conversation_id"]:
                self.require_settled(context["conversation_id"])
            row, data, _ = self.load(session, create=True)
        if not body.conversationId and body.clientTurnId:
            self.state.execute("INSERT INTO chat_initial_keys VALUES(?,?,?,?)",
                               (session["id"], session["_contextVersion"], body.clientTurnId, row["id"]))
        async with self.state.collection_lock("chat-" + row["id"]):
            row, data, _ = self.load(session, row["id"])
            if not row:
                raise ServiceError("conversation_changed", "The conversation context changed.", 409)
            key = body.clientTurnId or opaque("legacy_")
            fingerprint = hashlib.sha256(json.dumps(body.model_dump(exclude={"clientTurnId", "expectedRevision", "conversationId"}),
                                                      sort_keys=True).encode()).hexdigest()
            old = self.state.one("SELECT * FROM chat_turns WHERE conversation=? AND client_key=?", (row["id"], key))
            if old:
                if old["fingerprint"] != fingerprint:
                    raise ServiceError("idempotency_conflict", "This turn key belongs to different input.", 409)
                self.state.require_context(session)
                if old["response"]:
                    value = self.decode(old["response"])
                    if old["status"] == "failed":
                        raise ServiceError(**value)
                    return value
                linked = self.state.one("SELECT action_id FROM chat_action_links WHERE turn_id=?", (old["id"],))
                if linked:
                    action = public(self.actions.load(session, linked["action_id"])[1])
                    data["draft"] = self.action_draft(session, action)
                    session["_chatTurnId"] = old["id"]
                    reply = self.response(session, row, data, scope, "Recovered the saved review. Check its status before confirming.", "proposal", action=action)
                    return self.commit(session, row, data, old["id"], reply)
                raise ServiceError("chat_turn_interrupted", "This turn was interrupted before a review was saved. Explicitly restore the saved review before continuing.", 409)
            self.require_settled(row["id"])
            if body.expectedRevision is not None and body.expectedRevision != row["revision"]:
                raise ServiceError("conversation_changed", "A newer turn replaced these choices. Recover the current conversation.", 409)
            count = self.state.one("SELECT count(*) AS n FROM chat_turns WHERE conversation=?", (row["id"],))["n"]
            if count >= 40:
                raise ServiceError("conversation_limit", "This conversation reached its turn limit. Start a new conversation; existing actions remain recoverable.", 409)
            provider = chat_configuration(snapshot_for(self.vision, self.cfg))
            if data.get("provider") != provider:
                raise ServiceError("conversation_configuration_changed", "The chat provider changed. Start a new conversation before sending stored context.", 409)
            turn_id = opaque("turn_")
            session["_chatTurnId"], session["_chatConversationId"] = turn_id, row["id"]
            self.state.execute("INSERT INTO chat_turns VALUES(?,?,?,?,?,NULL,?)",
                               (turn_id, row["id"], key, fingerprint, "running", time.time()))
            try:
                reply = await self.dispatch(session, row, data, scope, body)
                self.state.require_context(session)
                return self.commit(session, row, data, turn_id, reply)
            except ServiceError as exc:
                self.state.execute("UPDATE chat_turns SET status='failed',response=? WHERE id=?",
                                   (self.encode({"code": exc.code, "message": exc.message, "status": exc.status, "details": exc.details}), turn_id))
                # Pending user fields survive same-context service failures.
                self.state.execute("UPDATE conversations SET data=? WHERE id=?", (self.encode(data), row["id"]))
                raise

    def commit(self, session, row, data, turn_id, reply):
        reply["revision"] = row["revision"] + 1
        if data.get("pending"):
            pending = copy.deepcopy(data["pending"])
            pending["id"] = opaque("cl_")
            for candidate in pending["candidates"]:
                candidate["id"] = opaque("ch_")
            data["pending"] = pending
            reply["clarification"] = copy.deepcopy(pending)
        bounded_choices(data, reply)
        data["lastReply"] = reply
        with self.state.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.state.require_context(session, db)
            db.execute("UPDATE conversations SET revision=?,expires=?,data=? WHERE id=?",
                       (reply["revision"], time.time() + self.cfg.conversation_ttl, self.encode(data), row["id"]))
            db.execute("UPDATE chat_turns SET status='done',response=? WHERE id=?", (self.encode(reply), turn_id))
        return reply
