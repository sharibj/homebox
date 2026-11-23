# Companion API and container contract

This is the integration contract for the separate PWA, Assistant API, MCP adapter, and deployment bundle. Homebox remains the inventory system of record.

## Containers

| Service | Build context / Dockerfile | Listen | Health | Readiness |
| --- | --- | --- | --- | --- |
| Assistant API | `apps/assistant-api` / `Dockerfile` | `0.0.0.0:8000` | `GET /healthz` | `GET /readyz` |
| Homebox MCP adapter | `services/homebox-mcp` / `Dockerfile` | `0.0.0.0:8001` | `GET /healthz` | `GET /readyz` |

Health checks are public and return only service status. Readiness checks return 503 when required local state or Homebox/MCP dependencies are unavailable. Ollama is optional for readiness: normal search and manual entry must remain available without it.

### Assistant API environment

- `HOMEBOX_URL=http://homebox:7745`: private Homebox origin, no `/api/v1` suffix.
- `HOMEBOX_INSTANCE_ID=<UUID>`: required stable install identity, generated once and backed up with Homebox and assistant state. The assistant database binds to this value on its first empty startup and refuses to open on mismatch. Keep it unchanged when moving the same Homebox database to a new hostname. Never reuse it for an unrelated Homebox database. An older nonempty assistant database without a binding fails closed and needs verified migration, not automatic adoption.
- `ASSISTANT_MCP_URL=http://homebox-mcp:8001/mcp`: internal streamable HTTP MCP endpoint.
- `OLLAMA_URL=http://ollama:11434`: private Ollama origin.
- `OLLAMA_MODEL=qwen3-vl:2b`.
- `ASSISTANT_PUBLIC_ORIGIN=https://inventory.example.com`: required exact browser origin; no path or trailing slash.
- `HOMEBOX_PUBLIC_ORIGIN=https://homebox.example.com`: optional links to the existing full UI.
- `ASSISTANT_STATE_PATH=/data/assistant.db`: SQLite orchestration state.
- `ASSISTANT_UPLOAD_DIR=/data/uploads`: temporary normalized photos. Mount one persistent `/data` volume.
- `ASSISTANT_SESSION_SECRET_FILE=/run/secrets/assistant_session_secret`: required secret file with at least 32 random bytes. Used to protect stored Homebox session credentials. Back it up with assistant state.
- `MCP_SHARED_SECRET_FILE=/run/secrets/mcp_shared_secret`: required secret file with at least 32 random bytes, identical in API and MCP containers.
- `ASSISTANT_SESSION_TTL_SECONDS=28800`, `ASSISTANT_ACTION_TTL_SECONDS=900`, `ASSISTANT_CAPTURE_TTL_SECONDS=3600`.
- `ASSISTANT_MAX_UPLOAD_BYTES=12582912`, `ASSISTANT_MAX_IMAGE_PIXELS=24000000`, `ASSISTANT_IMAGE_MAX_EDGE=1024`.
- `ASSISTANT_ALLOW_INSECURE_HTTP=false`: production default. `true` is permitted only with an explicit `http://localhost`, `http://127.0.0.1`, or `http://[::1]` public origin for disposable local tests.

There is no shared Homebox API key or password in deployment configuration. Each user signs in with an existing Homebox account. The API stores the resulting Homebox session credential encrypted server-side. The browser receives only an opaque companion session cookie.

This release supports Homebox's native username/password login. OIDC-only accounts without a local password cannot sign in to this companion yet. Homebox's own OIDC login remains unchanged; this API does not claim to implement SSO.

### MCP environment and authentication

- `HOMEBOX_URL=http://homebox:7745`.
- `MCP_SHARED_SECRET_FILE=/run/secrets/mcp_shared_secret`.
- MCP listens on `/mcp` using stateless streamable HTTP. Do not publish port 8001 outside Docker.
- API requests use `Authorization: Bearer <shared secret>` for internal service authentication, `X-Homebox-Authorization: Bearer <user session>` for the current Homebox user, and `X-Tenant: <collection UUID>` for the explicitly selected collection.
- Request credentials and collection context are isolated per request. Never change a process-global HTTP client's headers.
- MCP offers typed, allowlisted inventory operations with exact UUIDs. It does not expose arbitrary URLs, filesystem paths, deletes, or a generic Homebox HTTP proxy.
- The model sees read tools and proposals only. The API's authenticated confirmation route is the only path to write-tool dispatch.

## Browser authentication and errors

