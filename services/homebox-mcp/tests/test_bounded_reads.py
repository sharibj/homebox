import asyncio
import copy
import json
import time
from uuid import UUID, uuid4

import anyio
import httpx
import pytest
import respx

from test_adapter import a
from adapter import reads as r

BASE = "http://homebox:7745/api/v1"


def record(number, *, location=False, legacy=False, archived=False, quantity=1, **extra):
    entity = {
        "id": str(UUID(int=number)), "name": f"Record {number:04}", "archived": archived,
        "quantity": quantity, "tags": [],
    }
    if not legacy:
        entity["entityType"] = {"isLocation": location}
    return {**entity, **extra}


class Inventory:
    """Disposable REST fixture following the inspected Homebox entity filters."""

    def __init__(self, entities, paths=None):
        self.entities = {e["id"]: e for e in entities}
        self.paths = paths or {}
        self.calls = []
        self.listing_response = None
        self.detail_overrides = {}
        self.route = respx.route(url__startswith=BASE).mock(side_effect=self.respond)

    def respond(self, request):
        self.calls.append(request)
        assert request.method == "GET", "Read tools must never dispatch mutations"
        relative = request.url.path.removeprefix("/api/v1")
        if relative == "/entities":
            if self.listing_response:
                return self.listing_response(request)
            q = request.url.params
            assert q["includeArchived"] == "false"
            assert q["orderBy"] == "name" and q["orderDirection"] == "asc"
            values = [e for e in self.entities.values() if not e["archived"]]
            values = [e for e in values if bool((e.get("entityType") or {}).get("isLocation"))
                      == (q["isLocation"] == "true")]
            if q.get("q"):
                values = [e for e in values if q["q"].casefold() in e["name"].casefold()]
            if q.get("parentIds"):
                values = [e for e in values if (e.get("location") or e.get("parent") or {}).get("id")
                          == q["parentIds"]]
            values.sort(key=lambda e: (e["name"], e["id"]))
            page, size = int(q["page"]), int(q["pageSize"])
            return httpx.Response(200, json={
                "total": len(values), "page": page, "pageSize": size,
                "items": values[(page - 1) * size:page * size],
            })
        eid = relative.split("/")[2]
        entity = self.entities.get(eid)
        if entity is None:
            return httpx.Response(404, json={"error": "not found"})
        if relative.endswith("/path"):
            return httpx.Response(200, content=json.dumps(self.paths.get(eid, [
                {"id": eid, "name": entity["name"], "type": "location"},
            ])).encode())
        return httpx.Response(200, json=self.detail_overrides.get(eid, entity))

    @property
    def listings(self):
        return [c for c in self.calls if c.url.path == "/api/v1/entities"]

    @property
    def hydrated_ids(self):
        return [c.url.path.split("/")[4] for c in self.calls
                if c.url.path != "/api/v1/entities"]


def run(tool, **kwargs):
    with a.homebox_scope("Bearer disposable-alice", str(UUID(int=900_001))):
        return asyncio.run(tool(**kwargs))


@respx.mock
@pytest.mark.parametrize("count", [0, 2, 201, 405, 2001])
def test_overview_counts_are_query_wide_active_record_counts_and_work_is_fixed(count, monkeypatch):
    entities = [record(i + 1, legacy=(i == 0), quantity=3 if i == 0 else 5)
                for i in range(count)]
    locations = [record(10_001 + i, location=True) for i in range(7)]
    inventory = Inventory(entities + locations + [
        record(20_001, archived=True), record(20_002, location=True, archived=True),
    ])
    monkeypatch.setattr(a, "all_entities", lambda **_: pytest.fail("Whole inventory fetch"))
    monkeypatch.setattr(a, "detail", lambda _: pytest.fail("Unbounded legacy hydration"))
    output = run(a.inventory_overview, preview_limit=2)
    assert output["itemRecordCount"] == count and output["locationCount"] == 7
    assert output["includeArchived"] is False and output["complete"] is True
    assert len(output["items"]) == min(count, 2)
    assert len(inventory.listings) == 2
    assert [c.url.params["page"] for c in inventory.listings] == ["1", "1"]
    assert [c.url.params["pageSize"] for c in inventory.listings] == ["2", "1"]
    assert len(inventory.calls) == 2 + 2 * min(count, 2)
    assert set(inventory.hydrated_ids) <= {e["id"] for e in entities[:2]}


