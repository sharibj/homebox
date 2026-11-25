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
- `AI_PROVIDER=ollama`: deployment-selected `ollama`, `openai-compatible`, `anthropic-messages`, or `gemini`; never chosen by model/user input. Native Ollama remains the default. `AI_BASE_URL` and `AI_MODEL` override legacy `OLLAMA_URL`/`OLLAMA_MODEL` for Ollama.
- `AI_CHAT_PROVIDER`, `AI_CHAT_BASE_URL`, `AI_CHAT_MODEL`, `AI_CHAT_API_KEY_FILE` configure intent/chat independently. `AI_VISION_PROVIDER`, `AI_VISION_BASE_URL`, `AI_VISION_MODEL`, `AI_VISION_API_KEY_FILE` configure photos independently. Shared `AI_PROVIDER`, `AI_BASE_URL`, `AI_MODEL`, `AI_API_KEY_FILE` are optional defaults. A role model overrides an explicit shared model, then uses the selected provider's own fallback: `OLLAMA_MODEL` for Ollama or `gemini-3.1-flash-lite` for Gemini. Role URLs inherit a shared URL only for the same provider kind. Ollama falls back to `OLLAMA_URL`; Gemini falls back to `https://generativelanguage.googleapis.com/v1beta`. Injected legacy `OLLAMA_*` defaults never become Gemini settings.
- A shared API key is inherited only for the same provider kind and canonical complete base URL, including the path. A role-specific key file overrides it; an explicitly selected empty file means anonymous access. Unused shared secrets are not read. No AI secret file or host path is assumed by default.
- OpenAI-compatible and Anthropic roles require a resolved explicit base URL and model. Fully specified independent roles do not require a shared model or endpoint. Keys are server-side only. URLs cannot contain credentials, query, or fragment. TLS is verified, redirects and proxy-env inheritance are disabled, and no automatic external fallback occurs. Intent requests never contain photo bytes. Equal complete provider settings reuse one client, while inference resource locks use provider kind and canonical endpoint, excluding model and key: one endpoint serializes chat/photo across workers; independent endpoints can run concurrently.
- OpenAI-compatible means Chat Completions with vision image data URLs, JSON-object response mode, temperature zero, and `max_completion_tokens`. The configured model/endpoint must support these fields. Native Anthropic Messages uses the configured base URL plus `/v1/messages`, `x-api-key` when configured, and `anthropic-version: 2023-06-01`. Only Anthropic accepts a single complete whole-response JSON code fence; it never permits extra prose or invalid fields.
- Native Ollama uses `/api/chat`, `stream=false`, the full JSON schema in `format`, and normalized JPEG bytes as base64 in the user message's `images` array. Text intent contains no image. The same native path accepts the configured model name `minicpm-v4.6` for both roles, without changing neutral defaults or adding a model-specific adapter. Requests retain temperature zero, `num_ctx=4096`, `keep_alive=5m`, and the 600-token chat / 1,200-token photo caps. Strict local validation and one bounded repair still apply; the endpoint must support schema-valued `format`. See the opt-in [host Ollama profile](../../deploy/examples/ollama-minicpm-host.env) and [deployment guide](../../docs/assistant/deployment.md#host-ollama-with-minicpm-v46-for-chat-and-vision) for role selection, key clearing, routing, and timeout settings.
- Native Gemini uses the configured versioned API base plus `/models/{model}:generateContent`, `x-goog-api-key` when configured, text-only `systemInstruction`, user `contents`, and normalized JPEG `inlineData`. Models must be identifiers, not URLs; an optional `models/` prefix is removed. A custom native gateway base keeps its full path; the adapter never adds a second version prefix. Google requires an API key; no raw-key environment variable or implicit Google SDK credential is read. Configure a protected server-side `*_API_KEY_FILE`. Gemini sends the full Pydantic schema through `generationConfig.responseJsonSchema` with `responseMimeType=application/json`. Local validation remains authoritative, including string bounds that Google may ignore.
- Gemini preserves the caller's 600-token text and 1,200-token photo caps and remaining timeout. It uses model-default temperature and thinking settings; the recommended `gemini-3.1-flash-lite` defaults to minimal thinking. Responses are limited to 1 MiB of decoded data and require one completed, non-blocked `STOP` candidate containing only final text. Thought, function, code, media, malformed, and truncated output is rejected. Usage and thought signatures are not returned or used as authority. Strict local JSON/schema validation and one bounded repair apply to every provider. Transport, authentication, and quota errors fail safely without an automatic retry or provider switch.
- `ASSISTANT_PUBLIC_ORIGIN=https://inventory.example.com`: required exact browser origin; no path or trailing slash.
- `HOMEBOX_PUBLIC_ORIGIN=https://homebox.example.com`: optional links to the existing full UI.
- `ASSISTANT_STATE_PATH=/data/assistant.db`: SQLite orchestration state.
- `ASSISTANT_UPLOAD_DIR=/data/uploads`: temporary normalized photos. Mount one persistent `/data` volume.
- `ASSISTANT_SESSION_SECRET_FILE=/run/secrets/assistant_session_secret`: required secret file with at least 32 random bytes. Used to protect stored Homebox session credentials. Back it up with assistant state.
- `MCP_SHARED_SECRET_FILE=/run/secrets/mcp_shared_secret`: required secret file with at least 32 random bytes, identical in API and MCP containers.
- `ASSISTANT_SESSION_TTL_SECONDS=28800`, `ASSISTANT_ACTION_TTL_SECONDS=900`, `ASSISTANT_CAPTURE_TTL_SECONDS=3600`.
- `ASSISTANT_MAX_UPLOAD_BYTES=12582912`, `ASSISTANT_MAX_IMAGE_PIXELS=24000000`, `ASSISTANT_IMAGE_MAX_EDGE=1024`.
- `ASSISTANT_ANALYSIS_QUEUE_LIMIT=8`: maximum waiting photo jobs, shared across workers on the state volume.
- `ASSISTANT_ANALYSIS_QUEUE_TIMEOUT_SECONDS=120`: maximum wait for the model slot before returning a retained manual draft.
- `ASSISTANT_ANALYSIS_TIMEOUT_SECONDS=900`: total running photo-job budget, including both schema attempts and result enrichment, not a fresh timeout for each retry.
- `ASSISTANT_CAPTURE_SYNC_TIMEOUT_SECONDS=90`: bounded legacy `/captures` analysis/queue budget, capped at 120 seconds to leave headroom below the old 180-second browser request limit.
- `ASSISTANT_CHAT_TURN_TIMEOUT_SECONDS=60`: whole chat-request deadline, including authentication, locks, interpretation/repair, and inventory reads.
- `ASSISTANT_CONVERSATION_TTL_SECONDS=1800`: encrypted, scoped conversation idle lifetime.
- `ASSISTANT_INSTANCE_LABEL`: optional human-readable install label, otherwise a short stable install identifier.
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
| `GET /session` | | `{user:{id,email,name},collections:[{id,name}],collectionId,csrfToken,ai:{chat:{provider,model},vision:{provider,model}}}`; login/collection-switch return the same metadata, never a base URL or key. Provider kind alone does not imply a local endpoint. |
| `POST /session/collection` | `{collectionId}` | Session response; rejects inaccessible collection |