All endpoints below use prefix `/api/assistant/v1`, unless otherwise noted. The reverse proxy sends this prefix unchanged to the API. `/t/<tagId>` is a PWA route.

- Session cookie: `hb_assistant`, HttpOnly, Secure, SameSite=Strict, API path, bounded lifetime.
- `GET /session` returns a `csrfToken`. Send it as `X-CSRF-Token` on all authenticated POST/PATCH/DELETE requests.
- Every tenant-dependent request must also send `X-Collection-ID` equal to the `collectionId` from its current rendered session. This includes reads, captures, chat, actions, mappings, and `POST /session/collection`. That collection-switch route checks the old selected collection in the header and accepts the new one in its body. Missing or mismatched headers fail closed with HTTP 409 and code `COLLECTION_CONTEXT_CHANGED`; discard stale inventory/drafts/actions and call `GET /session` to recover. Login, logout, and `GET /session` are exempt. The backend uses one session snapshot for the request, never a collection guessed by the model.
- Send JSON with `Content-Type: application/json`; send the image form as multipart.
- Login validates the browser `Origin` against `ASSISTANT_PUBLIC_ORIGIN`. All browser mutations enforce the same-origin boundary. No permissive CORS.
- Errors: `{"error":{"code":"machine_readable","message":"Safe user-facing explanation","details":{}}}`.
- No endpoint returns Homebox credentials, model endpoints, or internal filesystem paths.
- All private responses use `Cache-Control: no-store`.

| Method/path | Request | Response |
| --- | --- | --- |
| `POST /auth/login` | `{username,password}` | Session response below; sets cookie |
| `POST /auth/logout` | `{}` | `{ok:true}`; revokes local session and clears cookie |
| `GET /session` | | `{user:{id,email,name},collections:[{id,name}],collectionId,csrfToken}` |
| `POST /session/collection` | `{collectionId}` | Session response; rejects inaccessible collection |

The user owns the Homebox credential, not a single collection. Every operation must use the selected collection. Existing actions and captures remain bound to the collection where they were proposed and cannot run after switching to another collection.

## Inventory, chat, and capture

`EntityCard` is `{id,name,description,locationId,locationPath,isLocation,quantity,manufacturer,model,serialNumber,tags}`. Optional values may be null. `Location` is `{id,name,path,parentId,isLocation:true}`. Entity IDs, collection IDs, and location IDs are Homebox UUIDs.

| Method/path | Request/query | Response |
| --- | --- | --- |
| `GET /locations` | | `{locations:Location[]}` |
| `GET /items` | `q?`, `locationId?` | `{items:EntityCard[],total}` |
| `GET /entities/{id}` | | `EntityCard` plus optional `children` |
| `POST /chat` | `{message}` | `{message,items:EntityCard[],action?:Action,choices?:{kind,candidates:EntityCard[]}}` |
| `POST /captures` | multipart `image`, `hint?`, `locationId?` | `{captureId,draft:Draft,duplicates:EntityCard[],locations:Location[],warnings:string[]}` |

`Draft` is `{name,category,manufacturer,model,serialNumber,quantity,notes,tags:string[]}`. Every field is editable. Category is a suggestion, not a new Homebox taxonomy. A malformed model answer does not mutate inventory. If photo analysis is unavailable or invalid after one retry, capture still returns HTTP 200 with its retained normalized photo, an empty editable draft name, and a clear warning. The user can manually enter fields and confirm the photo-backed create through `/actions`. Search and manual entry do not require Ollama.

The chat response can ask for clarification and show candidates, but never executes a write. Read results refer to actual Homebox records. `action`, when present, is an unexecuted confirmation proposal.

Common search/location/move sentences have a deterministic offline fast path. Other wording uses the local Ollama model to extract a strict, validated intent (search, contents, move, edit, tags, item/location creation). The server resolves actual records and sends writes through the same proposal/confirmation path. Model output cannot supply authority, credentials, arbitrary tool calls, or bypass confirmation. If intent extraction is unavailable or invalid, use direct search/manual forms; this is not an unrestricted autonomous agent or an inventory-accuracy guarantee.

## Proposed and confirmed actions

`Action` is:

```json
{
  "id": "opaque-action-id",
  "type": "item.move",
  "revision": 1,
  "status": "proposed",
  "summary": "Move HDMI cable to Garage / Electronics",
  "payload": {"entityId": "uuid", "locationId": "uuid"},
  "before": {},
  "after": {},
  "result": null,
  "error": null
}
```

