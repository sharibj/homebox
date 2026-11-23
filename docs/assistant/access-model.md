# Accounts, collections and offline behavior

## Existing Homebox accounts

The companion uses existing Homebox accounts, not a shared household login or a global service key. Homebox identifies each user. A group owns the inventory. Homebox's UI calls a group a **Collection**; the API calls the selected group a **tenant**.

```text
User ──< membership (owner or user) >── Collection ──< inventory
```

Both directions are many-to-many: a collection can contain several users, and one user can belong to several collections. Ownership applies to each membership. An owner in one collection need not own another.

- Members can read and change the collection's inventory. Homebox has no read-only membership role.
- Owners can also change collection settings, manage invitations, remove members, and delete a collection.
- Local registration without an invitation creates `"<name>'s Home"` and makes the new account its owner. Invited registration joins the existing collection as a member.
- An account has a default collection, but it can select another collection it belongs to. Switching does not combine inventories or grant access to other collections.

Sources: `backend/internal/data/ent/schema/user_group.go`, `backend/internal/data/ent/schema/group.go`, `backend/internal/core/services/service_user.go`, and `backend/app/api/routes.go`.

## Request and credential boundaries

Homebox's tenant middleware uses `X-Tenant`, then the `tenant` query parameter, then the user's default group. It rejects a group that is not in the authenticated user's memberships. The companion sends the authenticated user's credential and selected collection through MCP on each request. A collection ID or opaque NFC tag ID is not authorization.

The browser receives only the companion's opaque session cookie. Production uses HttpOnly, Secure, SameSite cookies and CSRF protection. Homebox session credentials remain encrypted in assistant state. MCP requires its own internal shared secret in addition to the caller's Homebox credential and collection ID. Never configure one shared Homebox API key for all companion users.

Homebox API keys authenticate as their owning user. They are not separately restricted to one collection. Treat them as account credentials, even if an operator normally selects only one collection.

Sources: `backend/app/api/middleware.go:330-397`, `backend/internal/data/ent/schema/api_key.go`, and `apps/assistant-api/CONTRACT.md`.

## Separate browser origins

Use different hostnames for Homebox and the companion, for example `homebox.home.arpa` and `inventory.home.arpa`. They can point to the same server and proxy. This keeps Homebox's existing root service worker out of the companion's scope.

On the companion hostname, the proxy routes only `/api/assistant/v1` and its children to the Assistant API. It does not expose the Homebox API, MCP, or Ollama there. `/t/<tagId>` loads the companion shell, then resolves the tag after authentication.

## Installation identity and NFC

`HOMEBOX_INSTANCE_ID` identifies the Homebox inventory behind this installation. Bootstrap generates it once in the protected deployment configuration. The assistant binds its state to that UUID and rejects a different value. NFC mappings must never be reinterpreted against an unrelated Homebox instance.

Preserve the ID when restoring the same inventory or changing its hostname. Give an unrelated installation a new ID and new assistant state. Back up the ID, both databases, attachments, mappings, and assistant encryption secret together. A stable tag ID does not keep an old sticker URL usable after a hostname change; keep the old hostname working or redirect it.

## Static-shell-only offline policy

In v1, offline support means the installed companion can open its **static shell** and explain that the server is unavailable.

- Cache only versioned application assets and the generic shell.
- Do not service-worker-cache authenticated API responses, photos, captures, chat, inventory, credentials, confirmations, or NFC mappings.
- Do not persist private responses in localStorage, IndexedDB, or a browser upload queue.
- Do not queue or replay offline inventory writes or confirmations.
- When the server returns, refresh the authenticated session and selected collection before requesting data or proposing a write.
- A tag URL reveals no inventory details before sign-in. Offline or unknown tags show a generic message, not a cached household record.
- Model failure is not offline operation. When Ollama is unavailable but the server is reachable, search and manual entry should still work.

This policy concerns the companion. It does not claim to change the existing Homebox frontend's caching behavior. Phone installation, camera, TLS trust, HEIC and physical NFC behavior still require tests on the target devices.
