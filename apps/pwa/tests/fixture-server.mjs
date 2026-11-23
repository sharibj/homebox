// Disposable browser-test fixture, not an application server. No real inventory or model.
import { createServer } from "node:http";
import { readFileSync, existsSync } from "node:fs";
import { resolve, extname } from "node:path";
import { randomUUID } from "node:crypto";

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
];
const entities = [
  { id: stove, name: "Camping stove", locationId: garage, locationPath: locations[0].path, description: "Two-burner stove", quantity: 1, manufacturer: "Example", model: "Trail", tags: ["camping"] },
  { id: box, name: "HDMI cable", locationId: study, locationPath: locations[1].path, description: "Spare HDMI cable", quantity: 2, tags: ["electronics"] },
];
const sessions = new Map();
const actions = new Map();
const mappings = new Map();
let confirms = 0, creates = 0, analyses = 0;
function send(res, status, data, headers = {}) {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers });
  res.end(JSON.stringify(data));
}
const error = (res, status, code, message) => send(res, status, { error: { code, message } });
function sessionView(session) {
  return { user: { id: "user", email: "demo@example.test", name: "Demo user" },
    collections: [{ id: collectionOne, name: "Our home" }, { id: collectionTwo, name: "Workshop" }],
    collectionId: session.collectionId, csrfToken: session.csrf };
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
  return action;
}
createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${port}`);
  const origin = req.headers.origin || `http://localhost:${port}`;
  if (url.pathname === "/__test/state") return send(res, 200, { confirms, creates, analyses, entities, actions: [...actions.values()] });
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
    const created = { id, collectionId: collectionOne, csrf: randomUUID() }; sessions.set(id, created);
    return send(res, 200, sessionView(created), { "Set-Cookie": `fixture_session=${id}; Path=/api/assistant/v1; HttpOnly; SameSite=Strict` });
  }
  if (!session) return error(res, 401, "unauthenticated", "Sign in to view your inventory.");
  if (req.method !== "GET" && req.headers["x-csrf-token"] !== session.csrf) return error(res, 403, "csrf", "Refresh your session.");
  if (path === "/session") return send(res, 200, sessionView(session));
  if (path === "/auth/logout") { sessions.delete(sid); return send(res, 200, { ok: true }, { "Set-Cookie": "fixture_session=; Max-Age=0; Path=/api/assistant/v1" }); }
  if (req.headers["x-collection-id"] !== session.collectionId) return error(res, 409, "COLLECTION_CONTEXT_CHANGED", "Collection context changed.");
  if (path === "/session/collection") {
    if (![collectionOne, collectionTwo].includes(body.collectionId)) return error(res, 403, "collection_denied", "You do not have access.");
    session.collectionId = body.collectionId;
    return send(res, 200, sessionView(session));
  }
  const available = session.collectionId === collectionOne;
  if (path === "/locations") return send(res, 200, { locations: available ? locations : [] });
  if (path === "/items") {
    const q = (url.searchParams.get("q") || "").toLowerCase();
    const items = available ? entities.filter(entity => entity.name.toLowerCase().includes(q) && (!url.searchParams.has("locationId") || entity.locationId === url.searchParams.get("locationId"))) : [];
    return send(res, 200, { items, total: items.length });
  }
  if (path.startsWith("/entities/")) {
    const entity = available && [...entities, ...locations.map(l => ({ ...l, locationPath: l.path }))].find(item => item.id === path.split("/").pop());
    return entity ? send(res, 200, entity) : error(res, 404, "entity_unavailable", "This record is unavailable.");
  }
  if (path === "/chat") {
    const text = String(body.message).toLowerCase();
    if (text.includes("ambiguous")) return send(res, 200, { message: "There is more than one match. Choose an exact record before making a change.", items: [], choices: { kind: "item", candidates: entities } });
    if (text.includes("move")) return send(res, 200, { message: "Review this move before confirming.", items: [], action: makeAction(session, "item.move", { entityId: box, locationId: garage }) });
    const items = available ? (text.includes("what") || text.includes("electronics") ? entities.filter(e => e.locationId === study) : entities.filter(e => e.id === stove)) : [];
    return send(res, 200, { message: items.length ? `${items[0].name} — ${items[0].locationPath}` : "No matching inventory in this collection.", items });
  }
  if (path === "/captures") {
    analyses++;
    if (raw.toString().includes("offline")) return send(res, 200, {
      captureId: randomUUID(), draft: { name: "", category: "", manufacturer: "", model: "", serialNumber: "", quantity: 1, notes: "", tags: [] },
      duplicates: [], locations, warnings: ["Photo suggestions are unavailable. Your photo was retained. Enter the item details manually."],
    });
    return send(res, 200, { captureId: randomUUID(), draft: { name: "Camping mug", category: "Camping", manufacturer: "", model: "", serialNumber: "", quantity: 1, notes: "Review model suggestions.", tags: ["camping"] }, duplicates: [entities[0]], locations, warnings: ["Model suggestions are not certain. Check the name and quantity."] });
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
