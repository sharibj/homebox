"""Read-only adaptation of MCP live_compatibility.py for the seeded QA stack."""

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


READS = {
    "inventory_overview", "list_items_page", "list_destinations_page",
    "get_entity_card", "get_entity_cards", "get_entity", "list_locations", "search_items",
}
ROOT = Path("/fixture")
ORACLE = json.loads((ROOT / "fixture-oracle.json").read_text())
CREDENTIALS = json.loads((ROOT / "qa-credentials.json").read_text())
SECRET = (ROOT / "mcp_shared_secret").read_text().strip()


def decoded(result):
    assert not (getattr(result, "is_error", False) or getattr(result, "isError", False)), "MCP read failed"
    value = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
    return value if value is not None else json.loads(result.content[0].text)


def page(value, key, total, number, count):
    assert value["total"] == total and value["page"] == number and value["pageSize"] == 20
    assert value["complete"] is True
    assert value["hasMore"] is (number * 20 < total)
    assert len(value[key]) == count
    return value[key]


async def collection_reads(token, collection_id, empty=False):
    headers = {"Authorization": "Bearer " + SECRET,
               "X-Homebox-Authorization": token, "X-Tenant": collection_id}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as client:
        async with streamable_http_client("http://homebox-mcp:8001/mcp", http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert len(tools.tools) == 14
                assert READS <= {tool.name for tool in tools.tools}

                async def read(name, arguments):
                    assert name in READS, "Only inventory read tools are permitted"
                    return decoded(await session.call_tool(name, arguments))

                overview = await read("inventory_overview", {"preview_limit": 10})
                assert overview["complete"] is True and overview["includeArchived"] is False
                if empty:
                    assert overview["itemRecordCount"] == overview["locationCount"] == 0
                    assert overview["items"] == []
                    page(await read("list_items_page", {"page": 1, "page_size": 20}), "items", 0, 1, 0)
                    for kind in ("locations", "items"):
                        page(await read("list_destinations_page", {"kind": kind, "page_size": 20}),
                             "destinations", 0, 1, 0)
                    inaccessible = await session.call_tool("get_entity_card", {"entity_id": ORACLE["items"][0]["id"]})
                    assert getattr(inaccessible, "is_error", False) or getattr(inaccessible, "isError", False)
                    return {"items": 0, "locations": 0, "crossCollectionExactReadDenied": True}

                expected_items = {item["id"]: item for item in ORACLE["items"]}
                assert overview["itemRecordCount"] == 25 and overview["locationCount"] == 13
                assert len(overview["items"]) == 10
                assert {item["id"] for item in overview["items"]} <= set(expected_items)
                one = page(await read("list_items_page", {"page": 1, "page_size": 20}), "items", 25, 1, 20)
                two = page(await read("list_items_page", {"page": 2, "page_size": 20}), "items", 25, 2, 5)
                assert len({item["id"] for item in one + two}) == 25
                assert {item["id"] for item in one + two} == set(expected_items)
                for item in one + two:
                    expected = expected_items[item["id"]]
                    assert all(item[key] == expected[key] for key in ("name", "quantity", "locationId"))
                assert sum(item["quantity"] for item in one + two) == 31

                locations = page(await read("list_destinations_page", {"kind": "locations", "page_size": 20}),
                                 "destinations", 13, 1, 13)
                assert {item["path"]: item["id"] for item in locations} == ORACLE["locations"]
                assert all(item["kind"] == "location" for item in locations)
                entries = [item for item in locations if item["name"] == "Entry"]
                assert {item["path"] for item in entries} == {"Ground floor / Entry", "Upper floor / Entry"}
                containers = []
                for number, count in ((1, 20), (2, 5)):
                    containers += page(await read("list_destinations_page",
                                                  {"kind": "items", "page": number, "page_size": 20}),
                                       "destinations", 25, number, count)
                assert {item["id"] for item in containers} == set(expected_items)
                assert all(item["kind"] == "item" for item in containers)
                tote = next(item for item in ORACLE["items"] if item["name"] == "Empty blue tote")
                assert any(item["id"] == tote["id"] and item["path"] == "Workshop / Empty blue tote"
                           for item in containers)
                legacy_tote = await read("get_entity", {"entity_id": tote["id"]})
                assert legacy_tote["name"] == "Empty blue tote" and legacy_tote["isLocation"] is False
                assert all(item.get("parentId") != tote["id"] for item in one + two)
                legacy_search = await read("search_items", {"query": "Portable charger"})
                assert legacy_search["total"] == 1 and legacy_search["items"][0]["quantity"] == 3
                legacy_locations = await read("list_locations", {})
                assert {item["id"] for item in legacy_locations["locations"]} == set(ORACLE["locations"].values())
                ground = ORACLE["locations"]["Ground floor"]
                entry = ORACLE["locations"]["Ground floor / Entry"]
                exact = await read("get_entity_card", {"entity_id": entry})
                assert exact["isLocation"] is True and exact["ancestorIds"] == [ground]
                assert "children" not in exact
                charger = next(item for item in ORACLE["items"] if item["name"] == "Portable charger")
                pair = await read("get_entity_cards", {"entity_ids": [charger["id"], entry]})
                assert [item["id"] for item in pair["items"]] == [charger["id"], entry]
                assert pair["items"][0]["ancestorIds"] == [ground, entry]
                assert pair["items"][1]["ancestorIds"] == [ground]
                assert all("children" not in item for item in pair["items"])
                assert pair["items"][0]["path"] == "Ground floor / Entry / Portable charger"
                assert pair["items"][1]["path"] == "Ground floor / Entry"
                assert pair["items"][0]["locationPath"] == "Ground floor / Entry"
                variants = (charger["id"], charger["id"].upper(), charger["id"].replace("-", ""),
                            "urn:uuid:" + charger["id"])
                for repeated in variants:
                    accepted = await read("get_entity_cards", {"entity_ids": [repeated, entry]})
                    assert [item["id"] for item in accepted["items"]] == [charger["id"], entry]
                    rejected = await session.call_tool("get_entity_cards",
                                                       {"entity_ids": [charger["id"], repeated]})
                    # The protocol contracts an error, not its masked text.
                    # The positive call above proves this is not a format error.
                    assert getattr(rejected, "is_error", False) or getattr(rejected, "isError", False)
                normalized = await read("get_entity_cards",
                                        {"entity_ids": [charger["id"].upper(), entry.replace("-", "")]})
                assert [item["id"] for item in normalized["items"]] == [charger["id"], entry]
                return {"items": 25, "locations": 13, "quantitySum": 31,
                        "itemPages": [20, 5], "itemDestinationPages": [20, 5],
                        "distinctEntryPaths": 2, "emptyItemDestination": True,
                        "exactAncestry": True, "legacyReadsCompatible": True,
                        "normalizedDuplicateBatchesRejected": len(variants),
                        "distinctNormalizedFormsAccepted": True,
                        "physicalLocationPathPreserved": True,
                        "logicalCardPathAvailable": all("path" in item for item in pair["items"])}


async def main():
    assert os.environ.get("ALLOW_DISPOSABLE_CONVERSATION_READS") == "1"
    os.umask(0o077)
    assert CREDENTIALS["username"].endswith("@example.invalid")
    assert ORACLE["primaryCollection"]["name"] == "QA Household"
    login = {key: CREDENTIALS[key] for key in ("username", "password")}
    with httpx.Client(base_url="http://homebox:7745/api/v1", timeout=30) as native:
        response = native.post("/users/login", json=login)
        response.raise_for_status()
        token = response.json()["token"]
    primary = await collection_reads(token, ORACLE["primaryCollection"]["id"])
    secondary = await collection_reads(token, ORACLE["secondaryCollection"]["id"], empty=True)

    with httpx.Client(timeout=30) as client:
        for url in ("http://assistant-api:8000/readyz", "http://homebox-mcp:8001/readyz"):
            response = client.get(url); response.raise_for_status()
        with httpx.Client(base_url="http://assistant-api:8000/api/assistant/v1",
                          headers={"Origin": "http://localhost:8088"}, timeout=30) as app:
            response = app.post("/auth/login", json=login); response.raise_for_status()
            session = response.json()
            for role in ("chat", "vision"):
                assert session["ai"][role] == {"provider": "anthropic-messages", "model": "claude-haiku-4-5"}
            expected_instance = os.environ.get("QA_EXPECTED_INSTANCE_ID", "c6033357-2488-44a1-a274-4632c142ae1d")
            assert session["scope"]["instanceId"] == expected_instance
            app.headers.update({"X-Collection-ID": session["collectionId"],
                                "X-Context-Version": str(session["scope"]["contextVersion"])})
            current = app.get("/conversations/current"); current.raise_for_status()
            assert current.json()["conversation"] is None
        headers = {"Host": "localhost:8088"}
        index = client.get("http://proxy:8088/", headers=headers); index.raise_for_status()
        asset = re.search(r'src="(/assets/index-[^"]+\.js)"', index.text).group(1)
        javascript = client.get("http://proxy:8088" + asset, headers=headers); javascript.raise_for_status()
        assert "javascript" in javascript.headers["content-type"] and len(javascript.content) > 10000
        assert client.get("http://proxy:8088/api/assistant/v1/session", headers=headers).status_code == 401
    report = {"status": "passed", "toolCount": 14, "primary": primary, "secondary": secondary,
              "actualServedAsset": asset, "bothRoles": "anthropic-messages/claude-haiku-4-5",
              "currentConversationEmptyRead": True, "inventoryWrites": 0, "modelCalls": 0}
    Path("/output/qa-read-gates.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