@respx.mock
@pytest.mark.parametrize("page,size,returned,more", [
    (1, 20, 20, True), (11, 20, 20, True), (21, 20, 5, False),
    (22, 20, 0, False), (81, 5, 5, False),
])
def test_item_pages_beyond_200_and_400_only_hydrate_returned_page(page, size, returned, more):
    inventory = Inventory([record(i) for i in range(1, 406)])
    output = run(a.list_items_page, page=page, page_size=size)
    assert output["total"] == 405 and len(output["items"]) == returned
    assert output["page"] == page and output["pageSize"] == size and output["hasMore"] is more
    assert output["complete"] is True  # Complete page, not exhaustive match set.
    assert len(inventory.listings) == 1 and len(inventory.calls) == 1 + 2 * returned
    assert set(inventory.hydrated_ids) == {e["id"] for e in output["items"]}


@respx.mock
@pytest.mark.parametrize("kind,location", [("locations", True), ("items", False)])
def test_many_destinations_are_paged_including_items_without_children(kind, location):
    entities = [record(i, location=location, legacy=(not location and i == 401))
                for i in range(1, 406)]
    inventory = Inventory(entities)
    output = run(a.list_destinations_page, kind=kind, page=21)
    assert output["total"] == 405 and len(output["destinations"]) == 5
    assert output["hasMore"] is False
    expected_kind = "location" if location else "item"
    assert all(d["kind"] == expected_kind for d in output["destinations"])
    assert entities[400]["id"] in {d["id"] for d in output["destinations"]}
    assert len(inventory.calls) == 11 and len(inventory.listings) == 1
    assert all(d["path"] == d["name"] for d in output["destinations"])


@respx.mock
def test_duplicate_exact_destination_on_next_page_is_not_hidden():
    values = [record(i, location=True, name="Entry" if i in {1, 21} else f"Entry {i:02}")
              for i in range(1, 26)]
    # Page ordering is deliberately controlled so exact names span the boundary.
    inventory = Inventory(values)
    def listing(request):
        page = int(request.url.params["page"])
        assert request.url.params["q"] == "Entry"
        return httpx.Response(200, json={
            "total": 25, "page": page, "pageSize": 20, "items": values[(page - 1) * 20:page * 20],
        })
    inventory.listing_response = listing
    first = run(a.list_destinations_page, query="Entry")
    second = run(a.list_destinations_page, query="Entry", page=2)
    assert first["total"] == second["total"] == 25
    assert first["hasMore"] is True and second["hasMore"] is False
    assert first["complete"] is True and second["complete"] is True
    assert sum(d["name"] == "Entry" for d in first["destinations"]) == 1
    assert sum(d["name"] == "Entry" for d in second["destinations"]) == 1
    assert first["filters"] == second["filters"]


@respx.mock
def test_no_local_exact_hit_before_next_page_does_not_become_not_found_or_zero():
    values = [record(i, location=True, name="Entry" if i == 25 else f"Other {i:02}",
                     description="Entry supplies") for i in range(1, 26)]
    inventory = Inventory(values)
    def matching_description_page(request):
        page = int(request.url.params["page"])
        assert request.url.params["q"] == "Entry"
        return httpx.Response(200, json={
            "total": 25, "page": page, "pageSize": 20, "items": values[(page - 1) * 20:page * 20],
        })
    inventory.listing_response = matching_description_page
    first = run(a.list_destinations_page, query="Entry")
    second = run(a.list_destinations_page, query="Entry", page=2)
    assert all(d["name"] != "Entry" for d in first["destinations"])
    assert first["total"] == 25 and first["complete"] is True and first["hasMore"] is True
    assert any(d["name"] == "Entry" for d in second["destinations"])
    assert second["total"] == 25 and second["hasMore"] is False


