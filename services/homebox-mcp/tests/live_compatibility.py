"""Live protocol/API proof. Run only against a disposable Homebox database."""
import asyncio
import json
import os
import time
from uuid import uuid4

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

HB = os.environ.get("TEST_HOMEBOX_URL", "http://homebox:7745")
MCP = os.environ.get("TEST_MCP_URL", "http://mcp:8001/mcp")
SECRET = "disposable-only-mcp-shared-secret-32-bytes"


def wait_ready(url):
    for _ in range(60):
        try:
            if httpx.get(url, timeout=2).is_success:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"Disposable service did not become ready: {url}")


def account(label):
    email = f"{label}-{uuid4().hex}@example.invalid"
    password = "disposable-test-password"
    with httpx.Client(base_url=f"{HB}/api/v1", timeout=30) as client:
        response = client.post("/users/register", json={
            "name": label, "email": email, "password": password,
        })
        response.raise_for_status()
        login = client.post("/users/login", json={"username": email, "password": password})
        login.raise_for_status()
        token = login.json()["token"]
        client.cookies.clear()
        user = client.get("/users/self", headers={"Authorization": token}).json()["item"]
        return token, user


def decoded(result):
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise AssertionError(str(result))
    value = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    if value is not None:
        return value
    return json.loads(result.content[0].text)


async def run():
    wait_ready(f"{HB}/api/v1/status")
    wait_ready(MCP.removesuffix("/mcp") + "/readyz")
    alice, user = account("Alice")
    bob, other = account("Bob")
    group = user["defaultGroupId"]
    headers = {"Authorization": f"Bearer {SECRET}", "X-Homebox-Authorization": alice, "X-Tenant": group}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as client:
        async with streamable_http_client(MCP, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 14
                location = decoded(await session.call_tool("create_location", {"name": "Garage"}))
                lid = location["id"]
                created = decoded(await session.call_tool("create_item", {
                    "payload": {"name": "Camping stove", "locationId": lid, "quantity": 1},
                }))
                eid = created["id"]
                overview = decoded(await session.call_tool("inventory_overview", {"preview_limit": 1}))
                assert overview["itemRecordCount"] == 1 and overview["locationCount"] == 1
                assert overview["includeArchived"] is False and overview["complete"] is True
                paged = decoded(await session.call_tool("list_items_page", {"page_size": 1}))
                assert paged["total"] == 1 and paged["items"][0]["id"] == eid
                assert paged["hasMore"] is False and paged["complete"] is True
                bounded_location = decoded(await session.call_tool("get_entity_card", {"entity_id": lid}))
                assert bounded_location["isLocation"] is True and "children" not in bounded_location
                assert bounded_location["ancestorIds"] == []
                assert bounded_location["path"] == "Garage"
                destinations = decoded(await session.call_tool("list_destinations_page", {"kind": "items"}))
                assert any(d["id"] == eid and d["kind"] == "item" for d in destinations["destinations"])
                found = decoded(await session.call_tool("search_items", {"query": "Camping stove"}))
                assert found["total"] == 1
                assert found["items"][0]["id"] == eid
                decoded(await session.call_tool("update_item", {
                    "entity_id": eid, "changes": {"notes": "Reviewed", "serialNumber": "ABC-123"},
                }))
                box = decoded(await session.call_tool("create_location", {"name": "Camping box", "parent_id": lid}))
                decoded(await session.call_tool("move_item", {"entity_id": eid, "destination_id": box["id"]}))
                item = decoded(await session.call_tool("get_entity", {"entity_id": eid}))
                assert item["serialNumber"] == "ABC-123"
                assert item["notes"] == "Reviewed"
                assert item["locationId"] == box["id"]
                pair = decoded(await session.call_tool("get_entity_cards", {"entity_ids": [eid, box["id"]]}))
                assert [e["id"] for e in pair["items"]] == [eid, box["id"]]
                assert pair["items"][0]["ancestorIds"] == [lid, box["id"]]
                assert pair["items"][1]["ancestorIds"] == [lid]
                assert pair["items"][0]["path"] == "Garage / Camping box / Camping stove"
                assert pair["items"][1]["path"] == "Garage / Camping box"
                locations = decoded(await session.call_tool("list_locations", {}))
                assert any(v["id"] == box["id"] for v in locations["locations"])
                destinations = decoded(await session.call_tool("list_destinations_page", {"kind": "locations"}))
                assert destinations["total"] == 2
                assert any(d["id"] == box["id"] and d["path"] == "Garage / Camping box"
                           for d in destinations["destinations"])
                print("PASS: MCP handshake, typed tools, create, search, location paths, preserved metadata, exact-ID move")
    headers["X-Homebox-Authorization"] = bob
    async with httpx2.AsyncClient(headers=headers, timeout=30) as client:
        async with streamable_http_client(MCP, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                result = await session.call_tool("get_entity", {"entity_id": eid})
                assert getattr(result, "is_error", False) or getattr(result, "isError", False)
                print("PASS: a different user's credential cannot access Alice's selected collection")


if __name__ == "__main__":
    asyncio.run(run())
