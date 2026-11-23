import contextvars
import hmac
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import homebox_mcp as upstream
import httpx
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


def secret() -> str:
    value = Path(os.environ["MCP_SHARED_SECRET_FILE"]).read_text().strip()
    if len(value) < 32:
        raise RuntimeError("MCP shared secret must contain at least 32 characters")
    return value


HOMEBOX = os.environ.get("HOMEBOX_URL", "http://homebox:7745").rstrip("/")
SECRET = secret()
current_client: contextvars.ContextVar[httpx.Client] = contextvars.ContextVar("homebox_client")


class ScopedClient:
    """Reuse upstream REST primitives without sharing credentials between users."""

    def __getattr__(self, name: str) -> Any:
        return getattr(current_client.get(), name)


upstream._client.close()
upstream._client = ScopedClient()
mcp = MCPServer("homebox-companion", version="1.0.0")
READ = ToolAnnotations(read_only_hint=True)


@contextmanager
def homebox_scope(token: str, tenant: str):
    with httpx.Client(
        base_url=f"{HOMEBOX}/api/v1",
        headers={"Authorization": token, "X-Tenant": str(UUID(tenant))},
        timeout=30,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        context = current_client.set(client)
        try:
            yield
        finally:
            current_client.reset(context)


def uid(value: str) -> str:
    return str(UUID(value))


def card(entity: dict) -> dict:
    eid = entity["id"]
    path = upstream._get(f"/entities/{eid}/path") or []
    names = [part.get("name", "") for part in path if part.get("id") != eid]
    location = entity.get("location") or (
        {"id": entity["locationId"]} if entity.get("locationId") else entity.get("parent") or {}
    )
    if (location.get("id") and not (entity.get("entityType") or {}).get("isLocation")
            and (entity.get("locationId") or location["id"] not in {part.get("id") for part in path})):
        physical_path = upstream._get(f"/entities/{location['id']}/path") or []
        names = [part.get("name", "") for part in physical_path]
    return {
        "id": eid,
        "name": entity.get("name", ""),
        "description": entity.get("description", ""),
        "quantity": entity.get("quantity", 1),
        "locationId": location.get("id"),
        "parentId": (entity.get("parent") or {}).get("id"),
        "locationPath": " / ".join(names),
        "isLocation": bool((entity.get("entityType") or {}).get("isLocation")),
        "manufacturer": entity.get("manufacturer", ""),
        "model": entity.get("modelNumber", ""),
        "serialNumber": entity.get("serialNumber", ""),
        "notes": entity.get("notes", ""),
        "tags": [tag["name"] for tag in entity.get("tags", [])],
        "updatedAt": entity.get("updatedAt"),
    }


def detail(entity_id: str) -> dict:
    return upstream._get(f"/entities/{uid(entity_id)}")


def all_entities(**params) -> list[dict]:
    result = []
    for page in range(1, 501):
        data = upstream._get("/entities", page=page, pageSize=200, **params)
        batch = data.get("items", [])
        result.extend(batch)
        if len(result) >= data.get("total", 0) or not batch:
            return result
    raise ValueError("Inventory exceeds the supported query size; narrow the search")


@mcp.tool(annotations=READ)
def get_entity(entity_id: str) -> dict:
    """Fetch an accessible entity by exact Homebox UUID, never fuzzy matching."""
    value = detail(entity_id)
    output = card(value)
    if output["isLocation"]:
        children = all_entities(parentIds=uid(entity_id))
        children += [e for e in all_entities(isLocation="true")
                     if (e.get("parent") or {}).get("id") == uid(entity_id)]
        output["children"] = [card(detail(e["id"])) for e in children]
    return output


@mcp.tool(annotations=READ)
def search_items(query: str = "", location_id: str | None = None) -> dict:
    """Search current collection with complete pagination and grounded paths."""
    params: dict[str, Any] = {"q": query[:500]}
    if location_id:
        detail(location_id)
        params["parentIds"] = uid(location_id)
    entities = all_entities(**params)
    return {"items": [card(detail(e["id"])) for e in entities], "total": len(entities)}


@mcp.tool(annotations=READ)
def list_locations() -> dict:
    """List all location UUIDs and their complete paths in the current collection."""
    values = []
    for entry in all_entities(isLocation="true"):
        entity = detail(entry["id"])
        result = card(entity)
        values.append({
            "id": result["id"], "name": result["name"], "parentId": result["parentId"],
            "isLocation": True,
            "path": " / ".join(filter(None, [result["locationPath"], result["name"]])),
        })
    return {"locations": values}


@mcp.tool(annotations=READ)
def location_contents(location_id: str) -> dict:
    """Return items at an exact accessible location or item-container UUID."""
    destination = detail(location_id)
    if (destination.get("entityType") or {}).get("isLocation"):
        return search_items(location_id=location_id)
    values = [
        card(detail(e["id"])) for e in all_entities()
        if (e.get("parent") or {}).get("id") == uid(location_id)
    ]
    return {"items": values, "total": len(values)}


class CreateItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=255)
    locationId: str | None = None
    quantity: float = Field(default=1, gt=0, le=1_000_000)
    description: str = Field(default="", max_length=1000)
    manufacturer: str = Field(default="", max_length=255)
    model: str = Field(default="", max_length=255)


