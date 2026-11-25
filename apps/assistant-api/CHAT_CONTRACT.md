# Conversational API contract

Additive to CONTRACT.md. Existing actions, captures, exhaustive search, and provider roles remain compatible.

## Scope and context

Session responses add `scope:{instanceId,instanceLabel,collectionId,collectionName,contextVersion}`. `instanceLabel` defaults to a short stable install identifier, not an internal URL. New clients send `X-Context-Version` on scoped requests, alongside `X-Collection-ID`. Missing context-version remains allowed for old clients. A supplied stale context version returns HTTP 409 `COLLECTION_CONTEXT_CHANGED`, including A → B → A. Switching collection or logging out invalidates conversation context. Every chat reply carries this same scope snapshot.

## Requests

`POST /api/assistant/v1/chat` accepts:

```text
{message?, conversationId?, clientTurnId?, expectedRevision?,
 mode?:"normal"|"read_only",
 selection?:{clarificationId,candidateId},
 continuation?:{id}}
```

Exactly one of message, selection, or continuation is required. New clients generate one stable `clientTurnId` before sending and reuse it after a lost response. `conversationId` and `expectedRevision` come from the previous reply; selections and paging require both. Legacy `{message}` starts/resumes the scoped conversation. IDs never confer authorization.

`GET /conversations/current` returns `{conversation:ChatReply|null,expired:boolean}` and recovers the current authorized conversation/action without rerunning a turn. `POST /conversations/reset` with `{}` returns the same envelope, closes dialogue context, and never cancels or forgets a running/uncertain action. Existing action recovery remains available.

Current GET and chat POST use the same scoped session lock. A successful GET is authoritative after any live turn settles; it cannot expose an earlier interactive preview while that turn is claimed. GET waits at most `min(10, ASSISTANT_CHAT_TURN_TIMEOUT_SECONDS)` seconds for the lock and recovery reads, then returns HTTP 503 `conversation_busy` with no conversation snapshot. Once the lock is available, an unfinished persisted turn returns HTTP 409 `chat_turn_interrupted`, also without a conversation snapshot. New unrelated turns cannot skip that interruption.

Neither error certifies readiness, even after a fresh reload with no in-memory request map. Block Confirm and Cancel for all recovered/history actions until recovery succeeds. Retry an in-memory request only with its original stable key and body; a linked saved review is recovered as the same action. If that request cannot be safely replayed, offer an explicit **Restore saved review** control using `POST /conversations/reset`. Reset waits on the same lock, closes the interrupted dialogue, and restores the latest unresolved durable action, including a newer linked action saved before a crash. It never creates another proposal or repeats the stopped request. A reset timeout/failure does not certify readiness. The restored response labels the saved review for a fresh field/status check. No `recoveryRequired` boolean is part of this contract.

## Reply

```text
ChatReply = {
 conversationId,turnId,revision,
 outcome:"answered"|"clarification"|"proposal"|"unsupported",
 scope,message,items:EntityCard[],
 overview?:{itemRecordCount,locationCount,includeArchived:false,complete:true},
 destinations?:Destination[],
 collections?:[{id,name,current}],
 pagination?:{continuationId:string|null,totalRecords,page,pageSize,hasMore,complete},
 clarification?:{id,slot,reason:"missing"|"not_found"|"ambiguous",candidates:Candidate[]},
 pendingOperation?:{id,type,payload,destinationQuery?,destination?,captureId?,actionId?,status},
 action?:Action,
 choices?:{kind,candidates} // legacy display compatibility only
}
Destination = {id,name,path,kind:"location"|"item",parentId?}
Candidate = {id,entityId,label,path,kind:"location"|"item"|"unassigned"}
```

Candidate `id` is a server-issued selection reference, distinct from `entityId`. A selection is valid only for its clarification, conversation revision, actor, browser session, instance, collection, and context version. Names/paths are plain untrusted text, never HTML or instructions. `pendingOperation` survives read-only side questions. A candidate click resumes the same draft and may produce a preview; it never confirms.

`pendingOperation.id` identifies a saved conversation draft, not a durable action or photo. Edits and replacement previews keep that ID once the draft is saved. Fresh, reset, expired, or linked-action recovery can issue a new ID. If an existing draftless conversation discovers an external manual/photo action through `GET /conversations/current`, that read does not save the discovered draft; another read or the next turn can issue a different ID. Do not infer photo ownership from the operation ID alone. A recovered photo draft includes its exact `captureId`; `actionId` identifies the current review and changes when that review is replaced.

The existing photo-association JSON path is `$.pendingOperation.captureId` in a chat reply, or `$.conversation.pendingOperation.captureId` in current/reset envelopes. It is not nested inside `payload`.

When clarification candidates represent the same page, they are the authoritative display/select/View population. Duplicate `destinations` is omitted and duplicate `items` is empty; paging, scope, paths, and counts remain unchanged. Direct destination browsing without clarification still uses `destinations`. A destination side question reissues current choices. A distinct requested read keeps its full result page. If that page and inactive clarification choices would exceed twenty public records, the API suspends the old choices instead: it omits `clarification` and invalidates those selection IDs while retaining `pendingOperation` and any unchanged durable action. It does not slice the requested page, merge totals, or add reads. Explicit **Destinations** or the earlier browse request reissues fresh current-revision choices in a later turn. Smaller combined populations can retain both requested records and current choices.

Only one unresolved write workflow is allowed in chat. Read-only browsing remains available while it awaits confirmation. Editing a still-proposed action invalidates the old action and returns a new full preview; running/uncertain actions require status recovery instead. Typed “yes” cannot confirm. Known empty reads, missing/ambiguous references, unsupported requests, and provider/inventory/auth failures stay distinct.