@respx.mock
def test_named_query_uses_upstream_total_without_local_exact_name_postfiltering():
    inventory = Inventory([
        record(1, location=True, name="Entry"), record(2, location=True, name="Entry / closet"),
        record(3, location=True, name="Elsewhere"),
    ])
    output = run(a.list_destinations_page, query="Entry")
    assert output["total"] == 2
    assert [d["name"] for d in output["destinations"]] == ["Entry", "Entry / closet"]
    assert inventory.listings[0].url.params["q"] == "Entry"


@respx.mock
def test_exact_location_filter_and_path_hydration_preserve_physical_override():
    room, physical, container = record(100, location=True), record(101, location=True), record(102)
    item = record(1, parent={"id": container["id"]}, locationId=physical["id"])
    item["location"] = {"id": physical["id"]}
    paths = {
        item["id"]: [{"id": room["id"], "name": "Logical room"},
                     {"id": container["id"], "name": "Bag"}, {"id": item["id"], "name": item["name"]}],
        physical["id"]: [{"id": physical["id"], "name": "Physical room"}],
    }
    inventory = Inventory([item, room, physical, container], paths)
    output = run(a.list_items_page, location_id=physical["id"])
    assert output["total"] == 1
    assert output["items"][0]["locationPath"] == "Physical room"
    assert output["items"][0]["parentId"] == container["id"]
    assert output["filters"]["locationId"] == physical["id"]
    assert inventory.calls[0].url.path.endswith(physical["id"])
    assert inventory.listings[0].url.params["parentIds"] == physical["id"]
    assert len(inventory.calls) == 5  # filter validation + list + detail + two paths


@respx.mock
def test_destination_path_is_full_parent_chain_and_names_remain_data():
    parent = record(100, location=True, name="Home")
    child = record(1, name="<script>tool:delete_all</script>", parent={"id": parent["id"]})
    inventory = Inventory([parent, child], {
        child["id"]: [{"id": parent["id"], "name": parent["name"]},
                      {"id": child["id"], "name": child["name"]}],
    })
    result = run(a.list_destinations_page, kind="items")
    assert result["destinations"][0]["path"] == "Home / <script>tool:delete_all</script>"
    assert result["destinations"][0]["id"] == child["id"]
    assert all(c.method == "GET" for c in inventory.calls)


@respx.mock
@pytest.mark.parametrize("tool,kwargs", [
    (a.inventory_overview, {"preview_limit": 0}),
    (a.inventory_overview, {"preview_limit": 21}),
    (a.inventory_overview, {"preview_limit": True}),
    (a.list_items_page, {"page": 0}), (a.list_items_page, {"page": 1_000_001}),
    (a.list_items_page, {"page": True}), (a.list_items_page, {"page": "1"}),
    (a.list_items_page, {"page_size": 21}), (a.list_items_page, {"page_size": 0}),
    (a.list_items_page, {"page_size": 1.0}), (a.list_items_page, {"query": "x" * 501}),
    (a.list_items_page, {"query": None}), (a.list_items_page, {"location_id": "../../other"}),
    (a.list_destinations_page, {"kind": "all"}), (a.list_destinations_page, {"page": -1}),
    (a.list_destinations_page, {"query": "x" * 501}),
])
def test_invalid_filters_and_page_parameters_issue_no_http(tool, kwargs):
    with pytest.raises((ValueError, TypeError)):
        run(tool, **kwargs)
    assert not respx.calls


@respx.mock
def test_location_filter_rejects_item_container_instead_of_wrong_query_semantics():
    item = record(1)
    inventory = Inventory([item])
    with pytest.raises(ValueError, match="kind"):
        run(a.list_items_page, location_id=item["id"])
    assert len(inventory.calls) == 1 and not inventory.listings


