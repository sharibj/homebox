import asyncio
import copy
import json
import multiprocessing
import time
from contextlib import suppress
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from assistant.clients import ServiceError
from assistant.main import PREFIX
from assistant.schemas import Intent, Message
from assistant.conversations import exhaustive_page
from test_api import FakeClients, G1, G2, confirm, setup
from test_capture_jobs import photo


def process_initial_turn(cfg, session, items, body, output):
    from assistant.actions import Actions
    from assistant.conversations import Conversations
    from assistant.state import State
    store, fake = State(cfg.state, cfg.instance_id), FakeClients()
    fake.items = items
    fake.memberships["Bearer alice"] = [session["collection"]]
    service = Conversations(cfg, store, fake, Actions(cfg, store, fake), None)
    try:
        output.put(("ok", asyncio.run(service.handle(session, Message(**body)))))
    except Exception as exc:
        output.put(("error", type(exc).__name__))


def turn(client, text=None, previous=None, **fields):
    body = {"clientTurnId": str(uuid4()), **fields}
    if text is not None:
        body["message"] = text
    if previous:
        body.update(conversationId=previous["conversationId"], expectedRevision=previous["revision"])
    response = client.post(PREFIX + "/chat", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def choose(client, previous, candidate=None):
    candidates = previous["clarification"]["candidates"]
    selected = candidate or next(c for c in candidates if c["kind"] != "unassigned")
    return turn(client, previous=previous, selection={"clarificationId": previous["clarification"]["id"],
                                                     "candidateId": selected["id"]})


def scoped_session(app):
    value = app.state.store.one("SELECT * FROM sessions")
    value["credential"] = "Bearer alice"
    value["_contextVersion"] = app.state.store.chat_context(value)["version"]
    return value


def test_exact_screenshot_sequence_preserves_name_draft_and_scope(setup):
    client, app, fake, _ = setup
    fake.add("Desk cable")
    destination = fake.add("Hall / Shelf", location=True)
    overview = turn(client, "Which inventory do I have?")
    assert overview["overview"]["itemRecordCount"] == 1
    assert overview["overview"]["locationCount"] == 1
    assert overview["scope"]["collectionId"] == G1
    assert "http" not in overview["scope"]["instanceLabel"]
    draft = turn(client, "Add a mobile charge to the entry.", overview)
    assert draft["pendingOperation"]["payload"]["name"] == "mobile charge"
    assert draft["pendingOperation"]["destinationQuery"] == "entry"
    assert draft["clarification"]["reason"] == "not_found"
    destinations = turn(client, "Which destinations do I have access to?", draft)
    assert destinations["pendingOperation"] == draft["pendingOperation"]
    assert destinations["clarification"]["id"] != draft["clarification"]["id"]
    assert destinations["revision"] == draft["revision"] + 1
    assert destinations["clarification"]["candidates"][0]["entityId"] == destination
    assert "destinations" not in destinations
    preview = choose(client, destinations)
    assert preview["action"]["payload"]["name"] == "mobile charge"
    assert preview["action"]["payload"]["locationId"] == destination
    assert fake.calls["create_item"] == 0
    assert fake.calls["search_items"] == 0


@pytest.mark.parametrize("phrase", ["What do I own?", "What inventory do I have?", "Show my inventory", "List all my items", "How many items do I have?"])
def test_overview_paraphrases_are_grounded(setup, phrase):
    client, _, fake, _ = setup
    fake.add("Cable")
    assert turn(client, phrase)["overview"]["itemRecordCount"] == 1
    assert fake.calls["inventory_overview"] == 1


def test_pending_side_question_quantity_reload_confirm_exactly_once(setup):
    client, app, fake, _ = setup
    fake.add("Entryway", location=True)
    draft = turn(client, "Add a mobile charge to the entry.")
    changed = turn(client, "make that two", draft)
    assert changed["pendingOperation"]["id"] == draft["pendingOperation"]["id"]
    assert changed["pendingOperation"]["payload"]["quantity"] == 2
    preview = choose(client, changed)
    first_action = preview["action"]
    browsed = turn(client, "Which destinations do I have access to?", preview, mode="read_only")
    assert browsed["action"] == first_action
    assert browsed["clarification"]["id"]
    restored = client.get(PREFIX + "/conversations/current").json()["conversation"]
    assert restored["action"] == first_action
    assert restored["pendingOperation"]["payload"]["quantity"] == 2
    typed_yes = turn(client, "yes", browsed)
    assert typed_yes["action"]["id"] == first_action["id"]
    assert fake.calls["create_item"] == 0
    completed = confirm(client, first_action)
    assert completed.json()["status"] == "succeeded"
    assert confirm(client, first_action).json() == completed.json()
    assert fake.calls["create_item"] == 1
    saved = fake.items[completed.json()["result"]["id"]]
    assert saved["name"] == "mobile charge" and saved["quantity"] == 2
    current = client.get(PREFIX + "/conversations/current").json()["conversation"]
    assert "pendingOperation" not in current and "action" not in current
    where = turn(client, "where is it?", typed_yes)
    assert where["items"][0]["id"] == completed.json()["result"]["id"]


def test_edit_replaces_immutable_preview_and_old_confirmation_is_rejected(setup):
    client, _, fake, _ = setup
    fake.add("Entry", location=True)
    preview = turn(client, "Add a mobile charge to the entry.")
    old = copy.deepcopy(preview["action"])
    updated = turn(client, "make that two", preview)
    assert updated["pendingOperation"]["id"] == preview["pendingOperation"]["id"]
    assert updated["action"]["id"] != old["id"]
    assert updated["action"]["payload"]["quantity"] == 2
    assert preview["action"] == old
    assert confirm(client, old).status_code == 409
    assert confirm(client, updated["action"]).json()["status"] == "succeeded"
    assert fake.calls["create_item"] == 1


def test_explicit_destination_choice_repreviews_proposed_action(setup):
    client, _, fake, _ = setup
    first, second = fake.add("Entry", location=True), fake.add("Garage", location=True)
    preview = turn(client, "Add a cable to Entry")
    browse = turn(client, "Show destinations", preview, mode="read_only")
    candidate = next(c for c in browse["clarification"]["candidates"] if c["entityId"] == second)
    updated = choose(client, browse, candidate)
    assert updated["action"]["payload"]["locationId"] == second
    assert updated["action"]["id"] != preview["action"]["id"]
    assert preview["action"]["payload"]["locationId"] == first
    assert confirm(client, preview["action"]).status_code == 409
    assert fake.calls["create_item"] == 0


def test_stale_selection_and_forged_candidate_do_not_read_or_propose(setup):
    client, _, fake, _ = setup
    fake.add("Entryway", location=True)
    draft = turn(client, "Add a cable to Entry")
    newer = turn(client, "Which destinations do I have access to?", draft)
    before = copy.deepcopy(fake.calls)
    response = client.post(PREFIX + "/chat", json={"conversationId": draft["conversationId"],
        "expectedRevision": draft["revision"], "clientTurnId": str(uuid4()),
        "selection": {"clarificationId": draft["clarification"]["id"], "candidateId": draft["clarification"]["candidates"][0]["id"]}})
    assert response.status_code == 409
    assert fake.calls == before
    forged = client.post(PREFIX + "/chat", json={"conversationId": newer["conversationId"],
        "expectedRevision": newer["revision"], "clientTurnId": str(uuid4()),
        "selection": {"clarificationId": newer["clarification"]["id"], "candidateId": fake.add("Elsewhere", location=True)}})
    assert forged.status_code == 409 and forged.json()["error"]["code"] == "clarification_expired"
    assert fake.calls == before


def test_lost_initial_response_deduplicates_without_conversation_id(setup):
    client, app, fake, _ = setup
    fake.add("Entry", location=True)
    body = {"message": "Add a cable to Entry", "clientTurnId": str(uuid4())}
    one = client.post(PREFIX + "/chat", json=body)
    calls = copy.deepcopy(fake.calls)
    two = client.post(PREFIX + "/chat", json=body)
    assert two.json() == one.json()
    assert fake.calls == calls
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 1
    conflict = client.post(PREFIX + "/chat", json={**body, "message": "Add a different cable to Entry"})
    assert conflict.status_code == 409 and conflict.json()["error"]["code"] == "idempotency_conflict"


def test_overlapping_initial_turns_allocate_one_conversation_and_action(setup):
    _, app, fake, _ = setup
    fake.add("Entry", location=True)
    session = scoped_session(app)
    body = Message(message="Add a cable to Entry", clientTurnId=str(uuid4()))
    async def run():
        return await asyncio.gather(*(app.state.conversations.handle(session, body) for _ in range(5)))
    values = asyncio.run(run())
    assert all(v == values[0] for v in values)
    assert app.state.store.one("SELECT count(*) AS n FROM conversations")["n"] == 1
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 1
    assert fake.calls["list_destinations_page"] == 2


def test_crash_between_action_and_turn_commit_recovers_link_not_new_proposal(setup, monkeypatch):
    client, app, fake, _ = setup
    fake.add("Entry", location=True)
    body = {"message": "Add a cable to Entry", "clientTurnId": str(uuid4())}
    original = app.state.conversations.commit
    def crash(*args):
        raise RuntimeError("synthetic process interruption")
    monkeypatch.setattr(app.state.conversations, "commit", crash)
    with pytest.raises(RuntimeError, match="synthetic process"):
        client.post(PREFIX + "/chat", json=body)
    monkeypatch.setattr(app.state.conversations, "commit", original)
    calls = copy.deepcopy(fake.calls)
    recovered = client.post(PREFIX + "/chat", json=body)
    assert recovered.status_code == 200
    assert recovered.json()["action"]["status"] == "proposed"
    assert fake.calls == calls
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 1
    assert app.state.store.one("SELECT status FROM chat_turns")["status"] == "done"


def test_reset_preserves_uncertain_action_and_blocks_old_initial_key(setup):
    client, app, fake, _ = setup
    fake.add("Entry", location=True)
    body = {"message": "Add a cable to Entry", "clientTurnId": str(uuid4())}
    preview = client.post(PREFIX + "/chat", json=body).json()
    fake.ambiguous_create = True
    assert confirm(client, preview["action"]).json()["status"] == "uncertain"
    reset = client.post(PREFIX + "/conversations/reset", json={}).json()
    assert reset["conversation"]["action"]["id"] == preview["action"]["id"]
    assert reset["conversation"]["action"]["status"] == "uncertain"
    assert client.post(PREFIX + "/chat", json=body).status_code == 409
    blocked = turn(client, "Add another cable to Entry", reset["conversation"])
    assert blocked["action"]["id"] == preview["action"]["id"]
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 1
    assert fake.calls["create_item"] == 1


def test_duplicate_entry_beyond_first_page_never_autoresolves(setup):
    client, _, fake, _ = setup
    fake.add("Entry", location=True)
    for i in range(19):
        fake.add(f"Entry shelf {i}", location=True)
    second = fake.add("Entry", location=True)
    first = turn(client, "Add a cable to Entry")
    assert "action" not in first
    assert len(first["clarification"]["candidates"]) == 20
    assert first["pagination"] == {"continuationId": first["pagination"]["continuationId"],
        "totalRecords": 21, "page": 1, "pageSize": 20, "hasMore": True, "complete": True}
    next_page = turn(client, previous=first, continuation={"id": first["pagination"]["continuationId"]})
    assert "action" not in next_page
    assert next_page["pagination"]["page"] == 2 and not next_page["pagination"]["hasMore"]
    assert next_page["clarification"]["candidates"][0]["entityId"] == second
    selected = choose(client, next_page)
    assert selected["action"]["payload"]["locationId"] == second
    assert fake.calls["create_item"] == 0


def test_no_local_exact_hit_on_page_one_is_not_not_found(setup):
    client, _, fake, _ = setup
    for i in range(20):
        fake.add(f"Entry shelf {i}", location=True)
    exact = fake.add("Entry", location=True)
    first = turn(client, "Add a cable to Entry")
    assert first["clarification"]["reason"] == "ambiguous"
    assert "action" not in first
    second = turn(client, previous=first, continuation={"id": first["pagination"]["continuationId"]})
    assert "action" not in second
    assert choose(client, second)["action"]["payload"]["locationId"] == exact


def test_location_and_item_with_same_name_require_explicit_choice(setup):
    client, _, fake, _ = setup
    location = fake.add("Entry", location=True)
    container = fake.add("Entry")
    response = turn(client, "Add a cable to Entry")
    assert "action" not in response
    assert {c["entityId"] for c in response["clarification"]["candidates"] if c["entityId"]} == {location, container}
    assert fake.calls["list_destinations_page"] == 2


@pytest.mark.parametrize("page,count,total,more,expected", [(1,1,1,False,True), (1,1,21,True,False),
    (2,1,21,False,False), (1,0,0,False,True), (1,0,21,True,False)])
def test_exhaustion_predicate_requires_complete_first_matching_page(page, count, total, more, expected):
    assert exhaustive_page({"page": page, "total": total, "hasMore": more, "complete": True}, [None] * count) is expected


def test_overview_counts_untyped_and_excludes_archived_and_other_collection(setup):
    client, _, fake, _ = setup
    for i in range(24):
        fake.add(f"Item {i}")
    archived = fake.add("Archived")
    fake.items[archived]["archived"] = True
    fake.add("Private", group=G2)
    fake.add("Room", location=True)
    result = turn(client, "Which inventory do I have?")
    assert result["overview"] == {"itemRecordCount": 24, "locationCount": 1, "includeArchived": False, "complete": True}
    assert len(result["items"]) == 20 and result["pagination"]["hasMore"]
    second = turn(client, previous=result, continuation={"id": result["pagination"]["continuationId"]})
    assert len(second["items"]) == 4
    assert not second["pagination"]["hasMore"]
    assert len({v["id"] for page in (result, second) for v in page["items"]}) == 24


def test_genuine_empty_collections_and_collections_memberships_are_distinct(setup):
    client, _, fake, _ = setup
    fake.add("Private record", group=G2)
    result = turn(client, "Which inventory do I have?")
    assert result["overview"]["itemRecordCount"] == 0
    assert "no active item" in result["message"]
    collections = turn(client, "Which collections do I have?", result)
    assert {g["id"] for g in collections["collections"]} == {G1, G2}
    assert next(g for g in collections["collections"] if g["current"])["id"] == G1
    assert all(item["name"] != "Private record" for item in collections["items"])


@pytest.mark.parametrize("code,status", [("mcp_unavailable",503), ("homebox_access_denied",403)])
def test_inventory_failure_is_not_successful_empty(setup, monkeypatch, code, status):
    client, _, fake, _ = setup
    async def failure(*args):
        raise ServiceError(code, "Synthetic inventory read failure", status)
    monkeypatch.setattr(fake, "tool", failure)
    result = client.post(PREFIX + "/chat", json={"message": "Which inventory do I have?"})
    assert result.status_code == status and result.json()["error"]["code"] == code
    assert "overview" not in result.json()


def test_short_or_inconsistent_page_fails_closed(setup, monkeypatch):
    client, _, fake, _ = setup
    async def invalid(*args):
        return {"items": [], "total": 21, "page": 1, "pageSize": 20, "hasMore": True, "complete": True}
    monkeypatch.setattr(fake, "tool", invalid)
    result = client.post(PREFIX + "/chat", json={"message": "Find cable"})
    assert result.status_code == 503 and result.json()["error"]["code"] == "incomplete_inventory_read"


def test_a_to_b_to_a_rejects_old_context_and_selection_before_reads(setup):
    client, _, fake, login = setup
    fake.add("Entryway", location=True)
    old_version = login.json()["scope"]["contextVersion"]
    client.headers["X-Context-Version"] = old_version
    old = turn(client, "Add a cable to Entry")
    b = client.post(PREFIX + "/session/collection", json={"collectionId": G2}).json()
    client.headers.update({"X-Collection-ID": G2, "X-Context-Version": b["scope"]["contextVersion"]})
    a = client.post(PREFIX + "/session/collection", json={"collectionId": G1}).json()
    client.headers.update({"X-Collection-ID": G1, "X-Context-Version": old_version})
    before = copy.deepcopy(fake.calls)
    assert client.post(PREFIX + "/chat", json={"message": "Which inventory do I have?"}).status_code == 409
    assert fake.calls == before
    client.headers["X-Context-Version"] = a["scope"]["contextVersion"]
    stale = client.post(PREFIX + "/chat", json={"message": "make that two", "conversationId": old["conversationId"],
        "expectedRevision": old["revision"], "clientTurnId": str(uuid4())})
    assert stale.status_code == 409 and fake.calls == before
    assert client.get(PREFIX + "/conversations/current").json()["conversation"] is None


def test_conversation_cannot_cross_browser_session_or_revoked_membership(setup):
    client, app, fake, _ = setup
    fake.add("Entryway", location=True)
    original = turn(client, "Add a cable to Entry")
    with TestClient(app, base_url="https://inventory.test", headers={"Origin": app.state.config.origin}) as other:
        login = other.post(PREFIX + "/auth/login", json={"username":"alice","password":"synthetic"}).json()
        other.headers.update({"X-CSRF-Token":login["csrfToken"],"X-Collection-ID":G1})
        response = other.post(PREFIX + "/chat", json={"message":"make that two","conversationId":original["conversationId"]})
        assert response.status_code == 409
        assert other.get(PREFIX + "/conversations/current").json()["conversation"] is None
    fake.memberships["Bearer alice"] = [G2]
    assert client.get(PREFIX + "/conversations/current").status_code == 403


def test_chat_configuration_reset_is_independent_of_vision(setup):
    client, app, fake, _ = setup
    original = turn(client, "Which inventory do I have?")
    cfg = app.state.config
    cfg.ai_roles = replace(cfg.ai_roles, vision=replace(cfg.ai_roles.vision, model="synthetic-other-vision"))
    unchanged = turn(client, "Show my inventory", original)
    cfg.ai_roles = replace(cfg.ai_roles, chat=replace(cfg.ai_roles.chat, model="synthetic-other-chat"))
    response = client.post(PREFIX + "/chat", json={"message":"Show my inventory",
        "conversationId":unchanged["conversationId"],"expectedRevision":unchanged["revision"],"clientTurnId":str(uuid4())})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "conversation_configuration_changed"
    assert client.post(PREFIX + "/conversations/reset", json={}).status_code == 200
    assert turn(client, "Show my inventory")["overview"]["itemRecordCount"] == 0


def test_conversation_payload_and_reply_are_encrypted_and_expire(setup):
    client, app, fake, _ = setup
    fake.add("Entryway", location=True)
    value = turn(client, "Add a synthetic secret item to Entry")
    store = app.state.store
    assert "synthetic secret" not in store.one("SELECT data FROM conversations")["data"]
    assert "synthetic secret" not in store.one("SELECT response FROM chat_turns")["response"]
    store.execute("UPDATE conversations SET expires=?", (time.time() - 1000,))
    assert client.get(PREFIX + "/conversations/current").json() == {"conversation":None,"expired":True}
    app.state.conversations.cleanup()
    assert store.one("SELECT count(*) AS n FROM conversations")["n"] == 0
    assert store.one("SELECT count(*) AS n FROM chat_turns")["n"] == 0


def test_read_only_cannot_edit_or_select_pending_write(setup):
    client, _, fake, _ = setup
    fake.add("Entry", location=True)
    value = turn(client, "Add a cable to Entry")
    read_only = turn(client, "make that two", value, mode="read_only")
    assert read_only["action"] == value["action"]
    assert read_only["pendingOperation"]["payload"]["quantity"] == 1
    assert fake.calls["create_item"] == 0


def test_new_write_does_not_replace_existing_pending_operation(setup):
    client, _, fake, _ = setup
    fake.add("Entryway", location=True)
    first = turn(client, "Add a cable to Entry")
    second = turn(client, "Add a charger to Entryway", first)
    assert second["pendingOperation"] == first["pendingOperation"]
    assert "action" not in second
    assert fake.calls["create_item"] == 0


def test_where_is_it_uses_exact_last_reference(setup):
    client, _, fake, _ = setup
    eid = fake.add("Cable")
    fake.items[eid]["locationPath"] = "Room / Shelf"
    found = turn(client, "Find Cable")
    reference = turn(client, "where is it?", found)
    assert reference["items"][0]["id"] == eid
    assert "Room / Shelf" in reference["message"]
    assert fake.calls["get_entity_card"] == 1


def test_contents_ambiguity_continues_to_bounded_location_filter(setup):
    client, _, fake, _ = setup
    first, second = fake.add("Room", location=True), fake.add("Room", location=True)
    eid = fake.add("Cable")
    fake.items[eid]["locationId"] = second
    question = turn(client, "What is in Room?")
    chosen = next(c for c in question["clarification"]["candidates"] if c["entityId"] == second)
    result = choose(client, question, chosen)
    assert [v["id"] for v in result["items"]] == [eid]
    assert fake.calls["location_contents"] == fake.calls["search_items"] == 0


def test_whole_turn_timeout_includes_inventory_wait_and_does_not_repeat(setup, monkeypatch):
    client, app, fake, _ = setup
    app.state.config.chat_timeout = .025
    async def slow(*args):
        await asyncio.sleep(.1)
        raise AssertionError("timed out call resumed")
    monkeypatch.setattr(fake, "tool", slow)
    body = {"message":"Which inventory do I have?","clientTurnId":str(uuid4())}
    started = time.monotonic()
    result = client.post(PREFIX + "/chat", json=body)
    assert result.status_code == 503 and result.json()["error"]["code"] == "chat_timed_out"
    assert time.monotonic() - started < .5
    repeated = client.post(PREFIX + "/chat", json=body)
    assert repeated.status_code == 409 and repeated.json()["error"]["code"] == "chat_turn_interrupted"


def test_initial_idempotency_serializes_across_os_processes(setup):
    _, app, fake, _ = setup
    fake.add("Entry", location=True)
    session = scoped_session(app)
    ctx = multiprocessing.get_context("spawn")
    output = ctx.Queue()
    body = {"message":"Add a cable to Entry","clientTurnId":str(uuid4())}
    processes = [ctx.Process(target=process_initial_turn,
        args=(app.state.config, session, fake.items, body, output)) for _ in range(3)]
    for process in processes:
        process.start()
    values = [output.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert all(status == "ok" for status, _ in values), values
    assert all(value == values[0][1] for _, value in values)
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 1
    assert app.state.store.one("SELECT count(*) AS n FROM conversations")["n"] == 1


def test_running_confirmation_wins_race_without_replacement_proposal(setup):
    client, app, fake, _ = setup
    fake.add("Entry", location=True)
    first = turn(client, "Add a cable to Entry")
    session = scoped_session(app)
    fake.create_delay = .05
    async def run():
        confirmation = asyncio.create_task(app.state.actions.confirm(session, first["action"]["id"], 1, first["action"]["id"]))
        await asyncio.sleep(.005)
        try:
            await app.state.conversations.handle(session, Message(message="make that two", conversationId=first["conversationId"],
                expectedRevision=first["revision"], clientTurnId=str(uuid4())))
        except ServiceError as exc:
            assert exc.code == "write_pending"
        else:
            raise AssertionError("Running action was replaced")
        return await confirmation
    saved = asyncio.run(run())
    assert saved["status"] == "succeeded"
    assert fake.calls["create_item"] == 1
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 1
    assert fake.items[saved["result"]["id"]]["quantity"] == 1


def test_context_rotation_during_read_never_commits_old_reply(setup, monkeypatch):
    client, app, fake, _ = setup
    original = fake.tool
    async def rotating(session, name, arguments=None):
        result = await original(session, name, arguments)
        app.state.store.chat_context(session, rotate=True)
        return result
    monkeypatch.setattr(fake, "tool", rotating)
    result = client.post(PREFIX + "/chat", json={"message":"Which inventory do I have?","clientTurnId":str(uuid4())})
    assert result.status_code == 409 and result.json()["error"]["code"] == "COLLECTION_CONTEXT_CHANGED"
    assert client.get(PREFIX + "/conversations/current").json()["conversation"] is None
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 0


def test_unrelated_side_read_refreshes_pending_choice_generation(setup):
    client, _, fake, _ = setup
    fake.add("Entryway", location=True)
    fake.add("Cable")
    first = turn(client, "Add a charger to Entry")
    side = turn(client, "Find Cable", first, mode="read_only")
    assert side["pendingOperation"] == first["pendingOperation"]
    assert side["clarification"]["id"] != first["clarification"]["id"]
    assert side["clarification"]["candidates"][0]["entityId"] == first["clarification"]["candidates"][0]["entityId"]


@pytest.mark.parametrize("phrase", ["make that 0", "make that 1000001"])
def test_invalid_local_quantity_is_typed_error_not_server_failure(setup, phrase):
    client, _, fake, _ = setup
    fake.add("Entry", location=True)
    first = turn(client, "Add a cable to Entry")
    result = client.post(PREFIX + "/chat", json={"message":phrase,"conversationId":first["conversationId"],
        "expectedRevision":first["revision"],"clientTurnId":str(uuid4())})
    assert result.status_code == 422 and result.json()["error"]["code"] == "invalid_intent"
    assert confirm(client, first["action"]).json()["status"] == "succeeded"


def test_that_one_resolves_current_candidate_without_creating_inventory(setup):
    client, _, fake, _ = setup
    fake.add("Entryway", location=True)
    first = turn(client, "Add a cable to Entry")
    # Includes explicit unassigned, so an unqualified pronoun remains ambiguous.
    unresolved = turn(client, "that one", first)
    assert "action" not in unresolved
    selected = turn(client, "the first", unresolved)
    assert selected["action"]["payload"]["name"] == "cable"
    assert fake.calls["create_item"] == 0


def test_turn_and_cursor_bounds_are_enforced(setup):
    client, app, fake, _ = setup
    for i in range(30):
        fake.add(f"Cable {i}")
    result = turn(client, "Find Cable")
    # Explicitly exercise forty stored turn records without tripping HTTP rate limiting.
    session = scoped_session(app)
    async def run():
        current = result
        for _ in range(39):
            current = await app.state.conversations.handle(session, Message(message="Find Cable",
                conversationId=current["conversationId"],expectedRevision=current["revision"],clientTurnId=str(uuid4())))
        with pytest.raises(ServiceError) as error:
            await app.state.conversations.handle(session, Message(message="Find Cable",
                conversationId=current["conversationId"],expectedRevision=current["revision"],clientTurnId=str(uuid4())))
        assert error.value.code == "conversation_limit"
        return current
    asyncio.run(run())
    row = app.state.store.one("SELECT data FROM conversations")
    data = app.state.conversations.decode(row["data"])
    assert len(data["pages"]) == 5
    assert len(data["lastEntities"]) == 20


def test_move_uses_exactly_four_total_inventory_calls_including_validation(setup):
    client, _, fake, _ = setup
    item, target = fake.add("Cable"), fake.add("Entry", location=True)
    preview = turn(client, "Move Cable to Entry")
    assert preview["action"]["payload"] == {"entityId":item,"locationId":target}
    assert sum(fake.calls.values()) == 4
    assert fake.calls["list_items_page"] == 1 and fake.calls["list_destinations_page"] == 2
    assert fake.calls["get_entity_cards"] == 1
    assert fake.calls["get_entity"] == 0
    assert confirm(client, preview["action"]).json()["status"] == "succeeded"
    assert fake.calls["move_item"] == 1


def test_descendant_and_self_move_rejected_before_any_proposal_or_write(setup):
    client, app, fake, _ = setup
    parent, child, grandchild = fake.add("Trunk"), fake.add("Box"), fake.add("Pouch")
    fake.items[child]["parentId"] = parent
    fake.items[grandchild]["parentId"] = child
    response = client.post(PREFIX + "/chat", json={"message":"Move Trunk to Pouch"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "invalid_move"
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 0
    assert fake.calls["move_item"] == 0
    client.post(PREFIX + "/conversations/reset", json={})
    own = client.post(PREFIX + "/chat", json={"message":"Move Trunk to Trunk"})
    assert own.status_code == 422 and own.json()["error"]["code"] == "invalid_move"
    assert fake.calls["move_item"] == 0


def test_missing_ancestry_and_short_batch_fail_closed(setup, monkeypatch):
    client, _, fake, _ = setup
    fake.add("Trunk")
    fake.add("Box")
    original = fake.tool
    async def missing(session, name, arguments=None):
        value = await original(session, name, arguments)
        if name == "get_entity_cards":
            for card in value["items"]:
                card.pop("ancestorIds", None)
        return value
    monkeypatch.setattr(fake, "tool", missing)
    response = client.post(PREFIX + "/chat", json={"message":"Move Trunk to Box"})
    assert response.status_code == 503 and response.json()["error"]["code"] == "destination_path_unavailable"
    assert fake.calls["move_item"] == 0


def test_four_call_budget_defers_preview_to_explicit_review_turn(setup):
    _, app, fake, _ = setup
    target = fake.add("Entry", location=True)
    session = scoped_session(app)
    session.update(_chatReads=4,_chatMode="normal",_chatTurnId="test_turn",_chatConversationId="unused")
    row, _, _ = app.state.conversations.load(session, create=True)
    data = app.state.conversations.fresh_data(session)
    data["draft"] = {"id":"op_test","type":"item.create","payload":{"name":"Cable","locationId":target},"status":"draft"}
    async def run():
        scope = await app.state.conversations.scope(session)
        reply = await app.state.conversations.propose(session, row, data, scope)
        assert "review draft" in reply["message"] and reply["clarification"]["slot"] == "review"
        assert sum(fake.calls.values()) == 0
        retained = copy.deepcopy(data)
        app.state.store.execute("UPDATE conversations SET data=? WHERE id=?", (app.state.conversations.encode(retained),row["id"]))
        return await app.state.conversations.handle(session,Message(message="review draft",conversationId=row["id"],
            expectedRevision=0,clientTurnId=str(uuid4())))
    reply = asyncio.run(run())
    assert reply["action"]["payload"]["locationId"] == target
    assert fake.calls["get_entity_cards"] == 1


def test_item_selection_on_last_page_remains_explicit(setup):
    client, _, fake, _ = setup
    for _ in range(20):
        fake.add("Cable")
    selected = fake.add("Cable")
    dest = fake.add("Entry", location=True)
    first = turn(client, "Move Cable to Entry")
    second = turn(client, previous=first, continuation={"id":first["pagination"]["continuationId"]})
    assert "action" not in second
    assert second["clarification"]["candidates"][0]["entityId"] == selected
    assert choose(client, second)["action"]["payload"] == {"entityId":selected,"locationId":dest}


def test_contents_selection_page_two_has_fresh_page_two_candidates(setup):
    client, _, fake, _ = setup
    for _ in range(20):
        fake.add("Room", location=True)
    selected = fake.add("Room", location=True)
    item = fake.add("Cable")
    fake.items[item]["locationId"] = selected
    first = turn(client, "What is in Room?")
    second = turn(client, previous=first, continuation={"id":first["pagination"]["continuationId"]})
    assert second["clarification"]["slot"] == "contents"
    assert second["clarification"]["candidates"][0]["entityId"] == selected
    assert [v["id"] for v in choose(client, second)["items"]] == [item]


def test_item_container_contents_is_explicitly_unsupported(setup):
    client, _, fake, _ = setup
    fake.add("Trunk")
    reply = turn(client, "What is in Trunk?")
    assert reply["outcome"] == "unsupported"
    assert "not item containers" in reply["message"]
    assert fake.calls["location_contents"] == 0


def test_full_405_record_catalogue_browses_without_duplicates_or_turn_limit(setup):
    client, app, fake, _ = setup
    expected = set()
    for index in range(405):
        eid = fake.add(f"Legacy untyped {index}")
        fake.items[eid]["quantity"] = 9
        expected.add(eid)
    archived = fake.add("Archived item")
    fake.items[archived]["archived"] = True
    fake.add("Private", group=G2)
    first = turn(client, "Which inventory do I have?")
    assert first["overview"]["itemRecordCount"] == 405
    assert len(first["items"]) == 20
    session = scoped_session(app)
    async def run():
        current, ids, pages = first, [v["id"] for v in first["items"]], 1
        while current["pagination"]["hasMore"]:
            current = await app.state.conversations.handle(session, Message(
                conversationId=current["conversationId"], expectedRevision=current["revision"], clientTurnId=str(uuid4()),
                continuation={"id":current["pagination"]["continuationId"]}))
            ids.extend(v["id"] for v in current["items"])
            assert len(current["items"]) <= 20
            assert current["pagination"]["totalRecords"] == 405
            pages += 1
        return ids, pages
    ids, pages = asyncio.run(run())
    assert len(ids) == len(set(ids)) == 405 and set(ids) == expected and pages == 21
    assert fake.calls["inventory_overview"] == 1 and fake.calls["list_items_page"] == 20
    assert fake.calls["search_items"] == 0


def test_timeout_includes_session_lock_wait(setup):
    _, app, _, _ = setup
    session = scoped_session(app)
    app.state.config.chat_timeout = .02
    async def run():
        async with app.state.store.collection_lock("chat-session-" + session["id"]):
            started = time.monotonic()
            with pytest.raises(ServiceError) as error:
                await app.state.conversations.handle(session, Message(message="Which inventory do I have?"))
            assert error.value.code == "chat_timed_out"
            assert time.monotonic() - started < .5
    asyncio.run(run())
    assert app.state.store.one("SELECT count(*) AS n FROM conversations")["n"] == 0


@pytest.mark.parametrize("initial_kind", ["locations", "items"])
def test_bare_destination_label_cannot_resolve_from_a_truncated_page(setup, initial_kind):
    client, _, fake, _ = setup
    location = initial_kind == "locations"
    fake.add("Entry", location=location)
    for index in range(19):
        fake.add(f"Entry shelf {index}", location=location)
    second = fake.add("Entry", location=location)
    first = turn(client, "Add a cable to Entry")
    assert first["pagination"]["totalRecords"] == 21
    repeated = turn(client, "Entry", first)
    assert repeated["outcome"] == "clarification" and "action" not in repeated
    assert repeated["pagination"]["hasMore"] and repeated["pagination"]["totalRecords"] == 21
    last = turn(client, previous=repeated, continuation={"id":repeated["pagination"]["continuationId"]})
    bare_last = turn(client, "Entry", last)
    assert "action" not in bare_last and bare_last["pagination"]["page"] == 1
    last_again = turn(client, previous=bare_last, continuation={"id":bare_last["pagination"]["continuationId"]})
    assert choose(client, last_again)["action"]["payload"]["locationId"] == second
    assert fake.calls["create_item"] == 0


def test_bare_destination_label_rechecks_both_kinds_after_location_browse(setup):
    client, _, fake, _ = setup
    fake.add("Entryway", location=True)
    fake.add("Entry", location=True)
    container = fake.add("Entry")
    draft = turn(client, "Add a cable to Missing")
    assert sum(c["label"] == "Entry" for c in draft["clarification"]["candidates"]) == 1
    reply = turn(client, "Entry", draft)
    assert "action" not in reply
    assert reply["clarification"]["reason"] == "ambiguous"
    assert any(c["entityId"] == container for c in reply["clarification"]["candidates"])


def test_bare_unique_destination_label_can_propose_after_exhaustive_lookup(setup):
    client, _, fake, _ = setup
    target = fake.add("Entryway", location=True)
    first = turn(client, "Add a cable to Entry")
    reply = turn(client, "Entryway", first)
    assert reply["action"]["payload"]["locationId"] == target
    assert fake.calls["create_item"] == 0


def test_bare_item_label_cannot_resolve_from_partial_item_candidates(setup):
    client, _, fake, _ = setup
    fake.add("Cable")
    for index in range(19):
        fake.add(f"Cable spare {index}")
    fake.add("Cable")
    fake.add("Entry", location=True)
    first = turn(client, "Move Cable to Entry")
    reply = turn(client, "Cable", first)
    assert "action" not in reply
    assert reply["pagination"]["totalRecords"] == 21 and reply["pagination"]["hasMore"]
    assert fake.calls["get_entity_cards"] == fake.calls["move_item"] == 0


def test_repeated_overview_uses_the_same_five_cursor_bound(setup):
    client, app, fake, _ = setup
    for index in range(25):
        fake.add(f"Item {index}")
    current, cursors = None, []
    for _ in range(7):
        current = turn(client, "Which inventory do I have?", current)
        cursors.append(current["pagination"]["continuationId"])
        data = app.state.conversations.decode(app.state.store.one("SELECT data FROM conversations")["data"])
        assert len(data["pages"]) <= 5
    assert set(data["pages"]) == set(cursors[-5:])
    stale = client.post(PREFIX + "/chat", json={"conversationId":current["conversationId"],
        "expectedRevision":current["revision"],"clientTurnId":str(uuid4()),"continuation":{"id":cursors[0]}})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "clarification_expired"
    last = turn(client, previous=current, continuation={"id":cursors[-1]})
    assert len(last["items"]) == 5


async def login_async(client):
    login = (await client.post(PREFIX + "/auth/login", json={"username":"alice","password":"synthetic"})).json()
    client.headers.update({"X-CSRF-Token":login["csrfToken"], "X-Collection-ID":login["collectionId"],
                           "X-Context-Version":login["scope"]["contextVersion"]})


def test_current_http_waits_for_inflight_edit_and_returns_only_the_final_review(setup, monkeypatch):
    _, app, fake, _ = setup
    fake.add("Entry", location=True)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def paused(message, context=None):
            entered.set()
            await release.wait()
            return Intent(kind="draft.edit", changes={"quantity":2})
        monkeypatch.setattr(app.state.vision, "intent", paused)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=app.state.config.origin,
                                     headers={"Origin":app.state.config.origin}) as client:
            await login_async(client)
            first = (await client.post(PREFIX + "/chat", json={"message":"Add a cable to Entry","clientTurnId":str(uuid4())})).json()
            body = {"message":"Please adjust the pending draft quantity.","conversationId":first["conversationId"],
                    "expectedRevision":first["revision"],"clientTurnId":str(uuid4())}
            editing = asyncio.create_task(client.post(PREFIX + "/chat",json=body))
            await asyncio.wait_for(entered.wait(), 1)
            # A paused model call holds no SQLite write transaction.
            assert await asyncio.wait_for(asyncio.to_thread(app.state.store.execute,
                "INSERT INTO metadata VALUES(?,?)", ("concurrent-recovery-probe","synthetic")), 1) == 1
            recovery = asyncio.create_task(client.get(PREFIX + "/conversations/current"))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(recovery), .03)
            replay = asyncio.create_task(client.post(PREFIX + "/chat",json=body))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(replay), .03)
            release.set()
            edited, restored, replayed = await asyncio.gather(editing,recovery,replay)
            assert edited.status_code == restored.status_code == replayed.status_code == 200
            assert edited.json() == replayed.json()
            current = restored.json()["conversation"]
            assert current["revision"] == edited.json()["revision"]
            assert current["action"]["id"] == edited.json()["action"]["id"] != first["action"]["id"]
            assert current["action"]["payload"]["quantity"] == 2
            old = await client.post(PREFIX + f"/actions/{first['action']['id']}/confirm",json={"revision":1},
                                    headers={"Idempotency-Key":first["action"]["confirmationKey"]})
            assert old.status_code == 409
    asyncio.run(run())
    assert fake.calls["create_item"] == 0


def test_interrupted_http_edit_blocks_reload_until_explicit_saved_review_restore(setup, monkeypatch):
    _, app, fake, _ = setup
    fake.add("Entry", location=True)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def paused(message, context=None):
            entered.set()
            await release.wait()
            return Intent(kind="draft.edit", changes={"quantity":2})
        monkeypatch.setattr(app.state.vision, "intent", paused)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=app.state.config.origin,
                                     headers={"Origin":app.state.config.origin}) as client:
            await login_async(client)
            first = (await client.post(PREFIX + "/chat",json={"message":"Add a cable to Entry","clientTurnId":str(uuid4())})).json()
            body = {"message":"Please adjust the pending draft quantity.","conversationId":first["conversationId"],
                    "expectedRevision":first["revision"],"clientTurnId":str(uuid4())}
            editing = asyncio.create_task(client.post(PREFIX + "/chat",json=body))
            await asyncio.wait_for(entered.wait(), 1)
            editing.cancel()
            with suppress(asyncio.CancelledError):
                await editing
            # A fresh page has none of the controller's in-memory request map.
            for _ in range(2):
                current = await client.get(PREFIX + "/conversations/current")
                assert current.status_code == 409 and current.json()["error"]["code"] == "chat_turn_interrupted"
                assert "conversation" not in current.json()
            retry = await client.post(PREFIX + "/chat",json=body)
            assert retry.status_code == 409 and retry.json()["error"]["code"] == "chat_turn_interrupted"
            skipped = await client.post(PREFIX + "/chat",json={"message":"Show my inventory","clientTurnId":str(uuid4())})
            assert skipped.status_code == 409 and skipped.json()["error"]["code"] == "chat_turn_interrupted"
            assert fake.calls["create_item"] == 0
            restored = await client.post(PREFIX + "/conversations/reset",json={})
            assert restored.status_code == 200
            saved = restored.json()["conversation"]
            assert saved["conversationId"] != first["conversationId"]
            assert saved["action"]["id"] == first["action"]["id"]
            assert saved["action"]["payload"]["quantity"] == 1
            assert "Restored the saved review" in saved["message"] and "No request was repeated" in saved["message"]
            assert (await client.get(PREFIX + "/conversations/current")).status_code == 200
            confirmed = await client.post(PREFIX + f"/actions/{saved['action']['id']}/confirm",json={"revision":1},
                                          headers={"Idempotency-Key":saved["action"]["confirmationKey"]})
            assert confirmed.json()["status"] == "succeeded"
    asyncio.run(run())
    assert fake.calls["create_item"] == 1


