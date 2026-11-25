import asyncio
import copy
import io
import json
import time
from collections import Counter
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from PIL import Image

from process_helpers import (
    hold_collection_lock, join_clean_processes, process_state, start_ready_processes, stop_processes,
    wait_for_process_signal,
)

from assistant.clients import ServiceError
from assistant.config import Config
from assistant.images import VisionClient, normalize
from assistant.main import PREFIX, create_app
from assistant.schemas import ActionRequest
from assistant.state import State

G1, G2, U1, U2 = [str(uuid4()) for _ in range(4)]


class FakeClients:
    def __init__(self):
        self.items = {}
        self.calls = Counter()
        self.memberships = {"Bearer alice": [G1, G2], "Bearer bob": [G2]}
        self.upload_failures = 0
        self.ambiguous_create = False
        self.create_delay = 0

    async def homebox(self, method, path, session=None, **kwargs):
        if path == "/users/login":
            username = kwargs["json"]["username"]
            if username not in {"alice", "bob"}:
                raise ServiceError("homebox_access_denied", "Invalid credentials", 401)
            return {"token": "Bearer " + username}
        token = session["credential"]
        if path == "/users/self":
            groups = self.memberships[token]
            return {"item": {"id": U1 if token.endswith("alice") else U2, "name": token[7:],
                             "email": "test@example.invalid", "groupIds": groups, "defaultGroupId": groups[0]}}
        if path == "/groups/all":
            return [{"id": gid, "name": gid} for gid in self.memberships[token]]
        return {}

    def add(self, name="Cable", location=False, group=G1):
        eid = str(uuid4())
        self.items[eid] = {
            "id": eid, "name": name, "isLocation": location, "locationPath": "",
            "locationId": None, "quantity": 1, "updatedAt": "v1", "tags": [],
            "_group": group,
        }
        return eid

    async def tool(self, session, name, arguments=None):
        self.calls[name] += 1
        args = arguments or {}
        if session["collection"] not in self.memberships[session["credential"]]:
            raise ServiceError("collection_denied", "Access denied", 403)
        def get(eid):
            if eid not in self.items or self.items[eid]["_group"] != session["collection"]:
                raise ServiceError("not_found", "Not found", 404)
            return {k: copy.deepcopy(v) for k, v in self.items[eid].items() if k != "_group"}
        def bounded(eid):
            value, ancestors, cursor = get(eid), [], self.items[eid].get("parentId") or self.items[eid].get("locationId")
            while cursor and cursor not in ancestors:
                ancestors.append(cursor)
                cursor = self.items[cursor].get("parentId") or self.items[cursor].get("locationId")
            return {**value, "ancestorIds": list(reversed(ancestors))}
        if name == "get_entity_cards":
            return {"items": [bounded(eid) for eid in args["entity_ids"]]}
        if name in {"get_entity", "get_entity_card"}:
            return bounded(args["entity_id"]) if name == "get_entity_card" else get(args["entity_id"])
        if name in {"inventory_overview", "list_items_page", "list_destinations_page"}:
            query = args.get("query", "").casefold()
            values = [get(eid) for eid, value in self.items.items()
                      if value["_group"] == session["collection"] and not value.get("archived", False)]
            if name == "inventory_overview":
                items = [v for v in values if not v["isLocation"]]
                return {"items": items[:args.get("preview_limit", 10)], "itemRecordCount": len(items),
                        "locationCount": sum(v["isLocation"] for v in values), "includeArchived": False, "complete": True}
            if name == "list_items_page":
                values = [v for v in values if not v["isLocation"] and query in v["name"].casefold()
                          and (not args.get("location_id") or v.get("locationId") == args["location_id"])]
            else:
                values = [{"id": v["id"], "name": v["name"], "path": v.get("path") or " / ".join(
                           filter(None, [v.get("locationPath"), v["name"]])),
                           "kind": "location" if v["isLocation"] else "item"}
                          for v in values if v["isLocation"] == (args["kind"] == "locations")
                          and query in v["name"].casefold()]
            page, size = args.get("page", 1), args.get("page_size", 20)
            return {"items" if name == "list_items_page" else "destinations": values[(page - 1) * size:page * size],
                    "total": len(values), "page": page, "pageSize": size, "hasMore": page * size < len(values), "complete": True}
        if name == "list_locations":
            return {"locations": [{**get(eid), "path": value["name"]} for eid, value in self.items.items()
                                   if value["_group"] == session["collection"] and value["isLocation"]]}
        if name == "search_items":
            query = args.get("query", "").casefold()
            values = [get(eid) for eid, value in self.items.items()
                      if value["_group"] == session["collection"] and not value["isLocation"]
                      and query in value["name"].casefold()]
            return {"items": values, "total": len(values)}
        if name == "location_contents":
            values = [get(eid) for eid, value in self.items.items()
                      if value["_group"] == session["collection"] and not value["isLocation"]
                      and value.get("locationId") == args["location_id"]]
            return {"items": values, "total": len(values)}
        if name == "create_item":
            if self.create_delay:
                await asyncio.sleep(self.create_delay)
            eid = self.add(args["payload"]["name"], group=session["collection"])
            self.items[eid].update(args["payload"])
            if self.ambiguous_create:
                raise ServiceError("mcp_unavailable", "Request outcome unknown")
            return get(eid)
        if name == "update_item":
            self.items[args["entity_id"]].update(args["changes"])
            return get(args["entity_id"])
        if name == "set_tags":
            self.items[args["entity_id"]]["tags"] = args["tags"]
            return get(args["entity_id"])
        if name == "move_item":
            self.items[args["entity_id"]]["locationId"] = args["destination_id"]
            return get(args["entity_id"])
        if name == "create_location":
            return get(self.add(args["name"], location=True, group=session["collection"]))
        raise AssertionError(name)

    async def upload(self, *args):
        self.calls["upload"] += 1
        if self.upload_failures:
            self.upload_failures -= 1
            raise ServiceError("homebox_unavailable", "Photo upload unavailable")


