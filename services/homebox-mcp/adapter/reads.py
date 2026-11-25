"""Bounded, fail-closed inventory reads, separate from exhaustive legacy tools."""

from contextlib import asynccontextmanager
from typing import Annotated, Literal
from uuid import UUID

import anyio
import httpx
from mcp.types import CallToolResult, TextContent
from pydantic import Field

PAGE_LIMIT = 20
READ_TIMEOUT_SECONDS = 20
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_READ_REQUESTS = 64
Page = Annotated[int, Field(strict=True, ge=1, le=1_000_000)]
PageSize = Annotated[int, Field(strict=True, ge=1, le=PAGE_LIMIT)]
Query = Annotated[str, Field(strict=True, max_length=500)]
DestinationKind = Literal["locations", "items"]
EntityIDs = Annotated[list[Annotated[str, Field(strict=True)]],
                      Field(strict=True, min_length=1, max_length=2)]


class StrictReadArguments:
    parameters = {
        "inventory_overview": {"preview_limit"},
        "list_items_page": {"query", "location_id", "page", "page_size"},
        "list_destinations_page": {"kind", "query", "page", "page_size"},
        "get_entity_card": {"entity_id"},
        "get_entity_cards": {"entity_ids"},
    }

    async def __call__(self, context, call_next):
        # MCP's function models otherwise ignore unknown fields. In particular,
        # an unsupported exclusion must never look like a successfully applied
        # filter while preserving an unfiltered upstream count.
        params = context.params
        if context.method == "tools/call" and isinstance(params, dict):
            allowed = self.parameters.get(params.get("name"))
            arguments = params.get("arguments", {})
            if allowed is not None and isinstance(arguments, dict) and set(arguments) - allowed:
                return CallToolResult(
                    content=[TextContent(type="text", text="Unsupported inventory read argument")],
                    is_error=True,
                )
        return await call_next(context)


def validate_page(query: str, page: int, page_size: int):
    if not isinstance(query, str) or len(query) > 500:
        raise ValueError("Query must be a string of at most 500 characters")
    if type(page) is not int or not 1 <= page <= 1_000_000:
        raise ValueError("Page must be an integer between 1 and 1000000")
    if type(page_size) is not int or not 1 <= page_size <= PAGE_LIMIT:
        raise ValueError("Page size must be an integer between 1 and 20")


def entity_id(value) -> str:
    if not isinstance(value, str):
        raise ValueError("Inventory record has no valid UUID")
    return str(UUID(value))


def is_location(entity: dict) -> bool:
    if not isinstance(entity, dict) or type(entity.get("archived")) is not bool:
        raise ValueError("Incomplete inventory record")
    if entity["archived"]:
        raise ValueError("Inventory changed: an archived record requires a fresh read")
    kind = entity.get("entityType")
    if kind is None:
        return False  # Legacy untyped records remain valid non-location items.
    if not isinstance(kind, dict) or type(kind.get("isLocation")) is not bool:
        raise ValueError("Incomplete inventory record type")
    return kind["isLocation"]


@asynccontextmanager
async def bounded_reads(scoped: httpx.Client):
    # The shared synchronous client stays untouched. Each invocation inherits only
    # this authenticated request's current credential/collection and one deadline.
    try:
        with anyio.fail_after(READ_TIMEOUT_SECONDS):
            async with httpx.AsyncClient(
                base_url=scoped.base_url,
                headers={"Authorization": scoped.headers["Authorization"],
                         "X-Tenant": scoped.headers["X-Tenant"]},
                timeout=READ_TIMEOUT_SECONDS, follow_redirects=False, trust_env=False,
            ) as client:
                yield InventoryReads(client)
    except TimeoutError:
        raise ValueError("Inventory read timed out; no complete result is available") from None