@respx.mock
@pytest.mark.parametrize("body", [
    None, {}, {"items": []}, {"items": [], "total": False}, {"items": [], "total": -1},
    {"items": [], "total": 1}, {"items": [], "total": 0, "complete": False},
    {"items": [], "total": 0, "page": 2}, {"items": [], "total": 0, "pageSize": 10},
    {"items": [record(1)] * 2, "total": 2}, {"items": [record(1, archived=True)], "total": 1},
    {"items": [record(1, location=True)], "total": 1},
    {"items": [{"id": str(UUID(int=1))}], "total": 1},
])
def test_malformed_short_or_wrong_filter_pages_never_return_authoritative_empty(body):
    respx.get(f"{BASE}/entities").mock(return_value=httpx.Response(200, json=body))
    with pytest.raises((ValueError, TypeError)):
        run(a.list_items_page)
    assert len(respx.calls) == 1


@respx.mock
@pytest.mark.parametrize("status", [401, 403, 404, 500])
def test_upstream_errors_propagate_without_success_zero_or_mutation(status):
    route = respx.get(f"{BASE}/entities").mock(return_value=httpx.Response(status, json={"error": "unavailable"}))
    with pytest.raises(httpx.HTTPStatusError) as exc:
        run(a.inventory_overview)
    assert exc.value.response.status_code == status
    assert route.call_count == 1


@respx.mock
def test_overview_location_count_failure_cannot_turn_into_zero():
    respx.get(f"{BASE}/entities", params__contains={"isLocation": "false"}).mock(
        return_value=httpx.Response(200, json={"items": [], "total": 0}))
    respx.get(f"{BASE}/entities", params__contains={"isLocation": "true"}).mock(
        return_value=httpx.Response(503, json={}))
    with pytest.raises(httpx.HTTPStatusError):
        run(a.inventory_overview)


@respx.mock
@pytest.mark.parametrize("path", [[], None, [{"name": "Unknown"}],
                                  [{"id": str(UUID(int=2)), "name": "Wrong"}],
                                  [{"id": str(UUID(int=1)), "name": "Cycle"}] * 2])
def test_incomplete_paths_fail_entire_page(path):
    item = record(1)
    Inventory([item], {item["id"]: path})
    with pytest.raises(ValueError, match="path|UUID"):
        run(a.list_items_page)


@respx.mock
def test_deleted_or_archived_detail_invalidates_page_instead_of_silently_dropping_it():
    item = record(1)
    inventory = Inventory([item])
    inventory.detail_overrides[item["id"]] = {**item, "archived": True}
    with pytest.raises(ValueError, match="archived"):
        run(a.list_items_page)


@respx.mock
@pytest.mark.parametrize("tool,kwargs", [
    (a.inventory_overview, {}), (a.list_items_page, {}),
    (a.list_destinations_page, {"kind": "items"}),
    (a.get_entity_card, {"entity_id": str(UUID(int=42))}),
])
def test_every_new_read_copies_only_current_actor_and_collection(tool, kwargs):
    inventory = Inventory([record(42)] if tool is a.get_entity_card else [])
    first, second = str(uuid4()), str(uuid4())
    async def exercise():
        with a.homebox_scope("Bearer alice", first):
            await tool(**kwargs)
            with a.homebox_scope("Bearer bob", second):
                await tool(**kwargs)
            await tool(**kwargs)
    asyncio.run(exercise())
    per_read = 2 if tool in {a.inventory_overview, a.get_entity_card} else 1
    assert [(c.headers["authorization"], c.headers["x-tenant"]) for c in inventory.calls] == (
        [("Bearer alice", first)] * per_read + [("Bearer bob", second)] * per_read
        + [("Bearer alice", first)] * per_read)
    with pytest.raises(LookupError):
        a.current_client.get()
    with pytest.raises(LookupError):
        asyncio.run(tool(**kwargs))


@respx.mock
def test_real_deadline_cancels_slow_upstream_and_returns_no_partial_result(monkeypatch):
    monkeypatch.setattr(r, "READ_TIMEOUT_SECONDS", 0.025)
    async def slow(_):
        await anyio.sleep(10)
        return httpx.Response(200, json={"items": [], "total": 0})
    respx.get(f"{BASE}/entities").mock(side_effect=slow)
    start = time.monotonic()
    with pytest.raises(ValueError, match="timed out"):
        run(a.inventory_overview)
    assert time.monotonic() - start < 0.5