The user owns the Homebox credential, not a single collection. Every operation must use the selected collection. Existing actions and captures remain bound to the collection where they were proposed and cannot run after switching to another collection.

## Inventory, chat, and capture

`EntityCard` is `{id,name,description,locationId,locationPath,isLocation,quantity,manufacturer,model,serialNumber,tags}`. Optional values may be null. `Location` is `{id,name,path,parentId,isLocation:true}`. Entity IDs, collection IDs, and location IDs are Homebox UUIDs.

| Method/path | Request/query | Response |
| --- | --- | --- |
| `GET /locations` | | `{locations:Location[]}` |
| `GET /items` | `q?`, `locationId?` | `{items:EntityCard[],total}` |
| `GET /entities/{id}` | | `EntityCard` plus optional `children` |
| `POST /chat` | Legacy `{message}` or structured turn/selection/continuation | Scoped `ChatReply`; complete additive contract in [CHAT_CONTRACT.md](CHAT_CONTRACT.md) |
| `GET /conversations/current` | | `{conversation:ChatReply|null,expired:boolean}` |
| `POST /conversations/reset` | `{}` | Same recovery envelope; unresolved actions remain recoverable |
| `POST /captures` | multipart `image`, `hint?`, `locationId?` | `{captureId,draft:Draft,duplicates:EntityCard[],locations:Location[],warnings:string[]}` |
| `POST /capture-jobs` | same multipart fields; required stable `Idempotency-Key` | HTTP 202 `CaptureJob`; returns after validation, normalization, and durable photo/job storage, not inference |
| `GET /capture-jobs/{id}` | | Current owned `CaptureJob` |
| `GET /capture-jobs` | optional `submissionKey` | `{jobs:CaptureJob[]}`; newest 20 for the actor, browser session, instance, and selected collection, or matching submission key |

### Durable asynchronous photo analysis

New clients use `/capture-jobs`. All three routes enforce the same authentication and `X-Collection-ID` boundary as inventory requests; submission also requires Origin and CSRF. A job never creates or updates inventory.

`CaptureJob` is `{id,status,submissionKey,captureId,createdAt,startedAt,finishedAt,expiresAt,result,error,warningCodes}`. Timestamps are ISO strings or null. Status is `queued`, `running`, `succeeded`, `manual`, `failed`, or `expired`. For `succeeded` or `manual`, `result` is the unchanged `/captures` draft response, including the retained `captureId`. For `manual`, the draft name is blank and `result.warnings` explains why inference did not finish. `failed` has a safe `{code,message}` error, such as invalidated access; it must not silently bypass an authentication failure. Expired jobs no longer offer a photo-backed draft.