@mcp.tool()
def create_item(payload: CreateItem) -> dict:
    """Create the basic record in one request. Caller journals its UUID before enrichment."""
    body = {
        "name": payload.name, "quantity": payload.quantity, "description": payload.description,
        "manufacturer": payload.manufacturer, "modelNumber": payload.model,
    }
    if payload.locationId:
        destination = detail(payload.locationId)
        body["parentId"] = destination["id"]
    return upstream._post("/entities", body)


@mcp.tool()
def move_item(entity_id: str, destination_id: str) -> dict:
    """Move one item to an exact UUID. Does not overwrite unrelated metadata."""
    entity = detail(entity_id)
    if (entity.get("entityType") or {}).get("isLocation"):
        raise ValueError("Use the Homebox UI to move a location")
    destination = detail(destination_id)
    visited = set()
    cursor = destination
    while cursor:
        if cursor["id"] == entity["id"] or cursor["id"] in visited:
            raise ValueError("An item cannot be moved into itself or its descendants")
        visited.add(cursor["id"])
        parent = cursor.get("parent")
        cursor = detail(parent["id"]) if parent else None
    upstream._patch(f"/entities/{entity['id']}", {"parentId": destination["id"]})
    return card(detail(entity["id"]))


class ItemChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=255)
    quantity: float | None = Field(default=None, gt=0, le=1_000_000)
    description: str | None = Field(default=None, max_length=1000)
    manufacturer: str | None = Field(default=None, max_length=255)
    model: str | None = Field(default=None, max_length=255)
    serialNumber: str | None = Field(default=None, max_length=255)
    notes: str | None = Field(default=None, max_length=1000)


@mcp.tool()
def update_item(entity_id: str, changes: ItemChanges) -> dict:
    """Apply reviewed fields while preserving every unrelated Homebox field."""
    full = detail(entity_id)
    body = upstream._preserve_item_body(full)
    # Upstream v1.5.1 and main omit this nonoptional PUT field. Omitting it
    # resets the container's child-location policy in current Homebox.
    body["syncChildEntityLocations"] = full.get("syncChildEntityLocations", False)
    if full.get("locationId"):
        body["locationId"] = full["locationId"]
    for key, value in changes.model_dump(exclude_none=True).items():
        body["modelNumber" if key == "model" else key] = value
    upstream._put(f"/entities/{full['id']}", body)
    return card(detail(full["id"]))


@mcp.tool()
def set_tags(entity_id: str, tags: list[str]) -> dict:
    """Replace an item's tags with the explicitly reviewed names."""
    entity = detail(entity_id)
    if len(tags) > 30 or any(not t.strip() or len(t) > 100 for t in tags):
        raise ValueError("Supply at most 30 nonempty tag names, each at most 100 characters")
    existing = {t["name"].casefold(): t["id"] for t in upstream._get("/tags") or []}
    ids = []
    for name in dict.fromkeys(t.strip() for t in tags):
        key = name.casefold()
        if key not in existing:
            existing[key] = upstream._post("/tags", {"name": name})["id"]
        ids.append(existing[key])
    upstream._patch(f"/entities/{entity['id']}", {"tagIds": ids})
    return card(detail(entity["id"]))


@mcp.tool()
def create_location(name: str, parent_id: str | None = None, description: str = "") -> dict:
    """Create a location with an existing location type, without modifying the taxonomy."""
    if not name.strip() or len(name) > 255 or len(description) > 1000:
        raise ValueError("Invalid location name or description")
    types = upstream._get("/entity-types") or []
    location_type = next((t["id"] for t in types if t.get("isLocation")), None)
    if not location_type:
        raise ValueError("Create a location entity type in Homebox first")
    body = {"name": name.strip(), "description": description, "entityTypeId": location_type}
    if parent_id:
        parent = detail(parent_id)
        if not (parent.get("entityType") or {}).get("isLocation"):
            raise ValueError("A location parent must be a location")
        body["parentId"] = parent["id"]
    return upstream._post("/entities", body)


async def health(request: Request):
    return JSONResponse({"status": "ok"})


async def ready(request: Request):
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=3) as client:
            response = await client.get(f"{HOMEBOX}/api/v1/status")
            response.raise_for_status()
        return JSONResponse({"status": "ready"})
    except httpx.HTTPError:
        return JSONResponse({"status": "unavailable"}, status_code=503)


inner = mcp.streamable_http_app(
    stateless_http=True, json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)
inner.routes.extend([Route("/healthz", health), Route("/readyz", ready)])


class Authenticate:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in {"/healthz", "/readyz"}:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        supplied = headers.get(b"authorization", b"").decode()
        token = headers.get(b"x-homebox-authorization", b"").decode()
        tenant = headers.get(b"x-tenant", b"").decode()
        if (not hmac.compare_digest(supplied, f"Bearer {SECRET}") or
                not token.startswith("Bearer ") or len(token) > 4096 or b"origin" in headers):
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
        try:
            tenant = uid(tenant)
        except ValueError:
            return await JSONResponse({"error": "invalid collection"}, status_code=400)(scope, receive, send)
        with homebox_scope(token, tenant):
            await self.app(scope, receive, send)


app = Authenticate(inner)
