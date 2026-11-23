import os
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import respx
from starlette.testclient import TestClient

Path("/tmp/mcp-test-secret").write_text("test-only-mcp-secret-with-32-bytes-minimum")
os.environ["MCP_SHARED_SECRET_FILE"] = "/tmp/mcp-test-secret"
os.environ["HOMEBOX_URL"] = "http://homebox:7745"

from adapter import main as a


def test_mcp_rejects_missing_service_auth():
    with TestClient(a.app) as client:
        assert client.post("/mcp", json={}).status_code == 401
        assert client.get("/healthz").status_code == 200


@respx.mock
def test_credentials_and_collection_are_request_scoped():
    gid1, gid2 = str(uuid4()), str(uuid4())
    seen = []
    route = respx.get("http://homebox:7745/api/v1/entities")
    route.mock(side_effect=lambda request: (
        seen.append((request.headers["authorization"], request.headers["x-tenant"]))
        or httpx.Response(200, json={"items": [], "total": 0})
    ))
    with a.homebox_scope("Bearer alice", gid1):
        a.search_items()
        with a.homebox_scope("Bearer bob", gid2):
            a.search_items()
        a.search_items()
    assert seen == [("Bearer alice", gid1), ("Bearer bob", gid2), ("Bearer alice", gid1)]
    with pytest.raises(LookupError):
        a.current_client.get()


@respx.mock
def test_move_uses_patch_exact_ids_and_rejects_cycles():
    eid, lid, gid = [str(uuid4()) for _ in range(3)]
    item = {"id": eid, "name": "Cable", "entityType": {"isLocation": False}}
    location = {"id": lid, "name": "Box", "entityType": {"isLocation": True}}
    respx.get(f"http://homebox:7745/api/v1/entities/{eid}").mock(return_value=httpx.Response(200, json=item))
    respx.get(f"http://homebox:7745/api/v1/entities/{lid}").mock(return_value=httpx.Response(200, json=location))
    respx.get(f"http://homebox:7745/api/v1/entities/{eid}/path").mock(return_value=httpx.Response(200, json=[]))
    patch = respx.patch(f"http://homebox:7745/api/v1/entities/{eid}").mock(return_value=httpx.Response(200, json={}))
    with a.homebox_scope("Bearer alice", gid):
        a.move_item(eid, lid)
        assert patch.call_count == 1
        with pytest.raises(ValueError, match="itself"):
            a.move_item(eid, eid)
    assert b'"parentId"' in patch.calls[0].request.content


@respx.mock
def test_update_preserves_explicit_location_and_custom_fields():
    eid, lid, parent, gid = [str(uuid4()) for _ in range(4)]
    full = {
        "id": eid, "name": "Cable", "quantity": 1, "locationId": lid,
        "syncChildEntityLocations": True,
        "parent": {"id": parent}, "fields": [{"name": "rating", "type": "number", "numberValue": 5}],
    }
    respx.get(f"http://homebox:7745/api/v1/entities/{eid}").mock(return_value=httpx.Response(200, json=full))
    respx.get(f"http://homebox:7745/api/v1/entities/{eid}/path").mock(return_value=httpx.Response(200, json=[]))
    respx.get(f"http://homebox:7745/api/v1/entities/{lid}/path").mock(
        return_value=httpx.Response(200, json=[{"id": lid, "name": "Physical room"}]))
    put = respx.put(f"http://homebox:7745/api/v1/entities/{eid}").mock(return_value=httpx.Response(200, json=full))
    with a.homebox_scope("Bearer alice", gid):
        output = a.update_item(eid, a.ItemChanges(notes="Reviewed"))
        assert output["locationPath"] == "Physical room"
    import json
    body = json.loads(put.calls[0].request.content)
    assert body["locationId"] == lid
    assert body["parentId"] == parent
    assert body["fields"][0]["numberValue"] == 5
    assert body["syncChildEntityLocations"] is True


@respx.mock
def test_pagination_does_not_truncate_inventory():
    gid = str(uuid4())
    seen = []
    def response(request):
        page = int(request.url.params["page"])
        seen.append(page)
        return httpx.Response(200, json={
            "total": 201,
            "items": [{"id": str(i)} for i in range(200)] if page == 1 else [{"id": "200"}],
        })
    respx.get("http://homebox:7745/api/v1/entities").mock(side_effect=response)
    with a.homebox_scope("Bearer alice", gid):
        assert len(a.all_entities()) == 201
    assert seen == [1, 2]


@respx.mock
def test_location_contents_includes_nested_locations():
    gid, parent, child = [str(uuid4()) for _ in range(3)]
    def entity(eid, name, parent_id=None):
        return {"id": eid, "name": name, "entityType": {"isLocation": True},
                **({"parent": {"id": parent_id}} if parent_id else {})}
    respx.get(f"http://homebox:7745/api/v1/entities/{parent}").mock(
        return_value=httpx.Response(200, json=entity(parent, "Garage")))
    respx.get(f"http://homebox:7745/api/v1/entities/{child}").mock(
        return_value=httpx.Response(200, json=entity(child, "Shelf", parent)))
    for eid in (parent, child):
        respx.get(f"http://homebox:7745/api/v1/entities/{eid}/path").mock(return_value=httpx.Response(200, json=[]))
    def listing(request):
        values = [entity(child, "Shelf", parent)] if request.url.params.get("isLocation") == "true" else []
        return httpx.Response(200, json={"items": values, "total": len(values)})
    respx.get("http://homebox:7745/api/v1/entities").mock(side_effect=listing)
    with a.homebox_scope("Bearer alice", gid):
        value = a.get_entity(parent)
    assert value["children"][0]["id"] == child