An explicit Cancel of the linked action retires its conversation draft and pending choices. Current recovery must not expose that cancelled task as a fresh draft, send it as extraction context, or let a later choice recreate it. A new add request starts a new operation from its own message. This differs from internal preview replacement during an edit, which retains the same draft. Cancelling a photo review releases but does not delete its retained capture; choosing to reuse that photo still requires an explicit photo-backed proposal.

The same atomic pending-action guard also covers manual and photo-backed `POST /actions` proposals in both directions. Its ownership scope is actor, browser session, and collection. It blocks proposed/running/uncertain actions and failed actions with a retryable or known partial creation. An unrelated terminal failure does not block. HTTP 409 `write_pending` includes the owned existing `actionId` and `recoveryEndpoint` in error details. It does not cancel either draft or claim/release/delete a blocked photo. Recover the same action instead of proposing again. An explicit edit of a recovered still-proposed photo action preserves its capture when replacing the review.

## MCP read contract

New reads are separate from existing exhaustive `search_items`:

- `inventory_overview(preview_limit=10)` → `{items,itemRecordCount,locationCount,includeArchived:false,complete:true}`.
- `list_items_page(query="",location_id=null,page=1,page_size=20)` → `{items,total,page,pageSize,hasMore,complete:true}`.
- `list_destinations_page(kind="locations",query="",page=1,page_size=20)` → `{destinations,total,page,pageSize,hasMore,complete:true}`. Kind is `"locations"` or `"items"`, never an arbitrary filter/tool. Destinations use the shape above. No exclusion filter is supported by the upstream query; do not silently filter pages or change totals. Self/invalid targets must be rejected by exact-ID validation.
- `get_entity_card(entity_id)` → bounded exact authorized `EntityCard`, with no recursive children expansion.
- `get_entity_cards(entity_ids)` → `{items:EntityCard[]}`, one or two distinct exact UUIDs, sharing a single bounded-read budget. Both new exact-card tools include `ancestorIds`, a validated root-to-parent UUID path excluding self, taken from the same authorized path read. The API rejects self/descendant move targets from these IDs before proposing.

Use active/non-archived entity queries and authoritative totals, including legacy untyped items. Do not use incompatible group-statistics totals. Hydrate only returned pages, with maximum page size 20, and finite upstream request/time budgets. Missing/failed totals must fail, not become zero. Ordinary chat never requests all matching pages. Physical-location contents can use `list_items_page(location_id=...)`; item-container child paging needs a separately verified bounded source, not a hidden whole-inventory scan.

`complete:true` means the requested **page** was fully read and validated. It does not mean all matches fit in that page. `total`, `page`, `pageSize`, and `hasMore` are mandatory on paged reads. Only page 1 with `returnedCount == total` and `hasMore:false` is an exhaustive candidate set. Automatic destination resolution must satisfy that condition across both locations and item containers. A page 2 or final page cannot establish uniqueness. Explicit server-issued candidate selection can proceed after a fresh authorized exact-ID read. An empty page with `total > 0` is not an empty collection or an authoritative not-found result. Local filtering must never reuse an upstream total as an exact filtered count.

Bare typed names/paths are name resolution, not explicit candidate-ID selection. They perform fresh bounded lookup and must satisfy the same exhaustive, cross-kind rule; a label visible once on a partial page cannot select it. Issued candidate IDs and current visible ordinals remain explicit choices.

## Limits and recovery

Defaults: `ASSISTANT_CHAT_TURN_TIMEOUT_SECONDS=60`, `ASSISTANT_CONVERSATION_TTL_SECONDS=1800`, four top-level inventory reads per turn, and twenty cards/candidates per page. One model extraction plus one schema repair remains the maximum. Structured conversation/turn state is encrypted, bounded, session-owned, and short-lived. No SQL transaction remains open during model/MCP I/O. Concurrent/replayed turns serialize per conversation and recheck current state after locking.

The four-call inventory budget includes action-preview validation. A move uses one item page, both destination-kind pages, and one fixed two-card validation batch. If validation would exceed the cap, the draft is retained and the reply requests an explicit “review draft” continuation in a new turn. MCP applies a twenty-second budget, maximum 64 upstream requests, and maximum twenty hydrated cards per tool; the API sixty-second outer budget also includes lock waits, extraction/repair, all reads, and state processing. A conversation accepts at most forty turns and retains at most five continuation controls. Chat provider protocol, endpoint identity, and model form its context fingerprint; a change requires reset before old context can be sent. Vision-only model/configuration changes do not invalidate chat context.

The review-budget response is not inventory pagination: `outcome:"clarification"` with `clarification:{id,slot:"review",reason:"missing",candidates:[]}`, retained `pendingOperation`, and no pagination cursor. A **Review draft** button sends a new stable-key normal-mode message `review draft`, with the current conversation ID and revision. It prepares a newly validated full preview and never confirms. All inventory continuation issuers, including repeated overview requests, share the five-control retention bound.

`clientTurnId` duplicates return the stored reply/action, not a new proposal. Different content with the same key returns 409 `idempotency_conflict`. Stale conversation revision/selection/continuation returns 409 `conversation_changed` or `clarification_expired`. Interruptions recover a durably linked proposal, never blindly create a replacement.

The PWA must also check its operation/context/form-edit generation: late chat or photo results cannot overwrite later manual edits or a reviewed action. Action confirmation retains its existing exact preview, original key, explicit control, and durable recovery.