@respx.mock
def test_response_byte_budget_rejects_oversized_input(monkeypatch):
    monkeypatch.setattr(r, "MAX_RESPONSE_BYTES", 32)
    respx.get(f"{BASE}/entities").mock(return_value=httpx.Response(200, content=b"x" * 33))
    with pytest.raises(ValueError, match="size"):
        run(a.list_items_page)


@respx.mock
def test_existing_exhaustive_search_still_visits_every_page_for_duplicate_detection(monkeypatch):
    values = [record(i) for i in range(1, 406)]
    seen = []
    def listing(request):
        page = int(request.url.params["page"])
        assert request.url.params["pageSize"] == "200"
        seen.append(page)
        return httpx.Response(200, json={"total": 405, "items": values[(page - 1) * 200:page * 200]})
    respx.get(f"{BASE}/entities").mock(side_effect=listing)
    by_id = {e["id"]: e for e in values}
    monkeypatch.setattr(a, "detail", by_id.__getitem__)
    monkeypatch.setattr(a, "card", copy.deepcopy)
    with a.homebox_scope("Bearer alice", str(uuid4())):
        output = a.search_items("Record")
    assert seen == [1, 2, 3] and output["total"] == 405 and len(output["items"]) == 405


def test_mcp_discovery_retains_nine_tools_and_adds_read_only_bounded_schemas():
    tools = asyncio.run(a.mcp.list_tools())
    by_name = {t.name: t.model_dump(by_alias=True) for t in tools}
    assert set(by_name) == {
        "get_entity", "search_items", "list_locations", "location_contents",
        "create_item", "move_item", "update_item", "set_tags", "create_location",
        "inventory_overview", "list_items_page", "list_destinations_page", "get_entity_card", "get_entity_cards",
    }
    for name in ("inventory_overview", "list_items_page", "list_destinations_page"):
        assert by_name[name]["annotations"]["readOnlyHint"] is True
        props = by_name[name]["inputSchema"]["properties"]
        size = props.get("page_size", props.get("preview_limit"))
        assert size["minimum"] == 1 and size["maximum"] == 20
    assert by_name["list_destinations_page"]["inputSchema"]["properties"]["kind"]["enum"] == [
        "locations", "items",
    ]
    assert by_name["get_entity_card"]["annotations"]["readOnlyHint"] is True
    assert by_name["get_entity_cards"]["annotations"]["readOnlyHint"] is True
    batch = by_name["get_entity_cards"]["inputSchema"]["properties"]["entity_ids"]
    assert batch["minItems"] == 1 and batch["maxItems"] == 2
    assert "exclude_entity_id" not in by_name["list_destinations_page"]["inputSchema"]["properties"]
    for name, allowed in r.StrictReadArguments.parameters.items():
        assert set(by_name[name]["inputSchema"]["properties"]) == allowed


@respx.mock
@pytest.mark.parametrize("location", [True, False])
def test_exact_entity_card_never_expands_location_or_container_children(location, monkeypatch):
    entity = record(1, location=location)
    inventory = Inventory([entity] + [record(i) for i in range(2, 407)])
    monkeypatch.setattr(a, "all_entities", lambda **_: pytest.fail("Recursive children scan"))
    output = run(a.get_entity_card, entity_id=entity["id"])
    assert output["id"] == entity["id"] and output["isLocation"] is location
    assert "children" not in output and len(inventory.calls) == 2 and not inventory.listings


@respx.mock
def test_worst_case_twenty_physical_override_cards_stay_under_fixed_http_budget():
    physical = record(10_001, location=True, name="Physical room")
    entities = [record(i, locationId=physical["id"]) for i in range(1, 406)]
    inventory = Inventory(entities + [physical])
    output = run(a.inventory_overview, preview_limit=20)
    assert output["itemRecordCount"] == 405 and len(output["items"]) == 20
    assert len(inventory.calls) == 62
    assert all(i["locationPath"] == "Physical room" for i in output["items"])
    assert set(inventory.hydrated_ids) <= {physical["id"], *(e["id"] for e in entities[:20])}


