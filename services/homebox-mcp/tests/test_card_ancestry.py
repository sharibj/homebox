import asyncio
import time
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
import respx

from test_bounded_reads import BASE, Inventory, a, r, record, run

BATCH_UUID = "abcdefab-cdef-4abc-8def-abcdefabcdef"
BATCH_UUID_FORMS = [
    BATCH_UUID, BATCH_UUID.upper(), UUID(BATCH_UUID).hex,
    "{" + BATCH_UUID + "}", UUID(BATCH_UUID).urn,
]


def chain(length=9):
    entities, path = [], []
    for i in range(1, length + 1):
        parent = {"parent": {"id": entities[-1]["id"]}} if entities else {}
        entity = record(i, **parent)
        entities.append(entity)
        path.append({"id": entity["id"], "name": entity["name"]})
    return entities, path


@respx.mock
def test_deep_ancestor_ids_use_existing_path_root_to_parent_excluding_self():
    entities, path = chain()
    inventory = Inventory(entities, {entities[-1]["id"]: path})
    output = run(a.get_entity_card, entity_id=entities[-1]["id"])
    assert output["ancestorIds"] == [e["id"] for e in entities[:-1]]
    assert output["id"] not in output["ancestorIds"]
    assert output["parentId"] == output["ancestorIds"][-1]
    assert len(inventory.calls) == 2 and not inventory.listings
    assert "children" not in output


@respx.mock
def test_root_has_known_empty_ancestors_from_valid_self_path_not_a_fallback():
    root = record(1)
    inventory = Inventory([root])
    output = run(a.get_entity_card, entity_id=root["id"])
    assert output["ancestorIds"] == [] and output["parentId"] is None
    assert len(inventory.calls) == 2


@respx.mock
@pytest.mark.parametrize("case", ["root", "nested", "physical_override"])
@pytest.mark.parametrize("batch", [False, True])
def test_exact_card_logical_display_path_is_grounded_without_changing_physical_path(case, batch):
    entities, logical_path = chain(1 if case == "root" else 4)
    entity = entities[-1]
    expected_location = " / ".join(p["name"] for p in logical_path[:-1])
    if case == "physical_override":
        physical = record(100, location=True, name="Physical room")
        entity["locationId"] = physical["id"]
        entities.append(physical)
        expected_location = "Physical room"
    inventory = Inventory(entities, {entity["id"]: logical_path})
    result = (run(a.get_entity_cards, entity_ids=[entity["id"]])["items"][0] if batch
              else run(a.get_entity_card, entity_id=entity["id"]))
    assert result["path"] == " / ".join(p["name"] for p in logical_path)
    assert result["path"].split(" / ")[-1] == entity["name"]
    assert result["locationPath"] == expected_location
    assert len(inventory.calls) == (3 if case == "physical_override" else 2)
    assert not inventory.listings


@respx.mock
def test_logical_ancestors_are_not_replaced_by_physical_location_override():
    entities, path = chain(3)
    physical = record(100, location=True, name="Other room")
    entities[-1]["locationId"] = physical["id"]
    inventory = Inventory(entities + [physical], {entities[-1]["id"]: path})
    output = run(a.get_entity_card, entity_id=entities[-1]["id"])
    assert output["ancestorIds"] == [e["id"] for e in entities[:-1]]
    assert physical["id"] not in output["ancestorIds"]
    assert output["locationPath"] == "Other room" and len(inventory.calls) == 3


@respx.mock
@pytest.mark.parametrize("invalid", [
    None, [], [{"id": "not-a-uuid", "name": "Bad"}],
    [{"id": str(UUID(int=3)), "name": "Self only"}],
    [{"id": str(UUID(int=1)), "name": "Wrong parent"}, {"id": str(UUID(int=3)), "name": "Self"}],
    [{"id": str(UUID(int=2)), "name": "Parent"}, {"id": str(UUID(int=2)), "name": "Cycle"},
     {"id": str(UUID(int=3)), "name": "Self"}],
    [{"id": str(UUID(int=3)), "name": "Self"}, {"id": str(UUID(int=2)), "name": "Backwards"}],
])
def test_unusable_ancestry_never_becomes_empty_ancestor_ids(invalid):
    entities, _ = chain(3)
    Inventory(entities, {entities[-1]["id"]: invalid})
    with pytest.raises(ValueError):
        run(a.get_entity_card, entity_id=entities[-1]["id"])


