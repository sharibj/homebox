import json
from contextlib import asynccontextmanager

import httpx
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class ServiceError(Exception):
    def __init__(self, code, message, status=503, details=None):
        self.code, self.message, self.status = code, message, status
        self.details = details or {}


class Clients:
    def __init__(self, config):
        self.config = config

    async def homebox(self, method, path, session=None, **kwargs):
        headers = {}
        if session:
            headers = {"Authorization": session["credential"], "X-Tenant": session["collection"]}
        try:
            async with httpx.AsyncClient(
                base_url=self.config.homebox + "/api/v1", headers=headers,
                timeout=30, follow_redirects=False, trust_env=False,
            ) as client:
                response = await client.request(method, path, **kwargs)
            if response.status_code in (401, 403):
                raise ServiceError("homebox_access_denied", "Homebox access was denied. Sign in again or select an accessible collection.", response.status_code)
            if response.status_code == 404:
                raise ServiceError("not_found", "This Homebox record no longer exists.", 404)
            response.raise_for_status()
            return response.json() if response.content else None
        except httpx.HTTPError as exc:
            raise ServiceError("homebox_unavailable", "Homebox is unavailable. No new action will be started.") from exc

    async def tool(self, session, name, arguments=None):
        headers = {
            "Authorization": f"Bearer {self.config.mcp_secret}",
            "X-Homebox-Authorization": session["credential"],
            "X-Tenant": session["collection"],
        }
        try:
            async with httpx2.AsyncClient(headers=headers, timeout=60, trust_env=False) as client:
                async with streamable_http_client(self.config.mcp, http_client=client) as streams:
                    async with ClientSession(streams[0], streams[1]) as mcp:
                        await mcp.initialize()
                        result = await mcp.call_tool(name, arguments or {})
                        if getattr(result, "is_error", False) or getattr(result, "isError", False):
                            raise ServiceError("inventory_tool_failed", "Homebox rejected this operation. Refresh the record and try again.", 502)
                        structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
                        if structured is not None:
                            return structured
                        return json.loads(result.content[0].text)
        except ServiceError:
            raise
        except Exception as exc:
            raise ServiceError("mcp_unavailable", "The inventory service is unavailable. No direct-write fallback was used.") from exc

    async def upload(self, session, entity_id, filename, content):
        # Direct REST is restricted to upload transport and retry reconciliation.
        entity = await self.homebox("GET", f"/entities/{entity_id}", session)
        if any(a.get("title") == filename for a in entity.get("attachments", [])):
            return
        await self.homebox("POST", f"/entities/{entity_id}/attachments", session,
                           data={"name": filename, "type": "photo", "primary": "true"},
                           files={"file": (filename, content, "image/jpeg")})
