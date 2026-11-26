# Primary-user AI settings

The human selected the AI Inventory Companion and the installation's original
account. A group owner, superuser, or first login is not the installation's
original account.

## HTTP contract

`GET /api/assistant/v1/settings/ai` and `PUT` at the same path require the
existing Companion session. The server revalidates `/users/self` for every
request and compares its UUID with the pinned original-account UUID and the
session actor. An unprovisioned installation denies access. Existing Origin,
CSRF, collection, and context-version checks remain in force.

Session responses add `capabilities: {canManageAISettings: boolean}`.
The Companion shows a separate AI settings tab only when this capability is true.
The capability is a display hint, not authorization.

GET and a successful PUT return:

```json
{
  "revision": 0,
  "chat": {
    "provider": "ollama",
    "baseUrl": "http://ollama:11434",
    "model": "qwen3-vl:2b",
    "keyConfigured": false
  },
  "vision": {
    "provider": "ollama",
    "baseUrl": "http://ollama:11434",
    "model": "qwen3-vl:2b",
    "keyConfigured": false
  }
}
```

Revision zero means the unchanged deployment defaults. PUT submits both roles:

```json
{
  "revision": 0,
  "chat": {
    "provider": "openai-compatible",
    "baseUrl": "https://provider.example/v1",
    "model": "example-model",
    "keyAction": "replace",
    "apiKey": "write-only-example"
  },
  "vision": {
    "provider": "ollama",
    "baseUrl": "http://ollama:11434",
    "model": "qwen3-vl:2b",
    "keyAction": "keep"
  }
}
```

- Providers: `ollama`, `openai-compatible`, `anthropic-messages`, `gemini`.
- Each role requires `provider`, `baseUrl`, `model`, and `keyAction`.
- `keyAction` is `keep`, `replace`, or `clear`.
- Only `replace` accepts and requires `apiKey`. No response returns a key,
  masked key fragment, key hash, or encrypted value.
- `keep` is permitted only for the same provider and normalized complete
  endpoint (scheme, host, effective port, and path). A model change can keep
  the key. An endpoint/provider change must explicitly clear or replace it.
- Roles never inherit keys from each other on Save.
- Field bounds: base URL 2048, model 256, API key 4096 characters. Unknown
  fields, non-string values, and malformed values fail with sanitized 422.
- A stale revision fails with 409 and code `AI_SETTINGS_CHANGED`.
- Unauthorized access fails with 403 and code `AI_SETTINGS_FORBIDDEN`.
- Save validates and atomically persists both roles. It does not contact a
  provider, list models, pull models, run inference, or modify `.env` or secrets.
- The UI keeps key input only in component memory, clears it after Save, and
  does not put settings or keys in browser storage or conversation history.

## Persistence and runtime interface

`AISettings(state, cfg)` captures deployment defaults before runtime injection.
`snapshot()` returns immutable `ProviderRoles`; `public()` returns the GET
shape; `update(body)` returns the new public shape. Each save increments the
revision. The complete role configuration is encrypted with the existing
session-secret cipher in the instance-bound assistant SQLite database.

`create_app` constructs the settings store and injects
`settings_snapshot=store.runtime_snapshot` into `VisionClient`. Conversations
and capture jobs share its operation context. No store pointer is attached to
`Config`; the deployment configuration stays picklable and unchanged. The callback returns the frozen
`AISnapshot(roles: ProviderRoles, chat_token: str | None, vision_token: str | None)`.
Tokens are opaque random role-version markers, never credential hashes. Each
token changes only when that role's effective configuration changes. Defaults
use `None` tokens and retain legacy fingerprints. Runtime code uses the
snapshot when available, otherwise existing defaults.
Each operation pins a consistent snapshot through queueing, retries, model
locking, and provider response validation. A later Save affects new work.
Persisted capture-job configuration must continue to guard against changed
providers. Existing conversation configuration checks remain in force.

The original-account UUID is stored separately from editable provider settings.
Only the trusted local Docker operator helper can provision it. There is no
HTTP enrollment/reassignment route and no automatic login claim. Native data
is read only in a short-lived helper, never mounted into the API. Unique native
creation ordering is required; absent or tied evidence fails closed. Because
native account deletion exists, current oldest-row ordering alone is not proof
that no earlier account was deleted. The operator must establish the original
account's provenance before the helper pins it. A different pinned UUID cannot
be overwritten. Global `--yes` does not provide this attestation.

The API's internal local binding command is
`python -m assistant.primary_binding`. It reads this proof from stdin:

```json
{
  "instanceId": "56cd30b1-7bdd-466e-9459-8b402cd3349c",
  "userId": "e729b2ef-aef7-4d8f-bcb3-88ec1ef1274e",
  "createdAt": "2025-01-01T01:00:00Z",
  "nativeProofVersion": 1,
  "operatorConfirmedOriginal": true
}
```

Only the trusted deployment helper submits this proof after native evidence
checks and explicit operator attestation. The command checks the database's
instance binding and refuses a different existing account pin. It never creates
a database, reads native data, recovers actions, or changes account privileges.
`python -m assistant.primary_binding --show` uses a read-only database connection.
It returns `{bound: false, instanceId}` or
`{bound: true, instanceId, userId, createdAt}`. Invalid or missing state fails
closed. These commands are not HTTP endpoints.

## Validation

Tests and app execution run only in Docker with synthetic credentials and
mock provider transports. No production provisioning or provider request is
part of implementation validation.