@respx.mock
def test_path_with_ancestors_but_missing_parent_metadata_is_not_treated_as_root():
    entities, path = chain(3)
    entities[-1].pop("parent")
    Inventory(entities, {entities[-1]["id"]: path})
    with pytest.raises(ValueError, match="parent/path"):
        run(a.get_entity_card, entity_id=entities[-1]["id"])


@respx.mock
def test_batch_returns_same_ordered_cards_under_one_client_and_fixed_six_get_limit(monkeypatch):
    source, destination = record(1, locationId=str(UUID(int=100))), record(2, locationId=str(UUID(int=101)))
    inventory = Inventory([source, destination, record(100, location=True), record(101, location=True)])
    clients = []
    original = httpx.AsyncClient
    def tracked_client(**kwargs):
        client = original(**kwargs)
        clients.append(client)
        return client
    monkeypatch.setattr(r.httpx, "AsyncClient", tracked_client)
    monkeypatch.setattr(a, "all_entities", lambda **_: pytest.fail("Recursive children scan"))
    output = run(a.get_entity_cards, entity_ids=[destination["id"], source["id"]])
    assert [c["id"] for c in output["items"]] == [destination["id"], source["id"]]
    assert all(c["ancestorIds"] == [] and "children" not in c for c in output["items"])
    assert all(c["path"] == c["name"] for c in output["items"])
    assert len(clients) == 1 and clients[0].is_closed
    assert len(inventory.calls) == 6 and not inventory.listings
    assert all(c.method == "GET" for c in inventory.calls)


@respx.mock
def test_two_card_batch_contains_deep_ancestry_without_parent_walks():
    entities, path = chain(15)
    inventory = Inventory(entities, {entities[-1]["id"]: path})
    output = run(a.get_entity_cards, entity_ids=[entities[0]["id"], entities[-1]["id"]])
    assert output["items"][0]["ancestorIds"] == []
    assert output["items"][1]["ancestorIds"] == [e["id"] for e in entities[:-1]]
    assert output["items"][0]["path"] == entities[0]["name"]
    assert output["items"][1]["path"] == " / ".join(p["name"] for p in path)
    assert len(inventory.calls) == 4


@respx.mock
@pytest.mark.parametrize("ids", [
    [], [str(UUID(int=1))] * 3, None, "not-a-list", [str(UUID(int=1)), "invalid-second-id"],
    [str(UUID(int=1)), None], [True],
])
def test_batch_validates_entire_max_two_uuid_input_before_http(ids):
    inventory = Inventory([record(1)])
    with pytest.raises((ValueError, TypeError)):
        run(a.get_entity_cards, entity_ids=ids)
    assert not inventory.calls


@respx.mock
@pytest.mark.parametrize("duplicate_id", BATCH_UUID_FORMS)
def test_batch_rejects_normalized_duplicate_uuids_before_http(duplicate_id):
    inventory = Inventory([record(1, id=BATCH_UUID)])
    with pytest.raises(ValueError, match="distinct"):
        run(a.get_entity_cards, entity_ids=[BATCH_UUID, duplicate_id])
    assert not inventory.calls


@respx.mock
@pytest.mark.parametrize("entity_id", BATCH_UUID_FORMS)
def test_batch_still_accepts_one_uuid_in_supported_normalized_forms(entity_id):
    inventory = Inventory([record(1, id=BATCH_UUID)])
    result = run(a.get_entity_cards, entity_ids=[entity_id])
    assert [item["id"] for item in result["items"]] == [BATCH_UUID]
    assert result["items"][0]["path"] == "Record 0001"
    assert len(inventory.calls) == 2


@respx.mock
@pytest.mark.parametrize("duplicate_id", BATCH_UUID_FORMS)
def test_raw_mcp_batch_rejects_normalized_duplicate_uuids_before_http(duplicate_id, mcp_client):
    inventory = Inventory([record(1, id=BATCH_UUID)])
    response = mcp_client.post("/mcp", headers={
        "Authorization": f"Bearer {a.SECRET}", "X-Homebox-Authorization": "Bearer alice",
        "X-Tenant": str(uuid4()), "Accept": "application/json, text/event-stream",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "get_entity_cards", "arguments": {
                 "entity_ids": [BATCH_UUID, duplicate_id],
             }}})
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert not inventory.calls