def test_current_http_busy_lock_returns_error_not_old_preview(setup):
    _, app, fake, _ = setup
    app.state.config.chat_timeout = .03
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=app.state.config.origin,
                                     headers={"Origin":app.state.config.origin}) as client:
            await login_async(client)
            session = app.state.store.one("SELECT * FROM sessions ORDER BY rowid DESC LIMIT 1")
            async with app.state.store.collection_lock("chat-session-" + session["id"]):
                result = await client.get(PREFIX + "/conversations/current")
                assert result.status_code == 503 and result.json()["error"]["code"] == "conversation_busy"
                assert "conversation" not in result.json()
            assert (await client.get(PREFIX + "/conversations/current")).status_code == 200
    asyncio.run(run())
    assert fake.calls["create_item"] == 0


def test_reload_and_explicit_reset_after_linked_edit_crash_restore_new_action(setup, monkeypatch):
    client, app, fake, _ = setup
    fake.add("Entry", location=True)
    original = turn(client, "Add a cable to Entry")
    commit = app.state.conversations.commit
    def crash(*args):
        raise RuntimeError("synthetic linked edit interruption")
    monkeypatch.setattr(app.state.conversations, "commit", crash)
    with pytest.raises(RuntimeError, match="synthetic linked edit"):
        client.post(PREFIX + "/chat",json={"message":"make that two","conversationId":original["conversationId"],
            "expectedRevision":original["revision"],"clientTurnId":str(uuid4())})
    monkeypatch.setattr(app.state.conversations, "commit", commit)
    linked = app.state.store.one("SELECT action_id FROM chat_action_links ORDER BY rowid DESC LIMIT 1")["action_id"]
    assert linked != original["action"]["id"]
    calls = copy.deepcopy(fake.calls)
    current = client.get(PREFIX + "/conversations/current")
    assert current.status_code == 409 and current.json()["error"]["code"] == "chat_turn_interrupted"
    assert "conversation" not in current.json()
    reset = client.post(PREFIX + "/conversations/reset",json={})
    assert reset.status_code == 200
    restored = reset.json()["conversation"]
    assert restored["action"]["id"] == linked
    assert restored["action"]["payload"]["quantity"] == 2
    assert "Restored the saved review" in restored["message"]
    assert client.get(PREFIX + "/conversations/current").json()["conversation"]["action"]["id"] == linked
    assert fake.calls == calls
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 2
    assert client.get(PREFIX + f"/actions/{original['action']['id']}").json()["status"] == "cancelled"
    assert fake.calls["create_item"] == 0


