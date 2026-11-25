import { mkdirSync, readFileSync, renameSync, statSync, writeFileSync } from "node:fs";
import { createHash, randomUUID } from "node:crypto";
import { isAbsolute, join } from "node:path";
import { test, expect } from "@playwright/test";

// Run only against the explicitly authorized disposable stack. Never record login.
test.use({ trace: "off", screenshot: "off", actionTimeout: 15000 });
const credentialFile = process.env.CONVERSATION_QA_CREDENTIAL_FILE;
const oracleFile = process.env.CONVERSATION_QA_ORACLE_FILE;
const credentials = credentialFile ? JSON.parse(readFileSync(credentialFile, "utf8")) : null;
const oracle = oracleFile ? JSON.parse(readFileSync(oracleFile, "utf8")) : null;
const privateCheckpointRoot = process.env.CONVERSATION_QA_PRIVATE_CHECKPOINT_DIR;
const checkpoints = new WeakMap();
const base = "/api/assistant/v1";
const chat = page => page.getByRole("region", { name: "Inventory conversation", exact: true });
const review = page => page.getByRole("region", { name: "Inventory change confirmation", exact: true });
const turn = (page, reply) => chat(page).locator(`[data-turn-id=${JSON.stringify(`${reply.turnId}:assistant`)}]`);
const savedEntity = action => action.result?.entity || action.result;
const evidence = (name, value) => writeFileSync(`/results/${name}.json`, JSON.stringify(value, null, 2));