Status is one of `proposed`, `running`, `succeeded`, `failed`, `uncertain`, `cancelled`. `retryable` is a boolean, false unless the backend has durably saved the created entity UUID and completed metadata enrichment, so only attachment upload/reconciliation or final result retrieval remains. For `failed` plus `retryable:true`, retry the same confirmation with the same action ID, revision, and `Idempotency-Key`; do not propose a new create. `uncertain` is never retryable. A saved partial entity may appear in `result` even when the action failed. Bind each proposal to its creator, session, selected collection, exact validated payload and IDs, expiry, and prior state. A stale revision or changed target requires a new proposal.

`after` always includes the full reviewed payload, an optional readable `destination` entity, and `photoIncluded:boolean`. A destination must never replace the fields being reviewed. The PWA must show non-default quantity, metadata, notes, tags, and photo inclusion before confirmation.

| Method/path | Request | Response |
| --- | --- | --- |
| `POST /actions` | `{type,payload,captureId?}` | `Action` |
| `GET /actions/{id}` | | `Action` |
| `GET /actions` | | `{actions:Action[]}`; newest 50 for current actor, browser session, and selected collection, including proposed/running/failed/uncertain and recent succeeded/cancelled |
| `POST /actions/{id}/confirm` | `{revision}` and `Idempotency-Key` header | `Action` |
| `POST /actions/{id}/cancel` | `{}` | `Action` |

Payloads:

- `item.create`: `{name,locationId?,quantity?,description?,manufacturer?,model?,serialNumber?,notes?,tags?:string[],category?}`. Optional `captureId` is top-level in the action request. The photo stays attached to the action and cannot be replaced during confirmation.
- `item.move`: `{entityId,locationId}`. A destination may be a location or item-container UUID. The API rejects cycles and ambiguous text resolution.
- `item.update`: `{entityId,name?,description?,manufacturer?,model?,serialNumber?,notes?,quantity?}`.
- `item.tags`: `{entityId,tags:string[]}`. These are the reviewed replacement tags.
- `location.create`: `{name,parentId?,description?}`.

Action creation, not just confirmation, validates fields and checks access. Confirming an action rechecks current Homebox access and relevant prior state. Audit records retain actor, selected collection, action, entity IDs, before/after, confirmation and execution timestamps, and outcome. Duplicate confirmations return the stored result. A partly completed create/photo operation resumes the attachment, not item creation. An ambiguous upstream write timeout returns `uncertain`, never blindly creates again.

Mutations from this API serialize per collection using kernel file locks on the same local persistent state volume, across workers/processes. The prior-state check runs inside that lock, so two different proposals from the same old entity state cannot both overwrite it. This does not provide atomicity against independent Homebox UI/API clients.

Every `Action` includes `confirmationKey`, initially `${id}:${revision}` and then the original submitted key. Use it as `Idempotency-Key`. On page reload or a lost response, fetch `GET /actions` before offering a fresh create, show unresolved proposals and recent results, and reuse the returned key. Do not persist private action payloads in localStorage. Cancelling a still-proposed create atomically releases its capture so the same photo can be edited and proposed again. Running or uncertain actions never release a capture.

## NFC mappings

| Method/path | Request | Response |
| --- | --- | --- |
| `GET /tags/mappings` | | `{mappings:Mapping[]}` |
| `POST /tags/mappings` | `{targetType:"item"|"location",entityId,label?}` | `Mapping` |
| `PATCH /tags/mappings/{tagId}` | `{entityId?,targetType?,status?:"active"|"revoked",label?}` | `Mapping` |
| `GET /tags/resolve/{tagId}` | | `{mapping:Mapping,entity:EntityCard}` |

`Mapping` is `{tagId,targetType,entityId,label,status,url,createdAt,updatedAt}`. `url` is the stable `${ASSISTANT_PUBLIC_ORIGIN}/t/${tagId}`. Mappings are collection-scoped. Unknown, inaccessible, and revoked tags do not leak inventory details. Mapping creation/reassignment/revocation is a direct, explicit PWA form action protected by authentication and CSRF, not a model-selected write. The PWA renders a QR code for the same URL and preserves the tag landing route across login.

## Validation and test entry points

Each service has its own Dockerfile and locked Python requirements. Container tests run with:

```sh
docker build -t homebox-assistant-test apps/assistant-api
docker run --rm homebox-assistant-test python -m pytest
docker build -t homebox-mcp-test services/homebox-mcp
docker run --rm homebox-mcp-test python -m pytest
```

Live integration tests use an explicitly configured disposable Homebox and MCP service on an isolated Docker network. They must not use a user's inventory, install runtimes on the host, or pull model weights.