def test_reset_and_current_http_wait_for_edit_and_restore_only_its_new_action(setup, monkeypatch):
    _, app, fake, _ = setup
    fake.add("Entry", location=True)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def paused(message, context=None):
            entered.set()
            await release.wait()
            return Intent(kind="draft.edit",changes={"quantity":2})
        monkeypatch.setattr(app.state.vision, "intent", paused)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=app.state.config.origin,
                                     headers={"Origin":app.state.config.origin}) as client:
            await login_async(client)
            first = (await client.post(PREFIX + "/chat",json={"message":"Add a cable to Entry","clientTurnId":str(uuid4())})).json()
            editing = asyncio.create_task(client.post(PREFIX + "/chat",json={"message":"Please adjust the pending draft quantity.",
                "conversationId":first["conversationId"],"expectedRevision":first["revision"],"clientTurnId":str(uuid4())}))
            await asyncio.wait_for(entered.wait(), 1)
            reset = asyncio.create_task(client.post(PREFIX + "/conversations/reset",json={}))
            current = asyncio.create_task(client.get(PREFIX + "/conversations/current"))
            for pending in (reset,current):
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(pending), .03)
            release.set()
            edited, restored, observed = await asyncio.wait_for(asyncio.gather(editing,reset,current), 2)
            assert edited.status_code == restored.status_code == observed.status_code == 200
            new_id = edited.json()["action"]["id"]
            assert new_id != first["action"]["id"]
            assert restored.json()["conversation"]["action"]["id"] == observed.json()["conversation"]["action"]["id"] == new_id
            assert restored.json()["conversation"]["action"]["payload"]["quantity"] == 2
    asyncio.run(run())
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 2
    assert fake.calls["create_item"] == 0