function writePrivate(path, value) {
  const temporary = `${path}.${randomUUID()}.tmp`;
  writeFileSync(temporary, JSON.stringify(value, null, 2), { flag: "wx", mode: 0o600 });
  renameSync(temporary, path);
  if ((statSync(path).mode & 0o777) !== 0o600) throw new Error("Private checkpoint file permissions differ from 0600.");
}
function scalarFields(value, names) {
  if (!value || typeof value !== "object") return {};
  return Object.fromEntries(names.filter(name => Object.hasOwn(value, name)
    && (value[name] === null || ["string", "number", "boolean"].includes(typeof value[name])))
    .map(name => [name, value[name]]));
}
function safeChatRequest(body) {
  const summary = scalarFields(body, ["message", "mode", "clientTurnId", "conversationId", "expectedRevision"]);
  if (body?.selection) summary.selection = scalarFields(body.selection, ["clarificationId", "candidateId"]);
  if (body?.continuation) summary.continuation = scalarFields(body.continuation, ["id"]);
  return summary;
}
function safeChatDestination(destination) {
  return scalarFields(destination, ["id", "entityId", "label", "name", "path", "kind"]);
}
function safeChatReply(reply) {
  const summary = scalarFields(reply, ["outcome", "message", "conversationId", "turnId", "revision"]);
  if (reply?.scope) summary.scope = scalarFields(reply.scope, ["instanceId", "collectionId", "contextVersion"]);
  if (reply?.overview) summary.overview = scalarFields(reply.overview, ["itemRecordCount", "locationCount", "includeArchived", "complete"]);
  if (Array.isArray(reply?.destinations)) summary.destinations = reply.destinations.map(safeChatDestination);
  if (reply?.clarification) summary.clarification = {
    ...scalarFields(reply.clarification, ["id", "slot", "reason"]),
    candidates: (reply.clarification.candidates || []).map(safeChatDestination),
  };
  if (reply?.pendingOperation) {
    const operation = reply.pendingOperation;
    summary.pendingOperation = {
      ...scalarFields(operation, ["id", "type", "status", "actionId", "destinationQuery"]),
      payload: scalarFields(operation.payload, ["name", "quantity", "locationId", "parentId"]),
      ...(operation.destination ? { destination: safeChatDestination(operation.destination) } : {}),
    };
  }
  if (reply?.action) summary.action = {
    ...scalarFields(reply.action, ["id", "type", "status"]),
    payload: scalarFields(reply.action.payload, ["name", "quantity", "locationId", "parentId"]),
  };
  if (reply?.error) summary.error = scalarFields(reply.error, ["code", "message"]);
  return summary;
}
function chatPost(request) {
  return request.method() === "POST" && new URL(request.url()).pathname === `${base}/chat`;
}
function observeChatRequest(page, request) {
  const current = checkpoints.get(page);
  if (!current || !chatPost(request)) return null;
  if (current.observationFailure) throw new Error("A private chat observation could not be saved.");
  let entry = current.observedChatRequests.get(request);
  if (!entry) {
    entry = { sequence: current.chatObservations.length + 1, request: safeChatRequest(request.postDataJSON()), replies: [] };
    current.observedChatRequests.set(request, entry);
    current.chatObservations.push(entry);
    writePrivate(join(current.directory, "chat-observations.json"), current.chatObservations);
  }
  return entry;
}
function observeChatReply(page, request, reply, status, source, jsonUnavailable = false) {
  const entry = observeChatRequest(page, request);
  if (!entry) return;
  entry.replies.push({ source, status, ...(jsonUnavailable ? { jsonUnavailable: true } : { reply: safeChatReply(reply) }) });
  writePrivate(join(checkpoints.get(page).directory, "chat-observations.json"), checkpoints.get(page).chatObservations);
}
async function observeBrowserChatReply(page, response) {
  let reply;
  try { reply = await response.json(); } catch {
    observeChatReply(page, response.request(), null, response.status(), "browser-response", true);
    return;
  }
  observeChatReply(page, response.request(), reply, response.status(), "browser-response");
}
async function checkpoint(page, stage) {
  const current = checkpoints.get(page);
  if (!current) return;
  const sequence = ++current.sequence;
  const authFile = `auth-${sequence}.json`, stateFile = `state-${sequence}.json`;
  const auth = await page.context().storageState();
  expect(auth.origins.every(origin => origin.localStorage.length === 0)).toBe(true);
  writePrivate(join(current.directory, authFile), auth);
  writePrivate(join(current.directory, stateFile), {
    stage, url: page.url(), scope: current.scope, conversation: current.conversation, replay: current.replay,
  });
  writePrivate(join(current.directory, "latest.json"), { stage, authFile, stateFile });
  evidence(`private-checkpoint-${current.caseId}`, { saved: true, stage, mode: "0600", outsideRepository: true });
}
function requestShape(headers, body) {
  const normalized = Object.fromEntries(Object.entries(headers).map(([name, value]) => [name.toLowerCase(), value]));
  return {
    originPresent: Boolean(normalized.origin), csrfPresent: Boolean(normalized["x-csrf-token"]),
    collectionPresent: Boolean(normalized["x-collection-id"]), contextPresent: Boolean(normalized["x-context-version"]),
    idempotencyPresent: Boolean(normalized["idempotency-key"]), bodyFields: Object.keys(body || {}).sort(),
  };
}
test.beforeEach(async ({ page }, info) => {
  if (!credentials || !oracle) return;
  if (!privateCheckpointRoot || !isAbsolute(privateCheckpointRoot)
    || ["/results", "/workspace", "/browser"].some(root => privateCheckpointRoot === root || privateCheckpointRoot.startsWith(`${root}/`))) {
    throw new Error("Live acceptance requires a private checkpoint mount outside the repository and results.");
  }
  mkdirSync(privateCheckpointRoot, { recursive: true, mode: 0o700 });
  if ((statSync(privateCheckpointRoot).mode & 0o077) !== 0) throw new Error("Private checkpoint root must exclude group and other access.");
  const caseId = createHash("sha256").update(info.testId).digest("hex").slice(0, 12);
  const directory = join(privateCheckpointRoot, `${caseId}-${randomUUID()}`);
  mkdirSync(directory, { mode: 0o700 });
  checkpoints.set(page, { caseId, directory, sequence: 0, scope: null, conversation: null, replay: null });
  const current = checkpoints.get(page);
  current.chatObservations = [];
  current.observedChatRequests = new WeakMap();
  page.on("request", request => {
    try { observeChatRequest(page, request); } catch (cause) { current.observationFailure = cause.name; }
  });
});
test.afterEach(async ({ page }, info) => {
  if (!checkpoints.has(page) || page.isClosed()) return;
  await checkpoint(page, `test-${info.status}`);
  if (checkpoints.get(page).observationFailure) throw new Error("A private chat observation could not be saved.");
});