@respx.mock
def test_request_budget_is_enforced_before_an_extra_network_call(monkeypatch):
    monkeypatch.setattr(r, "MAX_READ_REQUESTS", 1)
    inventory = Inventory([record(1)])
    with pytest.raises(ValueError, match="request budget"):
        run(a.list_items_page)
    assert len(inventory.calls) == 1


@respx.mock
def test_concurrent_reads_keep_actor_collection_headers_isolated():
    seen = []
    alice, bob = str(uuid4()), str(uuid4())
    async def respond(request):
        seen.append((request.headers["authorization"], request.headers["x-tenant"]))
        await anyio.sleep(0)
        return httpx.Response(200, json={"items": [], "total": 0})
    respx.get(f"{BASE}/entities").mock(side_effect=respond)
    async def worker(token, tenant):
        with a.homebox_scope(token, tenant):
            await a.inventory_overview()
            assert a.current_client.get().headers["Authorization"] == token
    async def exercise():
        await asyncio.gather(worker("Bearer alice", alice), worker("Bearer bob", bob))
    asyncio.run(exercise())
    assert sorted(seen) == sorted([("Bearer alice", alice)] * 2 + [("Bearer bob", bob)] * 2)


@respx.mock
def test_inaccessible_exact_filter_stops_before_listing_or_disclosing_names():
    eid = str(UUID(int=1))
    route = respx.get(f"{BASE}/entities/{eid}").mock(return_value=httpx.Response(403, json={}))
    with pytest.raises(httpx.HTTPStatusError):
        run(a.list_items_page, location_id=eid)
    assert len(respx.calls) == route.call_count == 1


@respx.mock
def test_actual_mcp_entry_point_authenticates_new_read_and_returns_structured_page(mcp_client):
    inventory = Inventory([])
    headers = {
        "Authorization": f"Bearer {a.SECRET}", "X-Homebox-Authorization": "Bearer alice",
        "X-Tenant": str(UUID(int=900_001)), "Accept": "application/json, text/event-stream",
    }
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "list_destinations_page", "arguments": {"kind": "locations"}}}
    denied = mcp_client.post("/mcp", json=payload)
    assert denied.status_code == 401 and not inventory.calls
    bad_tenant = mcp_client.post("/mcp", json=payload, headers={**headers, "X-Tenant": "not-a-uuid"})
    assert bad_tenant.status_code == 400 and not inventory.calls
    response = mcp_client.post("/mcp", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert not result.get("isError", False), result
    content = result.get("structuredContent") or json.loads(result["content"][0]["text"])
    assert content["destinations"] == [] and content["total"] == 0 and content["complete"] is True
    assert len(inventory.calls) == 1
    assert inventory.calls[0].headers["authorization"] == "Bearer alice"


@respx.mock
def test_real_mcp_input_validation_rejects_out_of_range_page_without_http(mcp_client):
    headers = {
        "Authorization": f"Bearer {a.SECRET}", "X-Homebox-Authorization": "Bearer alice",
        "X-Tenant": str(uuid4()), "Accept": "application/json, text/event-stream",
    }
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "list_items_page", "arguments": {"page_size": 21}}}
    response = mcp_client.post("/mcp", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["isError"] is True
    assert not respx.calls


@respx.mock
@pytest.mark.parametrize("extra", [
    {"exclude_entity_id": str(UUID(int=1))}, {"includeArchived": True},
])
def test_mcp_rejects_unsupported_filters_instead_of_inventing_filtered_totals(mcp_client, extra):
    inventory = Inventory([])
    headers = {
        "Authorization": f"Bearer {a.SECRET}", "X-Homebox-Authorization": "Bearer alice",
        "X-Tenant": str(uuid4()), "Accept": "application/json, text/event-stream",
    }
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "list_destinations_page", "arguments": {"kind": "locations", **extra}}}
    response = mcp_client.post("/mcp", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["result"]["isError"] is True
    assert not inventory.calls