class InventoryReads:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.requests = 0

    async def get(self, path: str, **params):
        if self.requests >= MAX_READ_REQUESTS:
            raise ValueError("Inventory read exceeded its request budget")
        self.requests += 1
        async with self.client.stream("GET", path, params=params) as response:
            response.raise_for_status()
            chunks, size = [], 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise ValueError("Inventory response exceeds the bounded read size")
                chunks.append(chunk)
            # Reuse httpx JSON decoding without buffering an unbounded response.
            return httpx.Response(200, content=b"".join(chunks)).json()

    async def detail(self, value: str, location: bool | None = None) -> dict:
        eid = entity_id(value)
        entity = await self.get(f"/entities/{eid}")
        actual_kind = is_location(entity)
        if entity_id(entity.get("id")) != eid or not isinstance(entity.get("name"), str):
            raise ValueError("Inventory returned a different or incomplete record")
        if location is not None and actual_kind != location:
            raise ValueError("Inventory record kind changed; request a fresh read")
        return entity

    async def path(self, value: str) -> list[dict]:
        eid = entity_id(value)
        result = await self.get(f"/entities/{eid}/path")
        if (not isinstance(result, list) or not result or len(result) > 200
                or any(not isinstance(p, dict) or not isinstance(p.get("name"), str) for p in result)):
            raise ValueError("Incomplete inventory path")
        ids = [entity_id(p.get("id")) for p in result]
        if ids[-1] != eid or len(set(ids)) != len(ids):
            raise ValueError("Incomplete or cyclic inventory path")
        return result

    async def card(self, entity: dict, *, include_ancestors: bool = False) -> dict:
        eid = entity_id(entity["id"])
        path = await self.path(eid)
        ancestor_ids = [entity_id(p["id"]) for p in path[:-1]]
        if include_ancestors:
            parent = entity.get("parent")
            if parent is not None and not isinstance(parent, dict):
                raise ValueError("Incomplete inventory parent")
            parent_id = entity_id(parent.get("id")) if parent is not None else None
            if ((parent_id is not None and (not ancestor_ids or ancestor_ids[-1] != parent_id))
                    or (parent_id is None and ancestor_ids)):
                raise ValueError("Inventory parent/path changed or is incomplete; request a fresh read")
        names = [p["name"] for p in path[:-1]]
        location = entity.get("location") or (
            {"id": entity["locationId"]} if entity.get("locationId") else entity.get("parent") or {}
        )
        location_id = entity_id(location["id"]) if location.get("id") else None
        if (location_id and not is_location(entity)
                and (entity.get("locationId") or location_id not in {p["id"] for p in path})):
            names = [p["name"] for p in await self.path(location_id)]
        result = {
            "id": eid, "name": entity["name"], "description": entity.get("description", ""),
            "quantity": entity.get("quantity", 1), "locationId": location_id,
            "parentId": entity_id(entity["parent"]["id"]) if entity.get("parent") else None,
            "locationPath": " / ".join(names), "isLocation": is_location(entity),
            "manufacturer": entity.get("manufacturer", ""), "model": entity.get("modelNumber", ""),
            "serialNumber": entity.get("serialNumber", ""), "notes": entity.get("notes", ""),
            "tags": [tag["name"] for tag in entity.get("tags", [])],
            "updatedAt": entity.get("updatedAt"),
        }
        if include_ancestors:
            result["ancestorIds"] = ancestor_ids
            result["path"] = " / ".join(p["name"] for p in path)
        return result

    async def cards(self, entity_ids: list[str]) -> dict:
        if not isinstance(entity_ids, list) or not 1 <= len(entity_ids) <= 2:
            raise ValueError("Supply one or two entity UUIDs")
        ids = [entity_id(value) for value in entity_ids]
        if len(set(ids)) != len(ids):
            raise ValueError("Supply distinct entity UUIDs")
        return {"items": [await self.card(await self.detail(eid), include_ancestors=True) for eid in ids]}

    async def page(self, *, location: bool, query: str, page: int, page_size: int,
                   location_id: str | None = None) -> tuple[list[dict], int]:
        params = {
            "q": query, "page": page, "pageSize": page_size,
            "isLocation": "true" if location else "false", "includeArchived": "false",
            "orderBy": "name", "orderDirection": "asc",
        }
        if location_id is not None:
            params["parentIds"] = location_id
        result = await self.get("/entities", **params)
        if not isinstance(result, dict) or result.get("complete") is False:
            raise ValueError("Incomplete inventory page")
        items, total = result.get("items"), result.get("total")
        if not isinstance(items, list) or type(total) is not int or total < 0:
            raise ValueError("Inventory page has no authoritative record total")
        expected = min(page_size, max(0, total - (page - 1) * page_size))
        if len(items) != expected:
            raise ValueError("Incomplete inventory page; retry the live view")
        for key, expected_value in (("page", page), ("pageSize", page_size)):
            if key in result and (type(result[key]) is not int or result[key] != expected_value):
                raise ValueError("Inventory returned a different page")
        ids = []
        for item in items:
            if is_location(item) != location:
                raise ValueError("Inventory page contains the wrong record kind")
            ids.append(entity_id(item.get("id")))
        if len(set(ids)) != len(ids):
            raise ValueError("Inventory page contains duplicate records")
        return items, total

    @staticmethod
    def pagination(total: int, page: int, page_size: int) -> dict:
        return {"total": total, "page": page, "pageSize": page_size,
                "hasMore": page * page_size < total, "complete": True}

    async def items_page(self, query: str, location_id: str | None, page: int, page_size: int) -> dict:
        validate_page(query, page, page_size)
        if location_id is not None:
            location_id = entity_id(location_id)
            await self.detail(location_id, location=True)
        entries, total = await self.page(
            location=False, query=query, page=page, page_size=page_size, location_id=location_id,
        )
        items = [await self.card(await self.detail(e["id"], location=False)) for e in entries]
        return {"items": items, **self.pagination(total, page, page_size),
                "filters": {"query": query, "locationId": location_id, "includeArchived": False}}

    async def overview(self, preview_limit: int) -> dict:
        validate_page("", 1, preview_limit)
        entries, item_total = await self.page(location=False, query="", page=1, page_size=preview_limit)
        _, location_total = await self.page(location=True, query="", page=1, page_size=1)
        items = [await self.card(await self.detail(e["id"], location=False)) for e in entries]
        return {
            "items": items, "itemRecordCount": item_total, "locationCount": location_total,
            "includeArchived": False, "complete": True,
        }

    async def destinations_page(self, kind: str, query: str, page: int, page_size: int) -> dict:
        validate_page(query, page, page_size)
        if kind not in {"locations", "items"}:
            raise ValueError("Destination kind must be locations or items")
        location = kind == "locations"
        entries, total = await self.page(location=location, query=query, page=page, page_size=page_size)
        destinations = []
        for entry in entries:
            entity = await self.detail(entry["id"], location=location)
            path = await self.path(entity["id"])
            destinations.append({
                "id": entity["id"], "name": entity["name"],
                "parentId": entity_id(entity["parent"]["id"]) if entity.get("parent") else None,
                "kind": "location" if location else "item", "path": " / ".join(p["name"] for p in path),
            })
        return {"destinations": destinations, **self.pagination(total, page, page_size),
                "filters": {"kind": kind, "query": query, "includeArchived": False}}