async function scoped(page) {
  const response = await page.request.get(`${base}/session`);
  expect(response.status()).toBe(200);
  const session = await response.json();
  const current = checkpoints.get(page);
  if (current) {
    if (current.scope?.contextVersion !== session.scope.contextVersion) current.conversation = null;
    current.scope = session.scope;
  }
  return { session, headers: {
    Origin: new URL(page.url()).origin,
    "X-CSRF-Token": session.csrfToken, "X-Collection-ID": session.collectionId,
    "X-Context-Version": String(session.scope.contextVersion),
  } };
}
async function records(page) {
  const { headers } = await scoped(page);
  const response = await page.request.get(`${base}/items?q=`, { headers });
  expect(response.status()).toBe(200);
  const result = await response.json();
  expect(result.items.length).toBe(result.total);
  return result.items;
}
async function login(page, viewport = { width: 360, height: 800 }) {
  await page.setViewportSize(viewport);
  await page.goto("/");
  await page.getByLabel("Email or username").waitFor();
  await page.evaluate(({ username, password }) => {
    for (const [selector, value] of [['input[autocomplete="username"]', username], ['input[autocomplete="current-password"]', password]]) {
      const input = document.querySelector(selector);
      input.value = value; input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }, { username: credentials.username, password: credentials.password });
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByLabel("Collection", { exact: true })).toBeEnabled({ timeout: 30000 });
  if (await page.getByLabel("Collection", { exact: true }).inputValue() !== oracle.primaryCollection.id) {
    await page.getByLabel("Collection", { exact: true }).selectOption(oracle.primaryCollection.id);
  }
  await expect(chat(page).getByRole("button", { name: "Show inventory", exact: true })).toBeEnabled({ timeout: 30000 });
  const asset = await page.locator('script[type="module"][src]').getAttribute("src");
  if (process.env.EXPECTED_PWA_ASSET) expect(asset).toBe(`/assets/${process.env.EXPECTED_PWA_ASSET}`);
  const { session } = await scoped(page);
  for (const role of ["chat", "vision"]) expect(session.ai[role]).toMatchObject({ provider: "anthropic-messages", model: "claude-haiku-4-5" });
  await checkpoint(page, "after-login");
  return asset;
}
async function requestTurn(page, action, predicate = () => true) {
  const response = page.waitForResponse(response => response.url().endsWith("/chat") && response.request().method() === "POST" && predicate(response.request().postDataJSON()), { timeout: 90000 });
  const started = Date.now();
  await action();
  const received = await response;
  await observeBrowserChatReply(page, received);
  expect(received.status()).toBe(200);
  const reply = await received.json();
  const current = checkpoints.get(page);
  if (current) {
    current.scope = reply.scope;
    current.conversation = { id: reply.conversationId, revision: reply.revision, turnId: reply.turnId,
      actionId: reply.action?.id || reply.pendingOperation?.actionId || null };
  }
  await expect(turn(page, reply)).toBeVisible();
  await expect(chat(page).getByRole("button", { name: "Ask Homebox", exact: true })).toHaveText("Send");
  expect(reply.items.length).toBeLessThanOrEqual(20);
  expect(reply.clarification?.candidates.length || 0).toBeLessThanOrEqual(20);
  const elapsedMs = Date.now() - started;
  await checkpoint(page, "after-turn");
  return { reply, request: received.request().postDataJSON(), elapsedMs };
}
async function ask(page, text) {
  const composer = page.getByLabel("Ask your inventory", { exact: true });
  await composer.fill(text);
  await expect(chat(page).getByRole("button", { name: "Ask Homebox", exact: true })).toBeEnabled({ timeout: 90000 });
  return requestTurn(page, () => composer.press("Enter"), body => body.message === text);
}
async function maybeReview(page, result) {
  if (result.reply.clarification?.slot !== "review") return result;
  const reviewed = await requestTurn(page, () => turn(page, result.reply).getByRole("button", { name: "Review draft", exact: true }).click(), body => body.message === "review draft");
  expect(reviewed.request.mode).toBe("normal");
  expect(reviewed.request.expectedRevision).toBe(result.reply.revision);
  expect(reviewed.request.clientTurnId).not.toBe(result.request.clientTurnId);
  return reviewed;
}
async function oneConfirmation(page) {
  const button = page.getByRole("button", { name: "Confirm change", exact: true });
  await expect(button).toHaveCount(1);
  await expect(button).toBeEnabled();
  return button;
}
function paths(reply) {
  return (reply.clarification?.candidates || reply.destinations || []).map(item => item.path);
}