@respx.mock
def test_second_card_404_fails_entire_batch_after_valid_first_card():
    first, missing = record(1), record(2)
    inventory = Inventory([first])
    with pytest.raises(httpx.HTTPStatusError) as exc:
        run(a.get_entity_cards, entity_ids=[first["id"], missing["id"]])
    assert exc.value.response.status_code == 404
    assert len(inventory.calls) == 3


@respx.mock
def test_batch_request_cap_is_shared_between_cards(monkeypatch):
    monkeypatch.setattr(r, "MAX_READ_REQUESTS", 3)
    inventory = Inventory([record(1), record(2)])
    with pytest.raises(ValueError, match="request budget"):
        run(a.get_entity_cards, entity_ids=[record(1)["id"], record(2)["id"]])
    assert len(inventory.calls) == 3


@respx.mock
def test_batch_uses_one_shared_deadline_not_a_fresh_timeout_per_card(monkeypatch):
    monkeypatch.setattr(r, "READ_TIMEOUT_SECONDS", 0.06)
    inventory = Inventory([record(1), record(2)])
    respond = inventory.respond
    async def delayed(request):
        await anyio.sleep(0.02)
        return respond(request)
    inventory.route.mock(side_effect=delayed)
    start = time.monotonic()
    with pytest.raises(ValueError, match="timed out"):
        run(a.get_entity_cards, entity_ids=[record(1)["id"], record(2)["id"]])
    assert time.monotonic() - start < 0.5
    assert len(inventory.calls) < 4


@respx.mock
def test_concurrent_two_card_batches_and_cross_actor_denials_stay_scoped():
    alice_group, bob_group = str(uuid4()), str(uuid4())
    scopes = {
        "Bearer alice": (alice_group, [record(1), record(2)]),
        "Bearer bob": (bob_group, [record(3), record(4)]),
    }
    seen = []
    async def respond(request):
        token, group = request.headers["authorization"], request.headers["x-tenant"]
        seen.append((token, group))
        expected_group, entities = scopes[token]
        await anyio.sleep(0)
        if group != expected_group:
            return httpx.Response(403, json={})
        requested = request.url.path.split("/")[4]
        entity = next((e for e in entities if e["id"] == requested), None)
        if not entity:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=(
            [{"id": entity["id"], "name": entity["name"]}] if request.url.path.endswith("/path") else entity
        ))
    respx.route(url__startswith=BASE).mock(side_effect=respond)
    async def worker(token):
        group, entities = scopes[token]
        with a.homebox_scope(token, group):
            return await a.get_entity_cards([e["id"] for e in entities])
    async def exercise():
        result = await asyncio.gather(worker("Bearer alice"), worker("Bearer bob"))
        assert [c["id"] for c in result[0]["items"]] == [record(1)["id"], record(2)["id"]]
        assert [c["id"] for c in result[1]["items"]] == [record(3)["id"], record(4)["id"]]
        with a.homebox_scope("Bearer alice", alice_group):
            with pytest.raises(httpx.HTTPStatusError) as exc:
                await a.get_entity_cards([record(1)["id"], record(3)["id"]])
            assert exc.value.response.status_code == 404
        with a.homebox_scope("Bearer alice", bob_group):
            with pytest.raises(httpx.HTTPStatusError) as exc:
                await a.get_entity_cards([record(1)["id"], record(2)["id"]])
            assert exc.value.response.status_code == 403
    asyncio.run(exercise())
    assert ("Bearer bob", alice_group) not in seen


@respx.mock
@pytest.mark.parametrize("arguments", [
    {"entity_ids": []}, {"entity_ids": [str(UUID(int=1))] * 3},
    {"entity_ids": [str(UUID(int=1))], "query": "unsupported"},
])
def test_batch_mcp_schema_and_raw_unknown_argument_guards(arguments, mcp_client):
    inventory = Inventory([record(1)])
    response = mcp_client.post("/mcp", headers={
        "Authorization": f"Bearer {a.SECRET}", "X-Homebox-Authorization": "Bearer alice",
        "X-Tenant": str(uuid4()), "Accept": "application/json, text/event-stream",
    }, json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "get_entity_cards", "arguments": arguments}})
    assert response.status_code == 200
    assert response.json()["result"]["isError"] is True
    assert not inventory.calls
