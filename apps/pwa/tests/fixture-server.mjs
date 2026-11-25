// Disposable browser-test fixture, not an application server. No real inventory or model.
import { createServer } from "node:http";
import { readFileSync, existsSync } from "node:fs";
import { resolve, extname } from "node:path";
import { createHash, randomUUID } from "node:crypto";

const port = Number(process.env.PORT || 4173);
const root = resolve("dist");
const garage = "10000000-0000-4000-8000-000000000001";
const study = "10000000-0000-4000-8000-000000000002";
const stove = "20000000-0000-4000-8000-000000000001";
const box = "20000000-0000-4000-8000-000000000002";
const collectionOne = "30000000-0000-4000-8000-000000000001";
const collectionTwo = "30000000-0000-4000-8000-000000000002";
const locations = [
  { id: garage, name: "Camping shelf", path: "Garage / Camping shelf", parentId: null, isLocation: true },
  { id: study, name: "Electronics box", path: "Study / Electronics box", parentId: null, isLocation: true },
  { id: "10000000-0000-4000-8000-000000000003", name: "Entry", path: "House / Entry", parentId: null, isLocation: true },
  { id: "10000000-0000-4000-8000-000000000004", name: "Entry", path: "Garage / Entry", parentId: null, isLocation: true },
];
const entities = [
  { id: stove, name: "Camping stove", locationId: garage, locationPath: locations[0].path, description: "Two-burner stove", quantity: 1, manufacturer: "Example", model: "Trail", tags: ["camping"] },
  { id: box, name: "HDMI cable", locationId: study, locationPath: locations[1].path, description: "Spare HDMI cable", quantity: 2, tags: ["electronics"] },
];
const sessions = new Map();
const actions = new Map();
const actionCaptures = new Map();
const consumedCaptures = new Set();
const mappings = new Map();
const captureJobs = new Map();
let confirms = 0, creates = 0, analyses = 0, submissions = 0, statusReads = 0;
function captureResult(hint, captureId = randomUUID()) {
  return hint.includes("offline") ? {
    captureId, draft: { name: "", category: "", manufacturer: "", model: "", serialNumber: "", quantity: 1, notes: "", tags: [] },
    duplicates: [], locations, warnings: ["Photo suggestions are unavailable. Your photo was retained. Enter the item details manually."],
  } : { captureId, draft: { name: "Camping mug", category: "Camping", manufacturer: "Suggested maker", model: "", serialNumber: "", quantity: 3, notes: "Review model suggestions.", tags: ["camping"] },
    duplicates: [entities[0]], locations, warnings: ["Model suggestions are not certain. Check the name and quantity."] };
}
function jobView(stored) {
  const now = Date.now(), start = stored.created + 100, end = stored.created + stored.duration;
  const done = now >= end;
  const value = { id: stored.id, status: done ? (stored.hint.includes("offline") ? "manual" : "succeeded") : now < start ? "queued" : "running",
    submissionKey: stored.submissionKey, captureId: stored.result.captureId, createdAt: new Date(stored.created).toISOString(),
    startedAt: now < start ? null : new Date(start).toISOString(), finishedAt: done ? new Date(end).toISOString() : null,
    expiresAt: new Date(stored.created + 3600_000).toISOString(), result: done ? stored.result : null, error: null, warningCodes: [] };
  return done && (consumedCaptures.has(stored.result.captureId) || now >= stored.created + 3600_000)
    ? { ...value, status: "expired", result: null, error: { code: "capture_expired", message: "This photo is no longer available. Take a new photo." }, warningCodes: [] }
    : value;
}
function send(res, status, data, headers = {}) {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers });
  res.end(JSON.stringify(data));
}
const error = (res, status, code, message) => send(res, status, { error: { code, message } });
function sessionView(session) {
  return { user: { id: "user", email: "demo@example.test", name: "Demo user" },
    collections: [{ id: collectionOne, name: "Our home" }, { id: collectionTwo, name: "Workshop" }],
    collectionId: session.collectionId, csrfToken: session.csrf, scope: scopeView(session) };
}
function scopeView(session) { return { instanceId: "fixture-homebox", instanceLabel: "Disposable Homebox", collectionId: session.collectionId, collectionName: session.collectionId === collectionOne ? "Our home" : "Workshop", contextVersion: session.contextVersion }; }
function currentConversation(session) {
  const value = session.conversation?.reply;
  if (!value) return null;
  if (value.pendingOperation?.actionId) {
    value.action = actions.get(value.pendingOperation.actionId);
    value.pendingOperation.status = value.action?.status || value.pendingOperation.status;
  }
  return value;
}
function makeAction(session, type, payload, captureId) {
  const entity = entities.find(item => item.id === payload.entityId);
  const destination = locations.find(item => item.id === payload.locationId);
  const summary = type === "item.move" ? `Move ${entity?.name || "item"} to ${destination?.path || "destination"}`
    : type === "item.create" ? `Create ${payload.name}`
    : type === "location.create" ? `Create location ${payload.name}`
    : type === "item.tags" ? `Replace tags on ${entity?.name}`
    : `Update ${entity?.name}`;
  const action = { id: randomUUID(), type, revision: 1, status: "proposed", summary, payload,
    before: entity ? { name: entity.name, location: entity.locationPath, quantity: entity.quantity } : {},
    after: { ...payload, destination: destination?.path, photoIncluded: Boolean(captureId) },
    collectionId: session.collectionId, sessionId: session.id };
  action.confirmationKey = `${action.id}:${action.revision}`;
  actions.set(action.id, action);
  if (captureId) actionCaptures.set(action.id, captureId);
  return action;
}
createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${port}`);
  const origin = req.headers.origin || `http://localhost:${port}`;
  if (url.pathname === "/__test/state") return send(res, 200, { confirms, creates, analyses, submissions, statusReads, entities, actions: [...actions.values()] });
  if (!url.pathname.startsWith("/api/assistant/v1")) {
    const filename = resolve(root, `.${url.pathname === "/" ? "/index.html" : url.pathname}`);
    const file = filename.startsWith(root + "/") && existsSync(filename) ? filename : resolve(root, "index.html");
    const type = { ".html": "text/html", ".js": "text/javascript", ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml", ".webmanifest": "application/manifest+json" }[extname(file)] || "application/octet-stream";
    res.writeHead(200, { "Content-Type": type, "Cache-Control": "no-cache" });
    return res.end(readFileSync(file));
  }
  let raw = Buffer.alloc(0);
  for await (const chunk of req) raw = Buffer.concat([raw, chunk]);
  let body = {};
  if (req.headers["content-type"]?.includes("application/json")) {
    try { body = JSON.parse(raw); } catch { return error(res, 400, "bad_json", "Invalid JSON"); }
  }
  const path = url.pathname.replace("/api/assistant/v1", "");
  const sid = /fixture_session=([a-z0-9-]+)/.exec(req.headers.cookie || "")?.[1];
  const session = sessions.get(sid);
  if (path === "/auth/login") {
    if (body.username !== "demo@example.test" || body.password !== "disposable-test-only") return error(res, 401, "invalid_login", "Invalid Homebox credentials.");
    const id = randomUUID();
    const created = { id, collectionId: collectionOne, csrf: randomUUID(), contextVersion: randomUUID() }; sessions.set(id, created);
    return send(res, 200, sessionView(created), { "Set-Cookie": `fixture_session=${id}; Path=/api/assistant/v1; HttpOnly; SameSite=Strict` });
  }
  if (!session) return error(res, 401, "unauthenticated", "Sign in to view your inventory.");
  if (req.method !== "GET" && req.headers["x-csrf-token"] !== session.csrf) return error(res, 403, "csrf", "Refresh your session.");
  if (path === "/session") return send(res, 200, sessionView(session));
  if (path === "/auth/logout") { sessions.delete(sid); return send(res, 200, { ok: true }, { "Set-Cookie": "fixture_session=; Max-Age=0; Path=/api/assistant/v1" }); }
  if (req.headers["x-collection-id"] !== session.collectionId) return error(res, 409, "COLLECTION_CONTEXT_CHANGED", "Collection context changed.");
  if (req.headers["x-context-version"] && req.headers["x-context-version"] !== session.contextVersion) return error(res, 409, "COLLECTION_CONTEXT_CHANGED", "Collection context changed.");
  if (path === "/session/collection") {
    if (![collectionOne, collectionTwo].includes(body.collectionId)) return error(res, 403, "collection_denied", "You do not have access.");
    session.collectionId = body.collectionId;
    session.contextVersion = randomUUID(); session.conversation = null;
    return send(res, 200, sessionView(session));
  }
  const available = session.collectionId === collectionOne;
  const availableLocations = [...locations, ...(session.extraLocations || [])];
  if (path === "/locations") return send(res, 200, { locations: available ? locations : [] });
  if (path === "/items") {
    const q = (url.searchParams.get("q") || "").toLowerCase();
    const items = available ? entities.filter(entity => entity.name.toLowerCase().includes(q) && (!url.searchParams.has("locationId") || entity.locationId === url.searchParams.get("locationId"))) : [];
    return send(res, 200, { items, total: items.length });
  }
  if (path.startsWith("/entities/")) {
    const entity = available && [...entities, ...(session.extraItems || []), ...availableLocations.map(l => ({ ...l, locationPath: l.path }))].find(item => item.id === path.split("/").pop());
    return entity ? send(res, 200, entity) : error(res, 404, "entity_unavailable", "This record is unavailable.");
  }
  if (path === "/conversations/current") return send(res, 200, { conversation: currentConversation(session), expired: false });
  if (path === "/conversations/reset") {
    const active = currentConversation(session);
    if (active?.action && ["proposed", "running", "uncertain", "failed"].includes(active.action.status)) return send(res, 200, { conversation: active, expired: false });
    session.conversation = null; return send(res, 200, { conversation: null, expired: false });
  }
  if (path === "/chat") {
    const text = String(body.message || "").toLowerCase();
    const conversation = session.conversation ||= { id: randomUUID(), revision: 0, turns: new Map(), operation: null, choice: null, lastEntity: null };
    if (!conversation.operation) {
      const manual = [...actions.values()].reverse().find(action => action.sessionId === session.id && action.collectionId === session.collectionId && action.status === "proposed");
      const destination = manual && locations.find(item => item.id === manual.payload.locationId);
      if (manual && destination) conversation.operation = {
        id: randomUUID(), type: manual.type, payload: { ...manual.payload }, actionId: manual.id, status: manual.status,
        captureId: actionCaptures.get(manual.id),
        destination: { id: destination.id, name: destination.name, path: destination.path, kind: "location" },
      };
    }
    const fingerprint = JSON.stringify(body);
    if (body.clientTurnId && conversation.turns.has(body.clientTurnId)) {
      const saved = conversation.turns.get(body.clientTurnId);
      return saved.fingerprint === fingerprint ? send(res, 200, saved.reply) : error(res, 409, "idempotency_conflict", "Turn key belongs to different content.");
    }
    if (body.conversationId && (body.conversationId !== conversation.id || body.expectedRevision !== conversation.revision)) return error(res, 409, "conversation_changed", "This conversation changed.");
    const reply = { conversationId: conversation.id, turnId: body.clientTurnId || randomUUID(), revision: conversation.revision + 1, outcome: "answered", scope: scopeView(session), message: "", items: [] };
    function choices(items, slot = "destination") {
      const candidates = items.map(item => ({ id: `candidate_${randomUUID()}`, entityId: item.id, label: item.name, path: item.path || item.locationPath || item.name, kind: item.isLocation ? "location" : "item" }));
      conversation.choice = { id: randomUUID(), slot, reason: "ambiguous", candidates };
      reply.clarification = conversation.choice; reply.outcome = "clarification";
    }
    function preview() {
      const operation = conversation.operation;
      reply.action = makeAction(session, operation.type, { ...operation.payload, locationId: operation.destination.id }, operation.captureId || actionCaptures.get(operation.actionId));
      operation.actionId = reply.action.id; operation.status = "proposed";
      reply.outcome = "proposal"; reply.message = "Here is your draft. Review every value, then confirm when it is right.";
    }
    const add = /^add (?:a |an |one |1 )?(.+?) to (?:the )?(.+?)[.]?$/i.exec(body.message || "");
    if (body.selection) {
      const choice = conversation.choice?.id === body.selection.clarificationId && conversation.choice.candidates.find(item => item.id === body.selection.candidateId);
      if (!choice || !conversation.operation) return error(res, 409, "clarification_expired", "This choice is no longer active.");
      conversation.operation.destination = { id: choice.entityId, name: choice.label, path: choice.path, kind: choice.kind };
      conversation.choice = null; preview();
    } else if (body.continuation) {
      reply.items = available ? entities.slice(1) : [];
      reply.message = "More items in this collection.";
      reply.pagination = { continuationId: null, totalRecords: available ? entities.length : 0, page: 2, pageSize: 1, hasMore: false, complete: true };
    } else if (text === "browse twenty destinations") {
      session.extraLocations ||= Array.from({ length: Math.max(0, 20 - locations.length) }, (_, index) => ({ id: randomUUID(), name: `QA shelf ${index + 1}`, path: `Workshop / QA shelf ${index + 1}`, isLocation: true }));
      choices([...locations, ...session.extraLocations]);
      reply.message = "Choose a destination from this page of 20 locations.";
      reply.pagination = { continuationId: null, totalRecords: 20, page: 1, pageSize: 20, hasMore: false, complete: true };
    } else if (text === "show twenty item records") {
      session.extraItems ||= Array.from({ length: 20 }, (_, index) => ({ id: randomUUID(), name: `QA paged item ${index + 1}`, quantity: 1, locationPath: "Workshop" }));
      conversation.choice = null;
      reply.items = session.extraItems;
      reply.message = "Here are the requested 20 item records. Your add draft is still pending; browse destinations to resume it.";
      reply.pagination = { continuationId: null, totalRecords: 20, page: 1, pageSize: 20, hasMore: false, complete: true };
    } else if (text === "prepare budgeted review" && conversation.operation?.destination) {
      const old = actions.get(conversation.operation.actionId);
      if (old?.status === "proposed") old.status = "cancelled";
      delete conversation.operation.actionId; conversation.operation.status = "clarification";
      conversation.choice = { id: randomUUID(), slot: "review", reason: "missing", candidates: [] };
      reply.clarification = conversation.choice; reply.outcome = "clarification";
      reply.message = "The draft is retained. Use review draft to prepare its complete preview.";
    } else if (text === "review draft" && conversation.operation?.destination) {
      conversation.choice = null; preview();
    } else if (text.includes("make that two") || text.includes("quantity to 2")) {
      if (!conversation.operation) reply.message = "Which draft would you like to change?";
      else {
        const old = actions.get(conversation.operation.actionId);
        if (old?.status === "proposed") old.status = "cancelled";
        conversation.operation.payload.quantity = 2; preview();
      }
    } else if (add) {
      if (conversation.operation?.actionId && actions.get(conversation.operation.actionId)?.status === "proposed") reply.message = "Finish or cancel the current draft before adding another item.";
      else {
        conversation.operation = { id: randomUUID(), type: "item.create", payload: { name: add[1], quantity: 1 }, destinationQuery: add[2], status: "clarification" };
        reply.message = "I can add that. Which Entry location did you mean?";
        choices(available ? locations.filter(location => location.name === "Entry") : []);
      }
    } else if (text.includes("destinations") || text.includes("locations")) {
      reply.message = `Here are the available destinations in ${scopeView(session).collectionName}.`;
      if (conversation.operation && !conversation.operation.actionId) choices(available ? availableLocations : []);
      else reply.destinations = available ? availableLocations.map(item => ({ id: item.id, name: item.name, path: item.path, kind: "location", parentId: item.parentId })) : [];
    } else if (text.includes("inventory") || text.includes("show items")) {
      reply.message = available ? "Here is the inventory in Our home." : "Workshop has no inventory records yet.";
      reply.overview = { itemRecordCount: available ? entities.length : 0, locationCount: available ? locations.length : 0, includeArchived: false, complete: true };
      reply.items = available ? entities.slice(0, 1) : [];
      reply.pagination = { continuationId: available ? "next-items" : null, totalRecords: available ? entities.length : 0, page: 1, pageSize: 1, hasMore: available, complete: true };
    } else if (text.includes("ambiguous")) {
      reply.message = "There is more than one match. Choose an exact record before making a change."; choices(entities, "item");
    } else if (text.includes("move")) {
      reply.message = "Review this move before confirming."; reply.outcome = "proposal"; reply.action = makeAction(session, "item.move", { entityId: box, locationId: garage });
    } else if (text.includes("add an item")) {
      reply.message = "What is the item's name, and where would you like to keep it?";
    } else if (text === "yes") {
      reply.message = "Use the explicit Confirm change button after reviewing the preview. A message cannot confirm a change.";
    } else {
      const previous = conversation.operation?.actionId && actions.get(conversation.operation.actionId)?.result;
      const items = available ? (text.includes("where is it") && previous ? [previous] : text.includes("what") || text.includes("electronics") ? entities.filter(e => e.locationId === study) : entities.filter(e => e.id === stove)) : [];
      reply.items = items; reply.message = items.length ? `${items[0].name} — ${items[0].locationPath}` : "No matching inventory in this collection.";
    }
    if (conversation.operation) reply.pendingOperation = structuredClone(conversation.operation);
    if (!reply.action && conversation.operation?.actionId) reply.action = actions.get(conversation.operation.actionId);
    conversation.revision = reply.revision; conversation.reply = structuredClone(reply);
    if (body.clientTurnId) conversation.turns.set(body.clientTurnId, { fingerprint, reply: structuredClone(reply) });
    return send(res, 200, reply);
  }
  if (path === "/captures") {
    analyses++;
    return send(res, 200, captureResult(raw.toString().includes("offline") ? "offline" : ""));
  }
  if (path === "/capture-jobs" && req.method === "POST") {
    submissions++;
    const submissionKey = req.headers["idempotency-key"];
    if (!submissionKey) return error(res, 400, "idempotency_required", "A photo submission key is required.");
    const form = await new Request("http://fixture", { method: "POST", body: raw, headers: { "Content-Type": req.headers["content-type"] } }).formData();
    const hint = String(form.get("hint") || "");
    const image = Buffer.from(await form.get("image").arrayBuffer());
    const signature = createHash("sha256").update(image).update(hint).update(String(form.get("locationId") || "")).digest("hex");
    const existing = [...captureJobs.values()].find(value => value.submissionKey === submissionKey && value.sessionId === session.id && value.collectionId === session.collectionId);
    if (existing) return existing.signature === signature ? send(res, 202, jobView(existing)) : error(res, 409, "idempotency_conflict", "This key belongs to a different photo.");
    analyses++;
    const stored = { id: randomUUID(), submissionKey, sessionId: session.id, collectionId: session.collectionId, signature, hint,
      created: Date.now(), duration: hint.includes("slow-184") ? 184_000 : hint.includes("delayed") ? 4500 : 250,
      result: captureResult(hint) };
    captureJobs.set(stored.id, stored);
    return send(res, 202, jobView(stored));
  }
  if (path === "/capture-jobs" && req.method === "GET") {
    statusReads++;
    const jobs = [...captureJobs.values()].filter(value => value.sessionId === session.id && value.collectionId === session.collectionId
      && (!url.searchParams.has("submissionKey") || value.submissionKey === url.searchParams.get("submissionKey"))).reverse().slice(0, 20).map(jobView);
    return send(res, 200, { jobs });
  }
  if (path.startsWith("/capture-jobs/") && req.method === "GET") {
    statusReads++;
    const value = captureJobs.get(path.split("/").pop());
    return value && value.sessionId === session.id && value.collectionId === session.collectionId
      ? send(res, 200, jobView(value)) : error(res, 404, "capture_job_unavailable", "This photo job is unavailable.");
  }
  if (path === "/actions" && req.method === "POST") {
    if (!available) return error(res, 403, "collection_denied", "No test records in this collection.");
    return send(res, 200, makeAction(session, body.type, body.payload, body.captureId));
  }
  if (path === "/actions" && req.method === "GET") return send(res, 200, {
    actions: [...actions.values()].filter(action => action.sessionId === session.id && action.collectionId === session.collectionId).reverse().slice(0,50),
  });
  if (path.startsWith("/actions/")) {
    const [id, operation] = path.slice("/actions/".length).split("/");
    const action = actions.get(id);
    if (!action || action.collectionId !== session.collectionId || action.sessionId !== session.id) return error(res, 404, "action_unavailable", "This change is not available in this collection.");
    if (operation === "cancel") action.status = "cancelled";
    if (operation === "confirm" && action.status === "proposed") {
      if (!req.headers["idempotency-key"] || body.revision !== action.revision) return error(res, 409, "stale_action", "Review this change again.");
      action.confirmationKey = req.headers["idempotency-key"];
      confirms++;
      const payload = action.payload;
      let entity = entities.find(e => e.id === payload.entityId);
      if (action.type === "item.create") { entity = { ...payload, id: randomUUID(), locationPath: locations.find(l => l.id === payload.locationId)?.path }; entities.push(entity); creates++; }
      if (action.type === "item.move" && entity) { entity.locationId = payload.locationId; entity.locationPath = locations.find(l => l.id === payload.locationId)?.path; }
      if (action.type === "item.update" && entity) Object.assign(entity, payload);
      if (action.type === "item.tags" && entity) entity.tags = payload.tags;
      if (action.type === "location.create") locations.push({ ...payload, id: randomUUID(), isLocation: true, path: payload.name });
      action.result = entity || null; action.status = "succeeded";
      if (actionCaptures.has(action.id)) consumedCaptures.add(actionCaptures.get(action.id));
    }
    return send(res, 200, action);
  }
  if (path === "/tags/mappings" && req.method === "GET") return send(res, 200, { mappings: available ? [...mappings.values()] : [] });
  if (path === "/tags/mappings" && req.method === "POST") {
    const tagId = `t_${randomUUID()}`;
    const mapping = { tagId, ...body, status: "active", url: `${origin}/t/${tagId}`, createdAt: new Date().toISOString(), updatedAt: new Date().toISOString() };
    mappings.set(tagId, mapping); return send(res, 200, mapping);
  }
  if (path.startsWith("/tags/mappings/") && req.method === "PATCH") {
    const mapping = mappings.get(path.split("/").pop());
    if (!mapping) return error(res, 404, "tag_unavailable", "This tag is unavailable.");
    Object.assign(mapping, body, { updatedAt: new Date().toISOString() }); return send(res, 200, mapping);
  }
  if (path.startsWith("/tags/resolve/")) {
    const mapping = mappings.get(path.split("/").pop());
    if (!available || !mapping || mapping.status === "revoked") return error(res, 404, "tag_unavailable", "This tag may be unknown, revoked, or in a different collection.");
    const entity = [...entities, ...locations.map(l => ({ ...l, locationPath: l.path }))].find(e => e.id === mapping.entityId);
    return send(res, 200, { mapping, entity });
  }
  return error(res, 404, "not_found", "Unknown fixture endpoint");
}).listen(port, "0.0.0.0", () => console.log(`Disposable PWA fixture listening on ${port}. Not a real Homebox backend.`));