Terminal results are recoverable only while their owned retained capture and file remain available and unexpired. A missing, consumed, expired, or wrong-scope capture projects the existing response as `status:"expired"`, `result:null`, `error:{code:"capture_expired",message:"This photo is no longer available. Take a new photo."}`, and `warningCodes:[]`. IDs and timestamps remain unchanged. This read-only projection covers single-job, exact-key/list recovery, and idempotent submission replay; it does not modify rows or run inference again. New chat/reset cannot resurrect a consumed photo. A valid unclaimed capture, or one retained for a partial/retryable action, remains recoverable across reset.

The browser creates one random submission key before upload, sends it as `Idempotency-Key`, and keeps only the opaque key/job ID for reload recovery. After a lost submission response, query `/capture-jobs?submissionKey=<key>` before attempting any new upload. Reposting the same key and same normalized image/hint/location returns the same job; different content with that key fails HTTP 409 `idempotency_conflict`. Keys are not authorization. Do not save image bytes, hints, model drafts, or credentials in localStorage.

Poll the owned job every 1–2 seconds while `queued`/`running`, using a short independent HTTP request. Stop polling on any terminal state, logout, collection change, or authentication error. On reload, recover owned jobs before offering a blind replacement upload. `GET /capture-jobs` is bounded to 20 results and accepts `submissionKey` to find the exact uncertain submission.

The persistent local SQLite store binds the job to actor, browser session, selected collection, and `HOMEBOX_INSTANCE_ID`. It snapshots the selected provider, a SHA-256 fingerprint of its base URL, and the vision model, never the API key. A queued job whose snapshot differs after a configuration change becomes a retained manual draft with `analysis_configuration_changed`; an old photo is never silently sent to a new endpoint or model. Shared-volume kernel locking permits only one photo analysis across API workers. Queue capacity and queue/running deadlines are finite. Full queues, elapsed queue/model deadlines, model errors, and interrupted in-flight jobs retain the photo and produce an explicit manual draft, with codes such as `analysis_queue_full`, `analysis_queue_timed_out`, `analysis_timed_out`, or `analysis_interrupted`. Restart recovery re-reads the current row after obtaining the model lock and never repeats a possibly completed inference automatically. A queued job can resume after restart if still valid. No result is an inventory mutation; saving still requires the existing reviewed action and confirmation.

Legacy `/captures` retains its synchronous response shape. Its queue and model work share one bounded total budget measured from request entry, then return the retained manual draft with a specific warning before the old browser deadline. An outer 150-second deadline also covers authentication, multipart upload, destination validation, and normalization. If that deadline expires before a photo can be validated and stored, the API returns a safe HTTP 504 `capture_request_timed_out`, not an unauthorized manual draft or a fabricated capture ID. This is a compatibility fallback, not the primary new-client path. Image edge 1024, context 4096, prediction limit 1200, temperature zero, and strict output schema stay unchanged.

`Draft` is `{name,category,manufacturer,model,serialNumber,quantity,notes,tags:string[]}`. Every field is editable. Category is a suggestion, not a new Homebox taxonomy. A malformed model answer does not mutate inventory. If photo analysis is unavailable or invalid after one retry, capture still returns HTTP 200 with its retained normalized photo, an empty editable draft name, and a clear warning. The user can manually enter fields and confirm the photo-backed create through `/actions`. Search and manual entry do not require Ollama.

The chat response can ask for clarification and show candidates, but never executes a write. Read results refer to actual Homebox records. `action`, when present, is an unexecuted confirmation proposal.

See [CHAT_CONTRACT.md](CHAT_CONTRACT.md) for conversation scope generations, replay, bounded reads, page semantics, selection references, immutable preview edits, and recovery. Session replies also include `scope`; new clients send `X-Context-Version` alongside `X-Collection-ID`. Destination side questions preserve the pending operation and issue fresh choices. A distinct requested read keeps its full page; if inactive choices would push the reply above twenty public records, the API suspends and invalidates those choices without dropping the draft or unchanged durable action. Explicit destination/earlier-slot browsing resumes fresh choices. Typed “yes” does not confirm. Provider/inventory failures are errors, never whole-sentence fallback searches or fabricated empty results.

Current-conversation recovery serializes with chat turns. HTTP 503 `conversation_busy` or HTTP 409 `chat_turn_interrupted` contains no conversation snapshot and must keep all confirmation/cancellation controls blocked, including after reload. An explicit reset restores the latest saved durable review without rerunning the stopped request. Chat, manual, and photo-backed proposals share one atomic scoped pending-action guard; a blocked `write_pending` response leaves the submitted draft/photo intact.

Explicitly cancelling the linked action also retires its chat draft and choices. It does not turn the cancelled payload back into a pending operation for the next add request. Internal review replacement during an edit still preserves the draft, and a cancelled photo remains available for explicit reuse while its capture is valid.

Common search/location/move sentences have a deterministic offline fast path. Other wording uses the configured chat model, Ollama by default, to extract a strict, validated intent (search, contents, move, edit, tags, item/location creation). The server resolves actual records and sends writes through the same proposal/confirmation path. Model output cannot supply authority, credentials, arbitrary tool calls, or bypass confirmation. If intent extraction is unavailable or invalid, use direct search/manual forms; this is not an unrestricted autonomous agent or an inventory-accuracy guarantee.

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