def test_initial_current_http_waits_for_initial_turn_before_allocating_recovery_state(setup, monkeypatch):
    _, app, fake, _ = setup
    fake.add("Entry",location=True)
    async def run():
        entered, release = asyncio.Event(), asyncio.Event()
        async def paused(message, context=None):
            entered.set()
            await release.wait()
            return Intent(kind="item.create",query="Cable",destination="Entry")
        monkeypatch.setattr(app.state.vision, "intent", paused)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=app.state.config.origin,
                                     headers={"Origin":app.state.config.origin}) as client:
            await login_async(client)
            initial = asyncio.create_task(client.post(PREFIX + "/chat",json={
                "message":"Please prepare this new inventory record.","clientTurnId":str(uuid4())}))
            await asyncio.wait_for(entered.wait(), 1)
            current = asyncio.create_task(client.get(PREFIX + "/conversations/current"))
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(current), .03)
            release.set()
            posted, recovered = await asyncio.wait_for(asyncio.gather(initial,current), 2)
            assert posted.status_code == recovered.status_code == 200
            assert recovered.json()["conversation"]["conversationId"] == posted.json()["conversationId"]
            assert recovered.json()["conversation"]["action"]["id"] == posted.json()["action"]["id"]
    asyncio.run(run())
    assert app.state.store.one("SELECT count(*) AS n FROM conversations")["n"] == 1
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 1
    assert fake.calls["create_item"] == 0


