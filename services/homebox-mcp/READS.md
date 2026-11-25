# Bounded inventory reads

These five additive tools use the authenticated MCP request's Homebox user credential and selected collection. They do not query other collections, call the group statistics route, or access the Homebox database. The original nine tools remain compatible, including exhaustive `search_items` for photo duplicate checks.

## Signatures

```text
inventory_overview(preview_limit=10)
  -> {items:EntityCard[],itemRecordCount,locationCount,includeArchived:false,complete:true}

list_items_page(query="",location_id=null,page=1,page_size=20)
  -> {items:EntityCard[],total,page,pageSize,hasMore,complete:true,
      filters:{query,locationId,includeArchived:false}}

list_destinations_page(kind="locations",query="",page=1,page_size=20)
  -> {destinations:Destination[],total,page,pageSize,hasMore,complete:true,
      filters:{kind,query,includeArchived:false}}

get_entity_card(entity_id)
  -> EntityCard + {ancestorIds:UUID[],path:string} // no recursive children expansion

get_entity_cards(entity_ids:[UUID]) // one or two distinct IDs, preserving input order
  -> {items:[EntityCard + {ancestorIds:UUID[],path:string}]}

Destination = {id,name,path,kind:"location"|"item",parentId}
```

Destination input `kind` accepts only `"locations"` or `"items"`. All active non-location records are permitted item-container candidates, including legacy untyped items and items without children. A candidate is not automatically a valid target for a particular mutation: the API must reauthorize the exact selection and reject self/descendant moves through the existing action rules.

The exact-card tools include `ancestorIds` in root-to-parent order, excluding self. These IDs come from the logical parent path already fetched for the card, not its possibly different physical-location path. Every path UUID, final self ID, duplicate/cycle condition, and immediate parent/path agreement is checked. Missing or unusable paths fail; `[]` means a validated root, never a fallback for missing ancestry. The legacy `get_entity` response is unchanged.

The same validated path also supplies the exact-card `path` display string, including the current entity, joined root-to-entity with ` / `. It preserves nested containment for destination previews even when `locationPath` points to a different physical location. Root `path` is its own name. This adds no HTTP request and does not change physical `locationPath` semantics.

`get_entity_cards` shares one authenticated client, deadline, and request budget across the whole one/two-card batch. All UUID inputs validate before HTTP. Duplicate UUIDs are rejected after normalization, including alternate case, compact, braced, and URN forms of the same ID. Any inaccessible, missing, archived, or incomplete card fails the entire result; it never returns a partial source/destination pair. This lets the API validate a move's source and full destination ancestry in one top-level MCP read, within the existing four-read chat-turn budget.

Homebox's public entity query has no exact exclusion/descendant filter. There is deliberately **no `exclude_entity_id` argument**. Filtering only a returned page and reusing the upstream total would invent an authoritative filtered count.

`location_id` means an exact active **location** UUID and uses Homebox's effective physical-location filter. Item-container UUIDs are rejected rather than silently applying the wrong parent semantics. Direct item-container child paging is not provided.

## Count and paging semantics

- `itemRecordCount`, `locationCount`, and page `total` come from query-wide entity-list totals before pagination, with `includeArchived=false`.
- Item records include untyped non-location entities. Counts are records, not summed quantities. Overview totals and browse filters use the same archival/type policy.
- Queries use Homebox's real search semantics. They are not local exact-name filters: a name, description, or other supported field may match.
- **`complete=true` means the requested page was fully read and validated. It does not mean all matches fit on that page.** It is normal to have both `complete=true` and `hasMore=true`.
- A complete candidate set exists only when **page 1** has `complete=true`, `hasMore=false`, and `returnedCount==total`. A last page or out-of-range page is not an exhaustive candidate set.
- Do not infer uniqueness from a partially browsed page, or from one kind when both locations and items are eligible. When kind is unspecified, both kind result sets must satisfy the exhaustive predicate before an implicit exact-name resolution. An explicit exact-ID choice can come from a page after fresh authorization.
- An empty page with `total>0` is not an empty collection or a named search with no matches. Reset or continue paging as appropriate.
- Pagination is a live view, not a transactionally consistent snapshot. A short/malformed page, inaccessible detail, changed kind/archive status, missing path, upstream error, or timeout fails the tool. No partial read becomes `complete=true`, an authoritative zero, or a silently dropped candidate.

## Bounds

- `preview_limit` and `page_size`: strict integers 1–20; preview default 10, page size default 20.
- `page`: strict integer 1–1,000,000. `query`: string of at most 500 characters, without silent truncation.
- One 20-second asynchronous deadline covers the full invocation, including every HTTP read and streamed body.
- No more than 64 GET requests per invocation, 2 MiB decoded bytes per upstream response, and 200 nodes per path. Oversized responses and paths fail explicitly.
- Only returned cards/candidates are hydrated. Overview uses two list queries and at most three reads per preview card (detail, parent path, optional physical-location path): at most 62 GETs with a 20-card preview. An item page uses at most 62 including exact location validation; destination pages use at most 41; an exact card uses at most three; a two-card batch uses at most six, without resetting its 20-second deadline or 64-GET cap.
- No added tool calls `all_entities`, recursively expands children, or dispatches a mutation. Existing unbounded legacy tools retain their previous behavior for their existing callers.

## Docker checks

```sh
docker build -t homebox-mcp:chat-reads services/homebox-mcp
docker run --rm --network none homebox-mcp:chat-reads \
  python -m pytest -q -p no:cacheprovider
```

`tests/live_compatibility.py` also exercises these tools when explicitly run against a disposable Homebox/MCP topology. It creates synthetic accounts and inventory; do not run it against an existing inventory.