test("real stack conversation resolves both Entry paths, edits, and creates exactly one reviewed item", async ({ page }) => {
  test.skip(!credentials || !oracle, "Requires protected disposable conversation QA files.");
  test.setTimeout(360000);
  const errors = []; page.on("pageerror", error => errors.push(error.name));
  const proof = { kind: "real-stack-canonical-flow", turns: [] };
  proof.asset = await login(page);
  const before = await records(page);
  expect(before.length).toBe(oracle.primaryCollection.itemRecords);
  expect(before.reduce((sum, item) => sum + item.quantity, 0)).toBe(oracle.primaryCollection.quantitySumNotRecordCount);
  expect(before.map(item => [item.id, item.name, item.quantity]).sort()).toEqual(oracle.items.map(item => [item.id, item.name, item.quantity]).sort());
  proof.beforeRecordCount = before.length;
  const track = result => { proof.turns.push({ message: result.request.message || "selection/page", elapsedMs: result.elapsedMs, outcome: result.reply.outcome, revision: result.reply.revision }); return result; };
  let overview = track(await ask(page, "Which inventory do I have?"));
  expect(overview.reply.overview).toMatchObject({ itemRecordCount: 25, locationCount: 13, includeArchived: false, complete: true });
  expect(overview.reply.scope.collectionId).toBe(oracle.primaryCollection.id);
  await chat(page).screenshot({ path: "/results/real-chat-overview-before.png" });
  const seen = new Set(overview.reply.items.map(item => item.id));
  expect(overview.reply.pagination?.hasMore).toBe(true);
  for (let index = 0; overview.reply.pagination?.hasMore && index < 3; index++) {
    overview = track(await requestTurn(page, () => turn(page, overview.reply).getByRole("button", { name: "Show more", exact: true }).click(), body => !!body.continuation));
    for (const item of overview.reply.items) { expect(seen.has(item.id)).toBe(false); seen.add(item.id); }
  }
  expect(overview.reply.pagination?.hasMore).toBe(false);
  expect([...seen].sort()).toEqual(oracle.items.map(item => item.id).sort());
  const draft = track(await ask(page, "Add a mobile charge to the entry."));
  expect(draft.reply.pendingOperation?.payload).toMatchObject({ name: oracle.pendingCreateExpectedName, quantity: 1 });
  expect(draft.reply.clarification?.reason).toBe("ambiguous");
  const issued = draft.reply.clarification.candidates;
  expect(issued).toHaveLength(3);
  expect(new Set(issued.map(item => item.id)).size).toBe(3);
  const entryIds = ["Ground floor / Entry", "Upper floor / Entry"].map(path => oracle.locations[path]);
  for (const candidate of issued) {
    expect(Object.keys(candidate).sort()).toEqual(["entityId", "id", "kind", "label", "path"]);
    expect(candidate.id).toMatch(/^ch_[A-Za-z0-9_-]+$/);
    expect(entryIds).not.toContain(candidate.id);
  }
  for (const path of ["Ground floor / Entry", "Upper floor / Entry"]) {
    expect(issued).toContainEqual({ id: expect.stringMatching(/^ch_[A-Za-z0-9_-]+$/), entityId: oracle.locations[path], label: "Entry", path, kind: "location" });
  }
  expect(issued).toContainEqual({ id: expect.stringMatching(/^ch_[A-Za-z0-9_-]+$/), entityId: null, label: "Leave unassigned", path: "", kind: "unassigned" });
  const unassigned = turn(page, draft.reply).locator(".destination-choice").filter({ has: page.getByRole("button", { name: "Use Leave unassigned", exact: true }) });
  await expect(unassigned).toHaveCount(1);
  await expect(unassigned.getByRole("button")).toHaveCount(1);
  await expect(unassigned.getByRole("button", { name: "Use Leave unassigned", exact: true })).toBeEnabled();
  await chat(page).screenshot({ path: "/results/real-chat-mobile-entry-choices.png" });
  let destinations = track(await ask(page, "Which destinations do I have access to?"));
  expect(destinations.reply.pendingOperation?.id).toBe(draft.reply.pendingOperation.id);
  for (const path of ["Ground floor / Entry", "Upper floor / Entry"]) expect(paths(destinations.reply)).toContain(path);
  const emptyItem = oracle.items.find(item => item.name === oracle.emptyItemContainer);
  expect(emptyItem).toBeTruthy();
  const hasEmptyItem = reply => (reply.clarification?.candidates || reply.destinations || [])
    .some(item => (item.entityId || item.id) === emptyItem.id && item.kind === "item");
  if (!hasEmptyItem(destinations.reply)) {
    const containers = track(await ask(page, "Show item containers."));
    expect(hasEmptyItem(containers.reply)).toBe(true);
    expect(containers.reply.pendingOperation?.id).toBe(draft.reply.pendingOperation.id);
    destinations = track(await ask(page, "Which destinations do I have access to?"));
  }
  const ground = destinations.reply.clarification.candidates.find(item => item.entityId === oracle.locations["Ground floor / Entry"]);
  expect(ground).toBeTruthy(); expect(ground.id).not.toBe(ground.entityId);
  await turn(page, destinations.reply).getByRole("button", { name: "View Ground floor / Entry", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Entry", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Confirm change", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Back to inventory", exact: true }).click();
  let proposal = track(await requestTurn(page, () => turn(page, destinations.reply).getByRole("button", { name: "Use Ground floor / Entry", exact: true }).click(), body => !!body.selection));
  expect(proposal.request.mode).toBe("normal");
  expect(proposal.request.selection.candidateId).toBe(ground.id);
  proposal = track(await maybeReview(page, proposal));
  const oldAction = proposal.reply.action.id;
  await oneConfirmation(page);
  await review(page).getByRole("button", { name: "Edit draft", exact: true }).click();
  await expect(page.getByRole("button", { name: "Confirm change", exact: true })).toBeDisabled();
  proposal = track(await maybeReview(page, track(await ask(page, "make that two"))));
  expect(proposal.reply.action.id).not.toBe(oldAction);
  expect(proposal.reply.action.payload).toMatchObject({ name: "mobile charge", quantity: 2, locationId: ground.entityId });
  await oneConfirmation(page);
  track(await ask(page, "yes"));
  await oneConfirmation(page);
  expect((await records(page)).map(item => item.id).sort()).toEqual(before.map(item => item.id).sort());
  const { headers } = await scoped(page);
  const obsolete = await (await page.request.get(`${base}/actions/${oldAction}`, { headers })).json();
  expect(obsolete.status).toBe("cancelled");
  await page.getByText(/Recent changes & recovery/).click();
  const history = page.locator("details").filter({ has: page.locator("summary", { hasText: "Recent changes & recovery" }) });
  await history.locator(`[data-action-id=${JSON.stringify(oldAction)}]`).click();
  await expect(review(page)).toContainText("Change cancelled");
  await expect(page.getByRole("button", { name: "Confirm change", exact: true })).toHaveCount(0);
  await history.locator(`[data-action-id=${JSON.stringify(proposal.reply.action.id)}]`).click();
  const confirmation = await oneConfirmation(page);
  await chat(page).screenshot({ path: "/results/real-chat-reviewed-before-confirm.png" });
  await checkpoint(page, "before-confirm");
  const response = page.waitForResponse(response => response.url().endsWith("/confirm") && response.request().method() === "POST", { timeout: 90000 });
  await confirmation.click();
  const confirmed = await response;
  expect(confirmed.status()).toBe(200);
  const confirmedAction = await confirmed.json(), entity = savedEntity(confirmedAction);
  expect(confirmedAction.status).toBe("succeeded");
  const workingRequest = confirmed.request(), workingHeaders = await workingRequest.allHeaders(), workingBody = workingRequest.postDataJSON();
  const replayHeaders = {
    Origin: new URL(page.url()).origin,
    "X-CSRF-Token": workingHeaders["x-csrf-token"], "X-Collection-ID": workingHeaders["x-collection-id"],
    "X-Context-Version": workingHeaders["x-context-version"], "Idempotency-Key": workingHeaders["idempotency-key"],
  };
  const same = {
    origin: workingHeaders.origin === replayHeaders.Origin, csrf: workingHeaders["x-csrf-token"] === replayHeaders["X-CSRF-Token"],
    collection: workingHeaders["x-collection-id"] === replayHeaders["X-Collection-ID"],
    context: workingHeaders["x-context-version"] === replayHeaders["X-Context-Version"],
    key: workingHeaders["idempotency-key"] === replayHeaders["Idempotency-Key"],
    body: workingRequest.postData() === JSON.stringify(workingBody), sharedBrowserCookieJar: page.request === page.context().request,
  };
  const requestProof = { workingUiObserved: requestShape(workingHeaders, workingBody),
    replayClientConfiguration: requestShape(replayHeaders, workingBody), same };
  checkpoints.get(page).replay = { url: confirmed.url(), method: "POST", headers: replayHeaders, body: workingBody };
  await checkpoint(page, "after-confirm-before-replay");
  evidence("real-confirmation-replay", { initialStatus: confirmed.status(), requestProof, replayStatus: null });
  for (const shape of [requestProof.workingUiObserved, requestProof.replayClientConfiguration]) {
    expect(shape).toMatchObject({ originPresent: true, csrfPresent: true, collectionPresent: true, contextPresent: true, idempotencyPresent: true });
    expect(shape.bodyFields).toEqual(["revision"]);
  }
  expect(Object.values(same).every(Boolean)).toBe(true);
  await expect(review(page)).toContainText("Saved to Homebox", { timeout: 30000 });
  let repeated;
  try {
    repeated = await page.request.post(confirmed.url(), { headers: replayHeaders, data: workingBody });
  } catch (cause) {
    evidence("real-confirmation-replay", { initialStatus: confirmed.status(), requestProof, replayStatus: null, networkError: cause.name });
    throw new Error("The replay request failed at the network boundary. Its original identity is retained only in the private checkpoint.");
  }
  let repeatedBody;
  try { repeatedBody = await repeated.json(); } catch { repeatedBody = { error: { code: "non_json", message: "The replay response was not JSON." } }; }
  evidence("real-confirmation-replay", {
    initialStatus: confirmed.status(), requestProof, replayStatus: repeated.status(),
    sameEntity: repeated.status() === 200 && savedEntity(repeatedBody)?.id === entity.id,
    error: repeated.status() >= 400 ? { code: repeatedBody.error?.code || "unknown", message: String(repeatedBody.error?.message || "No public error message.").slice(0, 300) } : null,
  });
  await checkpoint(page, "after-replay");
  expect(repeated.status()).toBe(200);
  expect(savedEntity(repeatedBody).id).toBe(entity.id);
  const after = await records(page), created = after.filter(item => !before.some(previous => previous.id === item.id));
  expect(created).toHaveLength(1);
  expect(created[0]).toMatchObject({ id: entity.id, name: "mobile charge", quantity: 2, locationId: ground.entityId });
  expect(after.filter(item => item.name === "mobile charge")).toHaveLength(1);
  proof.afterRecordCount = after.length; proof.created = created[0]; proof.repeatConfirmationSameEntity = true;
  evidence("real-conversation", proof);
  const followup = track(await ask(page, "where is it?"));
  expect(JSON.stringify(followup.reply)).toContain("Ground floor / Entry");
  expect(followup.reply.items.map(item => item.id)).toContain(entity.id);
  await chat(page).screenshot({ path: "/results/real-chat-saved-followup.png" });
  await page.reload();
  await expect(chat(page)).toContainText("Restored the latest saved response", { timeout: 30000 });
  await expect(chat(page).locator(".turn.user")).toHaveCount(0);
  await expect(chat(page)).toContainText("Ground floor / Entry");
  expect(errors).toHaveLength(0);
  await checkpoint(page, "canonical-flow-complete");
  evidence("real-conversation", proof);
});

test("real lost proposal response replays once and collection A B A rejects an old response", async ({ page }) => {
  test.skip(!credentials || !oracle, "Requires protected disposable conversation QA files.");
  test.setTimeout(240000);
  const asset = await login(page, { width: 1280, height: 1024 });
  const before = await records(page);
  expect(before.length).toBe(oracle.primaryCollection.itemRecords + 1);
  const initial = await ask(page, "Add a recovery test item to the entry.");
  const candidate = initial.reply.clarification.candidates.find(item => item.entityId === oracle.locations["Ground floor / Entry"]);
  const submissions = [], actionIds = [];
  const lose = async route => {
    const body = route.request().postDataJSON();
    if (!body.selection) return route.continue();
    submissions.push(body);
    const response = await route.fetch(), reply = await response.json();
    observeChatReply(page, route.request(), reply, response.status(), "lost-response-route");
    expect(response.status()).toBe(200); actionIds.push(reply.action?.id);
    if (submissions.length === 1) await route.abort("failed"); else await route.fulfill({ response });
  };
  await page.route("**/chat", lose);
  await turn(page, initial.reply).getByRole("button", { name: "Use Ground floor / Entry", exact: true }).click();
  await expect(chat(page)).toContainText("The reply was interrupted", { timeout: 90000 });
  await expect(page.getByRole("button", { name: "Confirm change", exact: true })).toHaveCount(0);
  await chat(page).getByRole("button", { name: "Check and retry this turn", exact: true }).click();
  await oneConfirmation(page);
  expect(submissions).toHaveLength(2); expect(submissions[1]).toEqual(submissions[0]);
  expect(submissions[0].selection.candidateId).toBe(candidate.id);
  expect(actionIds[0]).toBeTruthy(); expect(actionIds[1]).toBe(actionIds[0]);
  await page.unroute("**/chat", lose);
  await page.reload(); await oneConfirmation(page);
  expect(new URL(page.url()).searchParams.get("action")).toBe(actionIds[0]);
  await review(page).getByRole("button", { name: "Cancel change", exact: true }).click();
  await expect(review(page)).toContainText("Change cancelled");
  await review(page).getByRole("button", { name: "Done", exact: true }).click();
  const destinationQuestion = "Could you show the storage locations I can use?";
  const destinationParaphrase = await ask(page, destinationQuestion);
  expect(paths(destinationParaphrase.reply)).toContain("Ground floor / Entry");
  expect(paths(destinationParaphrase.reply)).toContain("Upper floor / Entry");
  const addQuestion = "Please prepare a new item called paraphrase lamp for the Ground floor / Entry location.";
  let addParaphrase = await ask(page, addQuestion);
  if (addParaphrase.reply.clarification?.slot === "destination") {
    const exact = addParaphrase.reply.clarification.candidates.find(item => item.entityId === oracle.locations["Ground floor / Entry"]);
    expect(exact).toBeTruthy();
    addParaphrase = await requestTurn(page, () => turn(page, addParaphrase.reply).getByRole("button", { name: `Use ${exact.path}`, exact: true }).click(), body => !!body.selection);
  }
  addParaphrase = await maybeReview(page, addParaphrase);
  expect(addParaphrase.reply.action.payload.name).toMatch(/^paraphrase lamp$/i);
  expect(addParaphrase.reply.action.payload.locationId).toBe(oracle.locations["Ground floor / Entry"]);
  await oneConfirmation(page);
  await review(page).getByRole("button", { name: "Cancel change", exact: true }).click();
  await expect(review(page)).toContainText("Change cancelled");
  await review(page).getByRole("button", { name: "Done", exact: true }).click();
  const paraphrases = [
    { question: destinationQuestion, outcome: destinationParaphrase.reply.outcome, nonFastPathSource: "assistant/conversations.py local_intent returns None; dispatch calls vision.intent" },
    { question: addQuestion, outcome: addParaphrase.reply.outcome, cancelledWithoutConfirmation: true, nonFastPathSource: "assistant/conversations.py local_intent returns None; dispatch calls vision.intent" },
  ];
  const firstContext = (await scoped(page)).session.scope.contextVersion;
  let release, fetched, delayedReply;
  const gate = new Promise(resolve => { release = resolve; });
  const ready = new Promise(resolve => { fetched = resolve; });
  let held = false;
  await page.route("**/chat", async route => {
    if (held || route.request().headers()["x-collection-id"] !== oracle.primaryCollection.id) return route.continue();
    held = true;
    const response = await route.fetch(); delayedReply = await response.json(); fetched();
    observeChatReply(page, route.request(), delayedReply, response.status(), "delayed-scope-route");
    await gate; await route.fulfill({ response }).catch(() => {});
  });
  await page.getByLabel("Ask your inventory", { exact: true }).fill("Which destinations do I have access to?");
  await page.getByLabel("Ask your inventory", { exact: true }).press("Enter");
  await ready;
  await page.getByLabel("Collection", { exact: true }).selectOption(oracle.secondaryCollection.id);
  await expect(page.getByRole("heading", { name: oracle.secondaryCollection.name, exact: true })).toBeVisible();
  const empty = await ask(page, "Which inventory do I have?");
  expect(empty.reply.overview).toMatchObject({ itemRecordCount: 0, locationCount: 0 });
  await chat(page).screenshot({ path: "/results/real-chat-desktop-empty-collection.png" });
  await page.getByLabel("Collection", { exact: true }).selectOption(oracle.primaryCollection.id);
  await expect(chat(page).getByRole("button", { name: "Show inventory", exact: true })).toBeEnabled();
  release();
  await expect(turn(page, delayedReply)).toHaveCount(0);
  const countQuestion = "How many inventory records are in this collection?";
  const overview = await ask(page, countQuestion);
  expect(overview.reply.overview).toMatchObject({ itemRecordCount: 26, locationCount: 13 });
  expect(overview.reply.scope.contextVersion).not.toBe(firstContext);
  await chat(page).screenshot({ path: "/results/real-chat-desktop-returned-collection.png" });
  expect((await records(page)).map(item => item.id).sort()).toEqual(before.map(item => item.id).sort());
  expect(await page.evaluate(() => Object.keys(localStorage).length + Object.keys(sessionStorage).length)).toBe(0);
  await checkpoint(page, "recovery-scope-complete");
  paraphrases.push({ question: countQuestion, outcome: overview.reply.outcome, overview: overview.reply.overview, nonFastPathSource: "assistant/conversations.py local_intent returns None; dispatch calls vision.intent" });
  evidence("real-recovery-scope", { asset, submissions: submissions.length, sameTurnBody: true, sameActionId: true, noInventoryWrites: true, oldResponseRejected: true, primaryRecords: 26, emptyRecords: 0, paraphrases });
});

test("real Haiku recognizes a pixel-only photo marker through the durable browser workflow", async ({ page }) => {
  test.skip(!credentials || !oracle || !process.env.CONVERSATION_QA_IMAGE_FILE, "Requires protected disposable QA and synthetic image.");
  test.setTimeout(240000);
  const asset = await login(page);
  const before = await records(page);
  let actionWrites = 0;
  page.on("request", request => { if (request.method() !== "GET" && /\/actions(?:\/|$)/.test(new URL(request.url()).pathname)) actionWrites++; });
  await page.getByRole("button", { name: "Add an item", exact: true }).click();
  await page.getByLabel("Take or choose a photo").setInputFiles({ name: "image.jpg", mimeType: "image/jpeg", buffer: readFileSync(process.env.CONVERSATION_QA_IMAGE_FILE) });
  await expect(page.getByLabel("Optional hint", { exact: true })).toHaveValue("");
  let release;
  const manual = new Promise(resolve => { release = resolve; });
  await page.route("**/capture-jobs/*", async route => {
    const response = await route.fetch(), job = await response.json();
    if (job.status === "succeeded") await manual;
    await route.fulfill({ response });
  });
  const accepted = page.waitForResponse(response => response.url().endsWith("/capture-jobs") && response.request().method() === "POST", { timeout: 30000 });
  const finished = page.waitForResponse(async response => /\/capture-jobs\/[^/]+$/.test(new URL(response.url()).pathname)
    && response.request().method() === "GET" && ["succeeded", "manual", "failed"].includes((await response.json()).status), { timeout: 180000 });
  const started = Date.now();
  await page.getByRole("button", { name: "Suggest details from photo", exact: true }).click();
  const queued = await accepted, acknowledgementMs = Date.now() - started;
  expect(queued.status()).toBe(202);
  const acceptedJob = await queued.json();
  await page.getByLabel("Manufacturer", { exact: true }).fill("QA browser manual maker"); release();
  const job = await (await finished).json(), elapsedMs = Date.now() - started;
  expect(job.id).toBe(acceptedJob.id); expect(job.status).toBe("succeeded");
  const recognized = JSON.stringify(job.result.draft);
  expect(recognized.replace(/[^a-z0-9]/gi, "").toUpperCase()).toContain("HBV482731");
  expect(recognized).toMatch(/blue/i); expect(recognized).toMatch(/box|bin|tote/i);
  await expect(page.getByLabel("Item name", { exact: true })).toHaveValue(job.result.draft.name, { timeout: 10000 });
  await expect(page.getByLabel("Manufacturer", { exact: true })).toHaveValue("QA browser manual maker");
  await page.screenshot({ path: "/results/real-haiku-image-reviewed-no-write.png", fullPage: true });
  expect((await records(page)).map(item => [item.id, item.quantity]).sort()).toEqual(before.map(item => [item.id, item.quantity]).sort());
  expect(actionWrites).toBe(0);
  expect(await page.evaluate(() => Object.keys(localStorage).length + Object.keys(sessionStorage).length)).toBe(0);
  await checkpoint(page, "photo-complete");
  evidence("real-haiku-image", { asset, acknowledgementMs, elapsedMs, serverElapsedMs: Date.parse(job.finishedAt) - Date.parse(job.startedAt), marker: "HBV482731", recognizedDraft: job.result.draft, manualEditRetained: true, inventoryWriteCount: actionWrites, uploadFilename: "image.jpg", hint: "" });
});