@pytest.mark.parametrize("with_photo", [False,True])
def test_chat_preview_blocks_manual_and_photo_proposals_without_losing_drafts(setup, with_photo):
    client, app, fake, _ = setup
    fake.add("Entry",location=True)
    existing = turn(client, "Add a chat cable to Entry")["action"]
    request = {"type":"item.create","payload":{"name":"Manual photo draft","quantity":3,"notes":"Keep these fields"}}
    capture = None
    if with_photo:
        job = client.post(PREFIX + "/capture-jobs",files={"image":("synthetic.jpg",photo(),"image/jpeg")},
                          headers={"Idempotency-Key":str(uuid4())})
        assert job.status_code == 202
        request["captureId"] = job.json()["captureId"]
        capture = app.state.store.one("SELECT * FROM captures WHERE id=?", (request["captureId"],))
        assert (app.state.config.uploads / capture["filename"]).exists()
    submitted = copy.deepcopy(request)
    blocked = client.post(PREFIX + "/actions",json=request)
    assert blocked.status_code == 409 and blocked.json()["error"]["code"] == "write_pending"
    assert blocked.json()["error"]["details"]["actionId"] == existing["id"]
    assert request == submitted
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 1
    assert client.get(PREFIX + f"/actions/{existing['id']}").json()["status"] == "proposed"
    if capture:
        retained = app.state.store.one("SELECT * FROM captures WHERE id=?", (capture["id"],))
        assert retained["action_id"] is None and retained["filename"] == capture["filename"]
        assert (app.state.config.uploads / capture["filename"]).exists()
    assert fake.calls["create_item"] == fake.calls["upload"] == 0


