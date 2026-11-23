"""Pagination, duplicate-name, and physical-location preservation on disposable Homebox."""
import asyncio
import os

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from live_compatibility import HB, MCP, SECRET, account, decoded, wait_ready


async def main():
    wait_ready(HB + "/api/v1/status")
    token, user = account("Edgecase fixture")
    gid = user["defaultGroupId"]
    headers = {"Authorization": token, "X-Tenant": gid}
    with httpx.Client(base_url=HB + "/api/v1", headers=headers, timeout=30) as hb:
        types = hb.get("/entity-types").json()
        location_type = next(t["id"] for t in types if t["isLocation"])
        def create(name, **extra):
            response = hb.post("/entities", json={"name": name, "quantity": 1, **extra})
            response.raise_for_status()
            return response.json()
        room_a = create("Room A", entityTypeId=location_type)
        room_b = create("Room B", entityTypeId=location_type)
        bag = create("Camera bag", parentId=room_b["id"])
        child = create("Lens", parentId=bag["id"], locationId=room_a["id"])
        full = hb.get("/entities/" + child["id"]).json()
        body = dict(full)
        body.update(parentId=bag["id"], locationId=room_a["id"], syncChildEntityLocations=True,
                    tagIds=[], entityTypeId=full["entityType"]["id"])
        response = hb.put("/entities/" + child["id"], json=body)
        response.raise_for_status()
        duplicate1 = create("Duplicate cable")
        duplicate2 = create("Duplicate cable")
        for index in range(201):
            create(f"Paged fixture {index:03d}")
        internal = {"Authorization": f"Bearer {SECRET}", "X-Homebox-Authorization": token, "X-Tenant": gid}
        async with httpx2.AsyncClient(headers=internal, timeout=120) as client:
            async with streamable_http_client(MCP, http_client=client) as streams:
                async with ClientSession(streams[0], streams[1]) as mcp:
                    await mcp.initialize()
                    paged = decoded(await mcp.call_tool("search_items", {"query": "Paged fixture"}))
                    assert paged["total"] == 201
                    found = decoded(await mcp.call_tool("search_items", {"query": "Duplicate cable"}))
                    assert len(found["items"]) == 2
                    decoded(await mcp.call_tool("move_item", {"entity_id": duplicate2["id"], "destination_id": room_a["id"]}))
                    assert hb.get("/entities/" + duplicate1["id"]).json().get("parent") is None
                    decoded(await mcp.call_tool("update_item", {"entity_id": child["id"], "changes": {"notes": "Reviewed"}}))
                    decoded(await mcp.call_tool("set_tags", {"entity_id": child["id"], "tags": ["optics"]}))
                    after = hb.get("/entities/" + child["id"]).json()
                    assert after["parent"]["id"] == bag["id"]
                    assert after["locationId"] == room_a["id"]
                    assert after["syncChildEntityLocations"] is True
                    grounded = decoded(await mcp.call_tool("get_entity", {"entity_id": child["id"]}))
                    assert grounded["locationPath"] == "Room A", grounded
                    decoded(await mcp.call_tool("move_item", {"entity_id": child["id"], "destination_id": room_b["id"]}))
                    moved = hb.get("/entities/" + child["id"]).json()
                    assert moved["syncChildEntityLocations"] is True
                    assert moved["notes"] == "Reviewed"
                    assert moved["location"]["id"] == room_b["id"]
        print("PASS: 201 search records across pages; duplicate names moved only by exact UUID")
        print("PASS: item containment differs from physical location; edit/tags preserve explicit location+sync policy")
        print("PASS: grounded physical-location path and move preserve unrelated metadata")


if __name__ == "__main__":
    asyncio.run(main())