@pytest.fixture
def setup(tmp_path):
    cfg = Config.load()
    cfg.state, cfg.uploads = tmp_path / "state.db", tmp_path / "uploads"
    fake = FakeClients()
    app = create_app(cfg, fake)
    with TestClient(app, base_url="https://inventory.test", headers={"Origin": cfg.origin}) as client:
        response = client.post(PREFIX + "/auth/login", json={"username": "alice", "password": "password"})
        assert response.status_code == 200
        client.headers["X-CSRF-Token"] = response.json()["csrfToken"]
        client.headers["X-Collection-ID"] = response.json()["collectionId"]
        yield client, app, fake, response


def propose(client, kind, payload, **extra):
    response = client.post(PREFIX + "/actions", json={"type": kind, "payload": payload, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def confirm(client, action):
    return client.post(PREFIX + f"/actions/{action['id']}/confirm",
                       json={"revision": 1}, headers={"Idempotency-Key": action["id"]})


def test_session_security_and_csrf(setup):
    client, app, fake, response = setup
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert "Bearer" not in response.text
    stored = app.state.store.one("SELECT token FROM sessions")
    assert "alice" not in stored["token"]
    client.headers.pop("X-CSRF-Token")
    assert client.post(PREFIX + "/actions", json={"type": "item.create", "payload": {"name": "X"}}).status_code == 403
    assert client.post(PREFIX + "/auth/login", json={"username": "alice", "password": "x"},
                       headers={"Origin": "https://attacker.test"}).status_code == 403


def test_create_requires_confirmation_and_replays_once(setup):
    client, app, fake, _ = setup
    action = propose(client, "item.create", {"name": "Stove", "notes": "Reviewed", "tags": ["camping"]})
    assert fake.calls["create_item"] == 0
    result = confirm(client, action)
    assert result.json()["status"] == "succeeded", result.text
    assert confirm(client, action).json() == result.json()
    assert fake.calls["create_item"] == 1
    assert fake.calls["set_tags"] == 1
    assert len(app.state.store.all("SELECT * FROM audit")) >= 3


def test_overlapping_confirms_only_create_once(setup):
    client, app, fake, _ = setup
    action = propose(client, "item.create", {"name": "Stove"})
    row = app.state.store.one("SELECT * FROM sessions")
    row["credential"] = "Bearer alice"
    fake.create_delay = .03
    async def run():
        return await asyncio.gather(*[
            app.state.actions.confirm(row, action["id"], 1, action["id"]) for _ in range(4)
        ])
    results = asyncio.run(run())
    assert fake.calls["create_item"] == 1
    assert any(value["status"] == "succeeded" for value in results)


def test_stale_action_does_not_move(setup):
    client, app, fake, _ = setup
    item, dest = fake.add(), fake.add("Box", location=True)
    action = propose(client, "item.move", {"entityId": item, "locationId": dest})
    fake.items[item]["updatedAt"] = "v2"
    assert confirm(client, action).status_code == 409
    assert fake.calls["move_item"] == 0
    assert client.get(PREFIX + f"/actions/{action['id']}").json()["status"] == "cancelled"


def test_collection_switch_cannot_execute_previous_action(setup):
    client, app, fake, _ = setup
    action = propose(client, "item.create", {"name": "Private"})
    assert client.post(PREFIX + "/session/collection", json={"collectionId": G2}).status_code == 200
    client.headers["X-Collection-ID"] = G2
    assert confirm(client, action).status_code == 404
    assert fake.calls["create_item"] == 0
    assert client.post(PREFIX + "/session/collection", json={"collectionId": str(uuid4())}).status_code == 403


def test_ambiguous_item_requires_choice_and_exact_ids_work(setup):
    client, app, fake, _ = setup
    one, two = fake.add("HDMI cable"), fake.add("HDMI cable")
    dest = fake.add("Electronics box", location=True)
    response = client.post(PREFIX + "/chat", json={"message": "I moved the HDMI cable into the electronics box."})
    assert response.json()["clarification"]["slot"] == "item"
    assert len(response.json()["clarification"]["candidates"]) == 2
    assert fake.calls["move_item"] == 0
    action = propose(client, "item.move", {"entityId": two, "locationId": dest})
    assert confirm(client, action).json()["status"] == "succeeded"
    assert fake.items[two]["locationId"] == dest
    assert fake.items[one]["locationId"] is None


def test_photo_failure_resumes_without_recreating(setup):
    client, app, fake, _ = setup
    row = app.state.store.one("SELECT * FROM sessions")
    capture = "c_test"
    (app.state.config.uploads / "photo.jpg").write_bytes(b"image-fixture")
    app.state.store.execute("INSERT INTO captures VALUES(?,?,?,?,?,NULL)",
                            (capture, row["id"], G1, "photo.jpg", time.time() + 3600))
    action = propose(client, "item.create", {"name": "Stove"}, captureId=capture)
    fake.upload_failures = 1
    failed = confirm(client, action).json()
    assert failed["status"] == "failed" and failed["retryable"] is True
    eid = failed["result"]["id"]
    resumed = confirm(client, action).json()
    assert resumed["status"] == "succeeded" and resumed["result"]["id"] == eid
    assert fake.calls["create_item"] == 1
    assert fake.calls["upload"] == 2
    assert not (app.state.config.uploads / "photo.jpg").exists()


def test_ambiguous_create_is_not_retryable(setup):
    client, app, fake, _ = setup
    fake.ambiguous_create = True
    action = propose(client, "item.create", {"name": "Stove"})
    result = confirm(client, action).json()
    assert result["status"] == "uncertain" and result["retryable"] is False
    assert confirm(client, action).json()["status"] == "uncertain"
    assert fake.calls["create_item"] == 1


def test_nfc_reassign_and_revoke_preserves_url(setup):
    client, app, fake, _ = setup
    one, two = fake.add("Garage", location=True), fake.add("Kitchen", location=True)
    mapping = client.post(PREFIX + "/tags/mappings", json={"targetType": "location", "entityId": one}).json()
    tag = mapping["tagId"]
    result = client.patch(PREFIX + f"/tags/mappings/{tag}", json={"entityId": two}).json()
    assert result["url"] == mapping["url"]
    assert client.get(PREFIX + f"/tags/resolve/{tag}").json()["entity"]["id"] == two
    assert client.patch(PREFIX + f"/tags/mappings/{tag}", json={"status": "revoked"}).status_code == 200
    assert client.get(PREFIX + f"/tags/resolve/{tag}").status_code == 404


def test_malformed_images_and_pixel_limits(setup):
    _, app, _, _ = setup
    cfg = app.state.config
    with pytest.raises(ServiceError, match="Use a single"):
        normalize(b"not-an-image", cfg)
    image = Image.new("RGB", (20, 20))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    normalized = normalize(buffer.getvalue(), cfg)
    assert Image.open(io.BytesIO(normalized)).format == "JPEG"
    cfg.max_pixels = 100
    with pytest.raises(ServiceError):
        normalize(buffer.getvalue(), cfg)


@respx.mock
def test_invalid_model_json_retries_only_once(setup):
    _, app, _, _ = setup
    route = respx.post(app.state.config.ollama + "/api/chat").mock(
        return_value=httpx.Response(200, json={"message": {"content": "invalid"}}))
    with pytest.raises(ServiceError) as exc:
        asyncio.run(VisionClient(app.state.config).analyze(b"image", ""))
    assert exc.value.code == "invalid_model_output"
    assert route.call_count == 2


def test_rejects_insecure_non_loopback_configuration(monkeypatch):
    monkeypatch.setenv("ASSISTANT_PUBLIC_ORIGIN", "http://inventory.example")
    monkeypatch.setenv("ASSISTANT_ALLOW_INSECURE_HTTP", "true")
    with pytest.raises(RuntimeError, match="HTTPS"):
        Config.load()


def test_full_create_review_includes_metadata_destination_and_photo(setup):
    client, app, fake, _ = setup
    row = app.state.store.one("SELECT * FROM sessions")
    dest = fake.add("Garage", location=True)
    (app.state.config.uploads / "review.jpg").write_bytes(b"fixture")
    app.state.store.execute("INSERT INTO captures VALUES(?,?,?,?,?,NULL)",
                            ("c_review", row["id"], G1, "review.jpg", time.time() + 3600))
    payload = {"name": "Stove", "quantity": 3, "notes": "Blue case", "tags": ["camping"],
               "manufacturer": "Example", "model": "M1", "serialNumber": "S1", "locationId": dest}
    action = propose(client, "item.create", payload, captureId="c_review")
    assert all(action["after"][k] == v for k, v in payload.items())
    assert action["after"]["destination"]["id"] == dest
    assert action["after"]["photoIncluded"] is True
    assert fake.calls["create_item"] == 0


def test_state_is_bound_to_explicit_homebox_instance(setup):
    _, app, _, _ = setup
    State(app.state.config.state, app.state.config.instance_id)
    with pytest.raises(RuntimeError, match="different Homebox instance"):
        State(app.state.config.state, str(uuid4()))


def test_stale_browser_collection_context_fails_closed(setup):
    client, app, fake, _ = setup
    changed = client.post(PREFIX + "/session/collection", json={"collectionId": G2})
    assert changed.status_code == 200
    stale = client.post(PREFIX + "/actions", json={"type": "item.create", "payload": {"name": "Wrong collection"}})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "COLLECTION_CONTEXT_CHANGED"
    assert client.get(PREFIX + "/items").status_code == 409
    assert client.get(PREFIX + "/session").json()["collectionId"] == G2
    client.headers.pop("X-Collection-ID")
    assert client.get(PREFIX + "/items").status_code == 409
    assert fake.calls["create_item"] == 0


def test_cancel_edit_repropose_same_owned_photo(setup):
    client, app, fake, _ = setup
    row = app.state.store.one("SELECT * FROM sessions")
    (app.state.config.uploads / "edit.jpg").write_bytes(b"fixture")
    app.state.store.execute("INSERT INTO captures VALUES(?,?,?,?,?,NULL)",
                            ("c_edit", row["id"], G1, "edit.jpg", time.time() + 3600))
    first = propose(client, "item.create", {"name": "Old draft"}, captureId="c_edit")
    assert client.post(PREFIX + f"/actions/{first['id']}/cancel", json={}).status_code == 200
    second = propose(client, "item.create", {"name": "Edited draft"}, captureId="c_edit")
    assert second["id"] != first["id"]
    assert app.state.store.one("SELECT action_id FROM captures WHERE id='c_edit'")["action_id"] == second["id"]
    assert fake.calls["create_item"] == 0


def test_recovery_includes_lost_success_and_original_confirmation_key(setup):
    client, app, fake, _ = setup
    action = propose(client, "item.create", {"name": "Stove"})
    result = client.post(PREFIX + f"/actions/{action['id']}/confirm", json={"revision": 1},
                         headers={"Idempotency-Key": action["confirmationKey"]}).json()
    recovered = client.get(PREFIX + "/actions").json()["actions"]
    assert recovered[0]["id"] == action["id"]
    assert recovered[0]["status"] == "succeeded"
    assert recovered[0]["confirmationKey"] == action["confirmationKey"]
    assert recovered[0]["result"]["id"] == result["result"]["id"]
    assert fake.calls["create_item"] == 1


def test_distinct_overlapping_actions_recheck_state_inside_collection_lock(setup):
    client, app, fake, _ = setup
    item = fake.add()
    first = propose(client, "item.update", {"entityId": item, "quantity": 2})
    first_session = app.state.store.one("SELECT * FROM sessions")
    first_session["credential"] = "Bearer alice"
    # Independent browser sessions may each review the same old record state.
    login = client.post(PREFIX + "/auth/login", json={"username":"alice","password":"synthetic"}).json()
    client.headers.update({"X-CSRF-Token":login["csrfToken"],"X-Collection-ID":login["collectionId"]})
    second = propose(client, "item.update", {"entityId": item, "quantity": 3})
    row = app.state.store.one("SELECT * FROM sessions ORDER BY rowid DESC LIMIT 1")
    row["credential"] = "Bearer alice"
    async def run():
        return await asyncio.gather(*[
            app.state.actions.confirm(session, action["id"], 1, action["confirmationKey"])
            for session, action in ((first_session,first),(row,second))
        ], return_exceptions=True)
    results = asyncio.run(run())
    assert fake.calls["update_item"] == 1
    assert sum(isinstance(result, ServiceError) and result.code == "stale_action" for result in results) == 1


def test_collection_switch_recovers_after_old_membership_removed(setup):
    client, app, fake, _ = setup
    fake.memberships["Bearer alice"] = [G2]
    assert client.get(PREFIX + "/items").status_code == 403
    assert client.get(PREFIX + "/session").status_code == 200
    switched = client.post(PREFIX + "/session/collection", json={"collectionId": G2})
    assert switched.status_code == 200
    client.headers["X-Collection-ID"] = G2
    assert client.get(PREFIX + "/items").status_code == 200


@pytest.mark.parametrize("status", [401, 503])
def test_logout_is_local_even_when_upstream_auth_unavailable(setup, status):
    client, app, fake, _ = setup
    async def unavailable(*args, **kwargs):
        raise ServiceError("unavailable", "Unavailable", status)
    fake.homebox = unavailable
    result = client.post(PREFIX + "/auth/logout", json={})
    assert result.status_code == 200
    assert "Max-Age=0" in result.headers["set-cookie"]
    assert app.state.store.one("SELECT 1 FROM sessions") is None


def test_startup_rechecks_success_committed_before_nonblocking_lock(setup, monkeypatch):
    import assistant.state as state_module
    client, app, fake, _ = setup
    action = propose(client, "item.create", {"name": "Stove"})
    row = app.state.store.one("SELECT data FROM actions WHERE id=?", (action["id"],))
    running = json.loads(row["data"])
    running["status"] = "running"
    app.state.store.save_action(running)
    original = state_module.fcntl.flock
    committed = False
    def interleaved(fd, operation):
        nonlocal committed
        if operation & state_module.fcntl.LOCK_NB and not committed:
            committed = True
            latest = dict(running, status="succeeded", result={"id": "latest-record"})
            app.state.store.save_action(latest)
        return original(fd, operation)
    monkeypatch.setattr(state_module.fcntl, "flock", interleaved)
    State(app.state.config.state, app.state.config.instance_id)
    final = json.loads(app.state.store.one("SELECT data FROM actions WHERE id=?", (action["id"],))["data"])
    assert committed
    assert final["status"] == "succeeded"
    assert final["result"]["id"] == "latest-record"


def test_collection_lock_serializes_separate_processes(setup):
    import multiprocessing
    _, app, _, _ = setup
    context = multiprocessing.get_context("spawn")
    ready1, ready2, start1, start2, first_in, second_in, release1, release2, contended = [
        context.Semaphore(0) for _ in range(9)
    ]
    args = (str(app.state.config.state), app.state.config.instance_id, G1)
    first = context.Process(target=hold_collection_lock, args=(*args, ready1, start1, first_in, release1))
    second = context.Process(target=hold_collection_lock,
                             args=(*args, ready2, start2, second_in, release2, contended))
    try:
        start_ready_processes(((first, ready1), (second, ready2)))
        start1.release()
        wait_for_process_signal(first, first_in, 3, "first collection lock acquisition")
        start2.release()
        wait_for_process_signal(second, contended, 3, "second collection lock contention")
        assert first.is_alive(), process_state(first)
        assert not second_in.acquire(block=False), "Second process entered while the first still held the lock"
        release1.release()
        wait_for_process_signal(second, second_in, 3, "second collection lock acquisition after release")
        release2.release()
        join_clean_processes((first, second))
    finally:
        release1.release()
        release2.release()
        stop_processes((first, second))


def test_process_bootstrap_failure_reports_exit_code(tmp_path):
    import multiprocessing
    context = multiprocessing.get_context("spawn")
    invalid_parent = tmp_path / "not-a-directory"
    invalid_parent.write_text("synthetic bootstrap failure")
    ready, start, acquired, release = [context.Semaphore(0) for _ in range(4)]
    process = context.Process(target=hold_collection_lock,
                              args=(str(invalid_parent / "state.db"), "test-instance", G1,
                                    ready, start, acquired, release))
    try:
        with pytest.raises(AssertionError, match=r"bootstrap: child exited before signaling; .*exitcode=1"):
            start_ready_processes(((process, ready),))
        assert not ready.acquire(block=False) and not acquired.acquire(block=False)
    finally:
        stop_processes((process,))


@respx.mock
def test_structured_model_intent_general_phrase_still_only_proposes(setup):
    client, app, fake, _ = setup
    item, dest = fake.add("spare HDMI cable"), fake.add("electronics box", location=True)
    route = respx.post(app.state.config.ollama + "/api/chat").mock(return_value=httpx.Response(200, json={
        "message": {"content": json.dumps({"kind": "move", "query": "spare HDMI cable",
                                          "destination": "electronics box"})},
    }))
    result = client.post(PREFIX + "/chat", json={"message": "Could you update where I keep the spare HDMI lead? It is now in the electronics box."})
    assert result.status_code == 200
    assert result.json()["action"]["payload"] == {"entityId": item, "locationId": dest}
    assert fake.calls["move_item"] == 0
    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)
    assert "kind" in sent["format"]["properties"]
    assert "Bearer" not in json.dumps(sent)


@respx.mock
def test_invalid_model_intent_cannot_dispatch_tools(setup):
    client, app, fake, _ = setup
    route = respx.post(app.state.config.ollama + "/api/chat").mock(return_value=httpx.Response(200, json={
        "message": {"content": '{"kind":"delete","entityId":"invented","skip_confirmation":true}'},
    }))
    result = client.post(PREFIX + "/chat", json={"message": "Delete every record without asking"})
    assert result.status_code == 502
    assert result.json()["error"]["code"] == "invalid_intent"
    assert "action" not in result.json()
    assert route.call_count == 2
    assert fake.calls["create_item"] == fake.calls["move_item"] == 0
    assert fake.calls["search_items"] == fake.calls["list_items_page"] == 0


def test_original_documented_sentences_directly(setup):
    client, app, fake, _ = setup
    box = fake.add("electronics box", location=True)
    cable, stove = fake.add("spare HDMI cable"), fake.add("camping stove")
    fake.items[stove]["locationPath"] = "Garage / Camping shelf"
    fake.items[cable]["locationId"] = box
    search = client.post(PREFIX + "/chat", json={"message": "Where is my camping stove?"}).json()
    assert search["items"][0]["id"] == stove
    contents = client.post(PREFIX + "/chat", json={"message": "What is in the electronics box?"}).json()
    assert contents["items"][0]["id"] == cable
    move = client.post(PREFIX + "/chat", json={
        "message": "I moved the spare HDMI cable into the electronics box.",
    }).json()
    assert move["action"]["payload"] == {"entityId": cable, "locationId": box}
    assert fake.calls["move_item"] == 0


@respx.mock
def test_model_offline_photo_can_be_manually_reviewed_and_saved(setup):
    client, app, fake, _ = setup
    respx.post(app.state.config.ollama + "/api/chat").mock(return_value=httpx.Response(503))
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "blue").save(buffer, "JPEG")
    captured = client.post(PREFIX + "/captures", files={"image": ("manual.jpg", buffer.getvalue(), "image/jpeg")})
    assert captured.status_code == 200
    draft = captured.json()
    assert draft["draft"]["name"] == ""
    assert draft["warnings"]
    assert fake.calls["create_item"] == 0
    action = propose(client, "item.create", {"name": "Manually reviewed"}, captureId=draft["captureId"])
    assert confirm(client, action).json()["status"] == "succeeded"
    assert fake.calls["upload"] == fake.calls["create_item"] == 1