@pytest.mark.parametrize("with_photo", [False,True])
def test_manual_or_photo_review_is_recovered_not_replaced_by_new_chat_write(setup, with_photo):
    client, app, fake, _ = setup
    fake.add("Entry",location=True)
    body = {"type":"item.create","payload":{"name":"Manual draft","quantity":3}}
    if with_photo:
        job = client.post(PREFIX + "/capture-jobs",files={"image":("synthetic.jpg",photo(),"image/jpeg")},
                          headers={"Idempotency-Key":str(uuid4())})
        assert job.status_code == 202
        body["captureId"] = job.json()["captureId"]
    existing = client.post(PREFIX + "/actions",json=body).json()
    reply = turn(client, "Add a different chat cable to Entry")
    assert reply["action"]["id"] == existing["id"] and reply["action"]["payload"] == existing["payload"]
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 1
    if with_photo:
        assert app.state.store.one("SELECT action_id FROM captures WHERE id=?", (body["captureId"],))["action_id"] == existing["id"]
        # An explicit chat edit keeps the original authorized photo.
        edited = turn(client,"make that two",reply)
        assert edited["action"]["after"]["photoIncluded"] is True
        assert app.state.store.one("SELECT action_id FROM captures WHERE id=?", (body["captureId"],))["action_id"] == edited["action"]["id"]
        assert edited["action"]["payload"]["quantity"] == 2
    assert fake.calls["create_item"] == fake.calls["upload"] == 0


@pytest.mark.parametrize("status,extra,blocked", [
    ("proposed",{},True),("running",{},True),("uncertain",{},True),
    ("failed",{"retryable":True},True),("failed",{"_createdId":"known-created-id"},True),
    ("failed",{"result":{"id":"known-partial-id"}},True),
    ("failed",{"retryable":False},False),("cancelled",{},False),("succeeded",{},False)])
def test_pending_guard_classifies_partial_and_terminal_action_states(setup,status,extra,blocked):
    client, app, fake, _ = setup
    existing = client.post(PREFIX + "/actions",json={"type":"item.create","payload":{"name":"First"}}).json()
    raw = app.state.actions.load(scoped_session(app),existing["id"])[1]
    raw.update(status=status,**extra)
    app.state.store.save_action(raw)
    next_action = client.post(PREFIX + "/actions",json={"type":"item.create","payload":{"name":"Second"}})
    assert next_action.status_code == (409 if blocked else 200)
    if blocked:
        assert next_action.json()["error"]["code"] == "write_pending"
        assert next_action.json()["error"]["details"]["actionId"] == existing["id"]
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == (1 if blocked else 2)
    assert fake.calls["create_item"] == 0


@pytest.mark.parametrize("cross_flow",[False,True])
@pytest.mark.parametrize("with_photo",[False,True])
def test_concurrent_http_proposal_claims_are_atomic_across_flows(setup,monkeypatch,cross_flow,with_photo):
    _, app, fake, _ = setup
    target = fake.add("Entry",location=True)
    original = fake.tool
    async def run():
        first_entered, release = asyncio.Event(),asyncio.Event()
        count = 0
        async def pause_validation(session,name,arguments=None):
            nonlocal count
            if name in {"get_entity","get_entity_cards"}:
                count += 1
                first_entered.set()
                if count == 2:
                    release.set()
                await asyncio.wait_for(release.wait(),1)
            return await original(session,name,arguments)
        monkeypatch.setattr(fake,"tool",pause_validation)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=app.state.config.origin,
                                     headers={"Origin":app.state.config.origin}) as client:
            await login_async(client)
            body = {"type":"item.create","payload":{"name":"Manual contender","locationId":target}}
            if with_photo:
                job = await client.post(PREFIX + "/capture-jobs",files={"image":("synthetic.jpg",photo(),"image/jpeg")},
                                        headers={"Idempotency-Key":str(uuid4())})
                assert job.status_code == 202
                body["captureId"] = job.json()["captureId"]
            first = asyncio.create_task(client.post(PREFIX + "/actions",json=body))
            await asyncio.wait_for(first_entered.wait(),1)
            second = asyncio.create_task(client.post(PREFIX + ("/chat" if cross_flow else "/actions"),json=(
                {"message":"Add a chat contender to Entry","clientTurnId":str(uuid4())} if cross_flow else
                {"type":"item.create","payload":{"name":"Second manual contender","locationId":target}})))
            results = await asyncio.wait_for(asyncio.gather(first,second),2)
            assert sorted(result.status_code for result in results) == [200,409]
            conflict = next(result for result in results if result.status_code == 409)
            assert conflict.json()["error"]["code"] == "write_pending"
            winner = app.state.store.one("SELECT id,status FROM actions")
            assert winner["status"] == "proposed" and conflict.json()["error"]["details"]["actionId"] == winner["id"]
            if with_photo:
                retained = app.state.store.one("SELECT * FROM captures WHERE id=?",(body["captureId"],))
                assert retained["action_id"] == (winner["id"] if results[0].status_code == 200 else None)
                assert (app.state.config.uploads / retained["filename"]).exists()
    asyncio.run(run())
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 1
    assert fake.calls["create_item"] == fake.calls["upload"] == 0


def test_pending_destination_page_has_one_authoritative_population(setup):
    client, _, fake, _ = setup
    locations = {fake.add(f"Entry location {index}",location=True) for index in range(20)}
    containers = {fake.add(f"Entry container {index}") for index in range(20)}
    pending = turn(client,"Add a cable to Entry")
    candidates = pending["clarification"]["candidates"]
    assert len(candidates) == 20
    assert {candidate["entityId"] for candidate in candidates} == locations
    assert not pending.get("destinations") and not pending["items"]
    assert pending["pagination"]["totalRecords"] == 20 and not pending["pagination"]["hasMore"]
    assert all(candidate["path"] and candidate["kind"] == "location" for candidate in candidates)
    other_kind = turn(client,"Show item containers",pending)
    assert len(other_kind["clarification"]["candidates"]) == 20
    assert {candidate["entityId"] for candidate in other_kind["clarification"]["candidates"]} == containers
    assert not other_kind.get("destinations") and not other_kind["items"]
    assert fake.calls["create_item"] == 0


def test_direct_destination_browse_and_distinct_side_items_are_not_dropped(setup):
    client, _, fake, _ = setup
    location, item = fake.add("Entryway",location=True),fake.add("Cable")
    direct = turn(client,"Show destinations")
    assert direct["destinations"][0]["id"] == location and "clarification" not in direct
    pending = turn(client,"Add a charger to Entry",direct)
    side = turn(client,"Find Cable",pending,mode="read_only")
    assert [record["id"] for record in side["items"]] == [item]
    assert side["clarification"]["candidates"][0]["entityId"] == location


def test_item_clarification_does_not_duplicate_its_candidate_cards(setup):
    client, _, fake, _ = setup
    expected = {fake.add("Cable") for _ in range(20)}
    fake.add("Entry",location=True)
    reply = turn(client,"Move Cable to Entry")
    assert reply["items"] == []
    assert {candidate["entityId"] for candidate in reply["clarification"]["candidates"]} == expected


@pytest.mark.parametrize("message", ["Find Entry container", "Which inventory do I have?"])
@pytest.mark.parametrize("item_count", [20, 21])
def test_distinct_read_suspends_excess_choices_and_resumes_same_draft(setup, message, item_count):
    client, app, fake, _ = setup
    locations = {fake.add(f"Entry location {index}", location=True) for index in range(20)}
    items = [fake.add(f"Entry container {index}") for index in range(20)]
    read_tools = ("inventory_overview", "list_items_page", "list_destinations_page",
                  "get_entity_card", "get_entity_cards")

    def checked_turn(text, previous=None, **fields):
        before = sum(fake.calls[name] for name in read_tools)
        reply = turn(client, text, previous, **fields)
        assert sum(fake.calls[name] for name in read_tools) - before <= 4
        count = len(reply["items"]) + len(reply.get("destinations", []))
        count += len((reply.get("clarification") or {}).get("candidates", []))
        assert count <= 20
        return reply

    pending = checked_turn("Add a mobile charge to Entry")
    assert len(pending["clarification"]["candidates"]) == 20
    assert {candidate["entityId"] for candidate in pending["clarification"]["candidates"]} == locations
    if item_count > 20:
        items.append(fake.add("Entry container 20"))
    side = checked_turn(message, pending, mode="read_only")
    assert [item["id"] for item in side["items"]] == items[:20]
    assert side["pendingOperation"] == pending["pendingOperation"]
    assert "clarification" not in side and not side.get("destinations")
    assert "browse destinations" in side["message"].lower()
    if "pagination" in side:
        assert side["pagination"] == {
            "continuationId": side["pagination"]["continuationId"],
            "totalRecords": item_count, "page": 1, "pageSize": 20,
            "hasMore": item_count > 20, "complete": True}
    if "overview" in side:
        assert side["overview"]["itemRecordCount"] == item_count
    current = client.get(PREFIX + "/conversations/current").json()["conversation"]
    assert current["items"] == side["items"] and "clarification" not in current
    _, data, _ = app.state.conversations.load(scoped_session(app))
    assert data["pending"] is None
    stale = client.post(PREFIX + "/chat", json={
        "clientTurnId": str(uuid4()), "conversationId": side["conversationId"],
        "expectedRevision": side["revision"],
        "selection": {"clarificationId": pending["clarification"]["id"],
                      "candidateId": pending["clarification"]["candidates"][0]["id"]}})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "clarification_expired"
    resumed = checked_turn("Show destinations", side, mode="read_only")
    assert resumed["pendingOperation"] == pending["pendingOperation"]
    assert resumed["clarification"]["id"] != pending["clarification"]["id"]
    assert {candidate["entityId"] for candidate in resumed["clarification"]["candidates"]} == locations
    assert not ({candidate["id"] for candidate in resumed["clarification"]["candidates"]}
                & {candidate["id"] for candidate in pending["clarification"]["candidates"]})
    preview = choose(client, resumed)
    assert preview["pendingOperation"]["id"] == pending["pendingOperation"]["id"]
    assert preview["action"]["payload"]["name"] == "mobile charge"
    assert preview["action"]["payload"]["locationId"] in locations
    choices = checked_turn("Show destinations", preview, mode="read_only")
    side_after_preview = checked_turn(message, choices, mode="read_only")
    assert "clarification" not in side_after_preview
    assert side_after_preview["pendingOperation"] == preview["pendingOperation"]
    assert side_after_preview["action"] == preview["action"]
    assert app.state.store.one("SELECT status FROM actions WHERE id=?", (preview["action"]["id"],))["status"] == "proposed"
    assert fake.calls["create_item"] == 0


@pytest.mark.parametrize("recover_current", [False, True])
def test_cancelled_recovery_review_cannot_replace_exact_new_add_with_old_name(setup, monkeypatch, recover_current):
    client, app, fake, _ = setup
    ground = fake.add("Entry", location=True)
    fake.items[ground]["path"] = "Ground floor / Entry"
    upper = fake.add("Entry", location=True)
    fake.items[upper]["path"] = "Upper floor / Entry"
    destination_question = "Could you show the storage locations I can use?"
    add_question = "Please prepare a new item called paraphrase lamp for the Ground floor / Entry location."
    contexts = []

    async def correct_intent(text, context=None):
        contexts.append((text, copy.deepcopy(context)))
        if text == destination_question:
            return Intent(kind="destinations.list")
        assert text == add_question
        return Intent(kind="item.create", query="paraphrase lamp", destination="Ground floor / Entry")

    monkeypatch.setattr(app.state.vision, "intent", correct_intent)
    assert app.state.conversations.local_intent(add_question, {}) is None
    initial = turn(client, "Add a recovery test item to the entry.")
    candidate = next(value for value in initial["clarification"]["candidates"] if value["entityId"] == ground)
    body = {"clientTurnId": str(uuid4()), "conversationId": initial["conversationId"],
            "expectedRevision": initial["revision"],
            "selection": {"clarificationId": initial["clarification"]["id"], "candidateId": candidate["id"]}}
    preview = client.post(PREFIX + "/chat", json=body).json()
    assert client.post(PREFIX + "/chat", json=body).json() == preview
    assert client.get(PREFIX + "/conversations/current").json()["conversation"]["action"] == preview["action"]
    cancelled = client.post(PREFIX + f"/actions/{preview['action']['id']}/cancel", json={})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    if recover_current:
        client.get(PREFIX + "/conversations/current")
    destinations = turn(client, destination_question, preview)
    added = turn(client, add_question, destinations)
    if (added.get("clarification") or {}).get("slot") == "destination":
        exact = next(value for value in added["clarification"]["candidates"] if value["entityId"] == ground)
        added = choose(client, added, exact)
    if (added.get("clarification") or {}).get("slot") == "review":
        added = turn(client, "review draft", added)
    assert added["action"]["payload"]["name"] == "paraphrase lamp"
    assert added["action"]["payload"]["locationId"] == ground
    assert added["action"]["id"] != preview["action"]["id"]
    assert added["pendingOperation"]["id"] != preview["pendingOperation"]["id"]
    assert "pendingOperation" not in destinations
    assert contexts == [(destination_question, {"pending": None, "awaiting": None, "lastItems": []}),
                        (add_question, {"pending": None, "awaiting": None, "lastItems": []})]
    assert fake.calls["create_item"] == 0


@pytest.mark.parametrize("with_photo", [False, True])
def test_external_cancel_retires_chat_task_and_choices_but_retains_photo(setup, with_photo):
    client, app, fake, _ = setup
    fake.add("Entry", location=True)
    fake.add("Garage", location=True)
    body = {"type": "item.create", "payload": {"name": "Cancelled manual draft", "quantity": 3}}
    capture = None
    if with_photo:
        job = client.post(PREFIX + "/capture-jobs", files={"image": ("synthetic.jpg", photo(), "image/jpeg")},
                          headers={"Idempotency-Key": str(uuid4())})
        assert job.status_code == 202
        body["captureId"] = job.json()["captureId"]
        capture = app.state.store.one("SELECT * FROM captures WHERE id=?", (body["captureId"],))
    existing = client.post(PREFIX + "/actions", json=body).json()
    adopted = client.get(PREFIX + "/conversations/current").json()["conversation"]
    assert adopted["action"]["id"] == existing["id"]
    choices = turn(client, "Show destinations", adopted, mode="read_only")
    assert choices["clarification"]["candidates"]
    cancelled = client.post(PREFIX + f"/actions/{existing['id']}/cancel", json={})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    current = client.get(PREFIX + "/conversations/current").json()["conversation"]
    assert "action" not in current and "pendingOperation" not in current and "clarification" not in current
    stale = client.post(PREFIX + "/chat", json={
        "clientTurnId": str(uuid4()), "conversationId": current["conversationId"],
        "expectedRevision": current["revision"],
        "selection": {"clarificationId": choices["clarification"]["id"],
                      "candidateId": choices["clarification"]["candidates"][0]["id"]}})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "clarification_expired"
    edit = turn(client, "make that two", current)
    assert "action" not in edit and "pendingOperation" not in edit
    assert app.state.store.one("SELECT count(*) AS n FROM actions")["n"] == 1
    assert client.get(PREFIX + f"/actions/{existing['id']}").json()["status"] == "cancelled"
    if capture:
        retained = app.state.store.one("SELECT * FROM captures WHERE id=?", (capture["id"],))
        assert retained["action_id"] is None and retained["filename"] == capture["filename"]
        assert (app.state.config.uploads / retained["filename"]).exists()
    fresh = client.post(PREFIX + "/actions", json={**body, "payload": {"name": "New manual draft", "quantity": 1}})
    assert fresh.status_code == 200 and fresh.json()["id"] != existing["id"]
    assert fresh.json()["after"]["photoIncluded"] is with_photo
    assert fake.calls["create_item"] == fake.calls["upload"] == 0
