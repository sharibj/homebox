import { test, expect } from "@playwright/test";
test.use({ trace: "off", screenshot: "off" });
const path = "/api/assistant/v1/settings/ai";
const original = () => ({ revision: 0,
  chat: { provider: "openai-compatible", baseUrl: "https://chat.example.test/v1", model: "custom-chat", keyConfigured: true },
  vision: { provider: "ollama", baseUrl: "http://ollama:11434", model: "custom-vision", keyConfigured: false },
});
const nav = page => page.getByRole("button", { name: "AI settings", exact: true });
const save = page => page.getByRole("button", { name: "Save both roles", exact: true });
const keyAction = (page, role = "Chat") => page.getByLabel(`${role} API key action`, { exact: true });
const keyInput = (page, role = "Chat") => page.getByLabel(`${role} new API key`, { exact: true });
async function fixture(page, options = {}) {
  const state = { current: original(), gets: 0, puts: [], requests: [], pending: null };
  for (const endpoint of ["/session", "/auth/login", "/session/collection"]) {
    await page.route(`**/api/assistant/v1${endpoint}`, async route => {
      const response = await route.fetch(), data = await response.json();
      if (!response.ok()) return route.fulfill({ response });
      const json = { ...data, capabilities: options.absent ? undefined : { canManageAISettings: options.manage !== false },
        user: { ...data.user, isSuperuser: true }, isOwner: true };
      if (options.noCollection) { json.collections = []; json.collectionId = ""; }
      await route.fulfill({ response, json });
    });
  }
  if (options.noCollection) {
    await page.route("**/api/assistant/v1/actions", route => route.fulfill({ json: { actions: [] } }));
    await page.route("**/api/assistant/v1/locations", route => route.fulfill({ status: 503, json: { error: { code: "unavailable", message: "Synthetic recovery unavailable" } } }));
  }
  await page.route(`**${path}`, async route => {
    state.requests.push({ method: route.request().method(), headers: route.request().headers() });
    if (options.forbidden) return route.fulfill({ status: 403, json: { error: { code: "AI_SETTINGS_FORBIDDEN" } } });
    if (route.request().method() === "GET") { state.gets++; return options.get ? options.get(route, state) : route.fulfill({ json: state.current }); }
    const body = route.request().postDataJSON(); state.puts.push(body);
    if (options.put) return options.put(route, state, body);
    state.current = { revision: state.current.revision + 1 };
    for (const name of ["chat", "vision"]) state.current[name] = {
      provider: body[name].provider, baseUrl: body[name].baseUrl, model: body[name].model,
      keyConfigured: body[name].keyAction === "replace" || (body[name].keyAction === "keep" && original()[name].keyConfigured),
    };
    await route.fulfill({ json: state.current });
  });
  await page.goto(options.direct ? "/?page=ai-settings" : "/");
  await page.getByLabel("Email or username").fill("demo@example.test");
  await page.getByLabel("Password", { exact: true }).fill("disposable-test-only");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible();
  return state;
}
async function open(page) { await nav(page).click(); await expect(page.getByLabel("Chat model ID", { exact: true })).toHaveValue("custom-chat"); }
async function discardDialog(page) {
  const dialog = page.getByRole("dialog", { name: "Discard unsaved AI settings?", exact: true });
  await expect(dialog).toBeVisible(); return dialog;
}
test("nonprimary and older sessions have no tab and cannot open a direct settings page", async ({ page }) => {
  const state = await fixture(page, { manage: false, direct: true });
  await expect(nav(page)).toHaveCount(0);
  await expect(page.getByRole("heading", { name: "AI settings unavailable" })).toBeVisible();
  expect(state.gets).toBe(0); expect(state.puts).toHaveLength(0);
});
test("a missing capability never grants management to an owner or superuser", async ({ page }) => {
  const state = await fixture(page, { absent: true });
  await expect(page.getByRole("button", { name: "Find & ask", exact: true })).toBeEnabled();
  await expect(nav(page)).toHaveCount(0); expect(state.gets).toBe(0);
});
test("a direct page handles server 403 without exposing the editor or keeping the tab", async ({ page }) => {
  const state = await fixture(page, { direct: true, forbidden: true });
  await expect(page.getByRole("heading", { name: "AI settings unavailable" })).toBeVisible();
  await expect(nav(page)).toHaveCount(0); await expect(page.getByLabel("Chat model ID", { exact: true })).toHaveCount(0);
  expect(state.puts).toHaveLength(0);
});
test("primary edits stay local until an atomic Save, keys are write-only and metadata refreshes after success", async ({ page }) => {
  const state = await fixture(page); await open(page);
  await expect(save(page)).toBeDisabled(); expect(state.current.revision).toBe(0);
  expect(await page.getByLabel("Chat provider", { exact: true }).locator("option").allTextContents()).toEqual(["Ollama", "OpenAI-compatible", "Anthropic Messages", "Gemini"]);
  await page.getByLabel("Chat model ID", { exact: true }).fill("custom/exact:model-2026");
  await page.getByLabel("Chat base URL", { exact: true }).fill("https://new-chat.example.test/full/v1");
  await keyAction(page).selectOption("replace");
  const secret = "synthetic-write-only-not-a-real-credential";
  await expect(keyInput(page)).toHaveAttribute("type", "password"); await expect(save(page)).toBeDisabled();
  await keyInput(page).fill(secret);
  await page.getByLabel("Images provider", { exact: true }).selectOption("gemini");
  await page.getByLabel("Images base URL", { exact: true }).fill("https://images.example.test/api");
  await page.getByLabel("Images model ID", { exact: true }).fill("custom-image-exact");
  await expect(save(page)).toBeDisabled(); await keyAction(page, "Images").selectOption("clear");
  expect(state.puts).toHaveLength(0); expect(state.current).toEqual(original());
  const storage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage } }));
  expect(storage).not.toContain(secret); expect(storage).not.toContain("custom/exact:model-2026");
  await save(page).click();
  await expect(page.getByText("AI settings saved.", { exact: false })).toBeVisible();
  expect(state.puts).toHaveLength(1);
  expect(state.puts[0]).toEqual({ revision: 0,
    chat: { provider: "openai-compatible", baseUrl: "https://new-chat.example.test/full/v1", model: "custom/exact:model-2026", keyAction: "replace", apiKey: secret },
    vision: { provider: "gemini", baseUrl: "https://images.example.test/api", model: "custom-image-exact", keyAction: "clear" },
  });
  expect(state.requests.at(-1).headers["x-csrf-token"]).toBeTruthy();
  await expect(keyInput(page)).toHaveCount(0); await expect(keyAction(page)).toHaveValue("keep");
  await expect(save(page)).toBeDisabled();
  await page.getByRole("button", { name: "AI details", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "AI details", exact: true })).toContainText("custom/exact:model-2026");
  await page.getByRole("button", { name: "Close AI details", exact: true }).click();
  await keyAction(page).selectOption("replace"); await expect(keyInput(page)).toHaveValue("");
});
test("model-only edits keep the configured key and never send an empty or copied API key", async ({ page }) => {
  const state = await fixture(page); await open(page);
  await page.getByLabel("Chat model ID", { exact: true }).fill("another/exact-model");
  await save(page).click(); await expect(save(page)).toBeDisabled();
  expect(state.puts).toHaveLength(1); expect(state.puts[0].chat.keyAction).toBe("keep");
  expect(state.puts[0].chat).not.toHaveProperty("apiKey"); expect(state.puts[0].vision).not.toHaveProperty("apiKey");
});
test("revision conflict keeps local fields and key, reload requires explicit discard and Escape restores focus", async ({ page }) => {
  const state = await fixture(page, { put: async (route, state) => {
    state.current = { ...original(), revision: 2, chat: { ...original().chat, model: "changed-elsewhere" } };
    await route.fulfill({ status: 409, json: { error: { code: "AI_SETTINGS_CHANGED" } } });
  } }); await open(page);
  await page.getByLabel("Chat model ID", { exact: true }).fill("my-unsaved-model");
  await keyAction(page).selectOption("replace"); await keyInput(page).fill("synthetic-conflict-key");
  await save(page).click(); await expect(page.getByRole("alert")).toContainText("changed elsewhere");
  await expect(keyInput(page)).toHaveValue("synthetic-conflict-key");
  await expect(save(page)).toBeDisabled(); expect(state.gets).toBe(1); expect(state.puts).toHaveLength(1);
  const reload = page.getByRole("button", { name: "Reload saved settings", exact: true });
  await reload.click(); await discardDialog(page); await page.keyboard.press("Escape");
  await expect(reload).toBeFocused(); await expect(keyInput(page)).toHaveValue("synthetic-conflict-key"); expect(state.gets).toBe(1);
  await reload.click(); await (await discardDialog(page)).getByRole("button", { name: "Discard draft", exact: true }).click();
  await expect(page.getByLabel("Chat model ID", { exact: true })).toHaveValue("changed-elsewhere");
  await expect(keyInput(page)).toHaveCount(0); expect(state.gets).toBe(2); expect(state.puts).toHaveLength(1);
});
test("an interrupted Save never retries automatically and reload resolves the unknown outcome", async ({ page }) => {
  const state = await fixture(page, { put: async (route, state, body) => {
    state.current = { ...original(), revision: 1, chat: { ...original().chat, model: body.chat.model } };
    await route.abort("failed");
  } }); await open(page);
  await page.getByLabel("Chat model ID", { exact: true }).fill("saved-before-connection-lost");
  await save(page).click(); await expect(page.getByRole("alert")).toContainText("save could not be verified");
  await expect(save(page)).toBeDisabled(); expect(state.puts).toHaveLength(1); expect(state.gets).toBe(1);
  await page.getByRole("button", { name: "Discard changes", exact: true }).click();
  await (await discardDialog(page)).getByRole("button", { name: "Discard draft", exact: true }).click();
  expect(state.current.chat.model).toBe("saved-before-connection-lost");
  await expect(page.getByText("Draft discarded. Reload saved settings to see what is active.", { exact: true })).toBeVisible();
  await expect(save(page)).toBeDisabled();
  await page.getByLabel("Chat model ID", { exact: true }).fill("cannot-save-before-reload");
  await expect(save(page)).toBeDisabled();
  expect(state.puts).toHaveLength(1);
  await page.getByRole("button", { name: "Reload saved settings", exact: true }).click();
  await (await discardDialog(page)).getByRole("button", { name: "Discard draft", exact: true }).click();
  await expect(page.getByLabel("Chat model ID", { exact: true })).toHaveValue("saved-before-connection-lost");
  expect(state.puts).toHaveLength(1);
});
test("server validation errors keep the draft without echoing error details or saving again", async ({ page }) => {
  const state = await fixture(page, { put: route => route.fulfill({ status: 422, json: { error: { code: "invalid_request", message: "synthetic-private-error-must-not-render" } } }) });
  await open(page); await page.getByLabel("Chat model ID", { exact: true }).fill("local-model");
  await save(page).click(); await expect(page.getByRole("alert")).toContainText("Settings were not saved");
  await expect(page.getByText("synthetic-private-error-must-not-render")).toHaveCount(0);
  await expect(page.getByLabel("Chat model ID", { exact: true })).toHaveValue("local-model"); expect(state.puts).toHaveLength(1);
});
test("auth loss clears key drafts and returns to sign-in without an unsaved prompt", async ({ page }) => {
  await fixture(page, { put: route => route.fulfill({ status: 401, json: { error: { code: "session_expired" } } }) });
  await open(page); await keyAction(page).selectOption("replace"); await keyInput(page).fill("synthetic-auth-loss-key");
  await save(page).click(); await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();
  await expect(keyInput(page)).toHaveCount(0); await expect(page.getByRole("dialog", { name: "Discard unsaved AI settings?", exact: true })).toHaveCount(0);
});
test("a revoked settings permission clears the key editor and management navigation", async ({ page }) => {
  await fixture(page, { put: route => route.fulfill({ status: 403, json: { error: { code: "AI_SETTINGS_FORBIDDEN" } } }) });
  await open(page); await keyAction(page).selectOption("replace"); await keyInput(page).fill("synthetic-revoked-key");
  await save(page).click(); await expect(page.getByRole("heading", { name: "AI settings unavailable" })).toBeVisible();
  await expect(keyInput(page)).toHaveCount(0); await expect(nav(page)).toHaveCount(0);
});
test("tab changes guard unsaved values, keyboard Escape keeps them, discard removes keys on re-entry", async ({ page }) => {
  const state = await fixture(page); await open(page);
  await keyAction(page).selectOption("replace"); await keyInput(page).fill("synthetic-discard-key");
  const find = page.getByRole("button", { name: "Find & ask", exact: true });
  await find.click(); const dialog = await discardDialog(page);
  await expect(dialog.getByRole("button", { name: "Keep editing", exact: true })).toBeFocused();
  await page.keyboard.press("Escape"); await expect(find).toBeFocused(); await expect(keyInput(page)).toHaveValue("synthetic-discard-key");
  await find.click(); await (await discardDialog(page)).getByRole("button", { name: "Discard draft", exact: true }).click();
  await expect(page.getByLabel("Chat model ID", { exact: true })).toHaveCount(0); await open(page);
  await keyAction(page).selectOption("replace"); await expect(keyInput(page)).toHaveValue(""); expect(state.puts).toHaveLength(0);
});
test("settings stay accessible without collections or inventory recovery, while inventory remains disabled", async ({ page }) => {
  const state = await fixture(page, { noCollection: true });
  await expect(page.getByRole("button", { name: "Find & ask", exact: true })).toBeDisabled();
  await expect(nav(page)).toBeEnabled(); await open(page);
  await page.getByLabel("Chat model ID", { exact: true }).fill("no-collection-model"); await save(page).click();
  await expect(page.getByText("AI settings saved.", { exact: false })).toBeVisible();
  expect(state.puts).toHaveLength(1); expect(state.requests.at(-1).headers).not.toHaveProperty("x-collection-id");
});
test("offline entry stays available, fields and Save wait for reconnect and explicit load", async ({ page, context }) => {
  const state = await fixture(page); await expect(nav(page)).toBeEnabled();
  await context.setOffline(true); await expect(page.getByText("You’re offline.", { exact: true })).toBeVisible();
  await nav(page).click(); await expect(page.getByRole("heading", { name: "AI settings", exact: true })).toBeVisible();
  expect(state.gets).toBe(0); await expect(page.getByRole("button", { name: "Load saved settings", exact: true })).toBeDisabled();
  await context.setOffline(false); await page.getByRole("button", { name: "Load saved settings", exact: true }).click();
  await expect(page.getByLabel("Chat model ID", { exact: true })).toHaveValue("custom-chat");
});
test("loading and in-flight Save disable editing and duplicate saves, navigation waits for the result", async ({ page }) => {
  let releaseLoad, releaseSave;
  const loaded = new Promise(resolve => { releaseLoad = resolve; }), saved = new Promise(resolve => { releaseSave = resolve; });
  const state = await fixture(page, {
    get: async (route, state) => { await loaded; await route.fulfill({ json: state.current }); },
    put: async (route, state, body) => {
      await saved; state.current = { ...state.current, revision: 1, chat: { ...state.current.chat, model: body.chat.model } };
      await route.fulfill({ json: state.current });
    },
  });
  await nav(page).click(); await expect(page.getByText("Loading saved AI settings…", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Load saved settings", exact: true })).toBeDisabled();
  releaseLoad(); await expect(page.getByLabel("Chat model ID", { exact: true })).toHaveValue("custom-chat");
  await page.getByLabel("Chat model ID", { exact: true }).fill("in-flight-model"); await save(page).click();
  await expect(page.getByText("Saving both roles…", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Chat model ID", { exact: true })).toBeDisabled(); await expect(save(page)).toBeDisabled();
  await page.getByRole("button", { name: "Find & ask", exact: true }).click();
  await expect(nav(page)).toHaveAttribute("aria-current", "page");
  await page.getByRole("button", { name: "Sign out", exact: true }).click();
  await expect(page.getByRole("button", { name: "Sign out", exact: true })).toBeVisible(); expect(state.puts).toHaveLength(1);
  releaseSave(); await expect(page.getByText("AI settings saved.", { exact: false })).toBeVisible();
});
for (const destination of ["Sign out", "Homebox Companion home"]) {
  test(`${destination} asks before discarding and never saves a key draft`, async ({ page }) => {
    const state = await fixture(page); await open(page); await keyAction(page).selectOption("replace"); await keyInput(page).fill("synthetic-leave-key");
    const target = destination === "Sign out" ? page.getByRole("button", { name: destination, exact: true }) : page.getByRole("link", { name: destination, exact: true });
    await target.click(); await (await discardDialog(page)).getByRole("button", { name: "Keep editing", exact: true }).click();
    await expect(keyInput(page)).toHaveValue("synthetic-leave-key");
    await target.click(); await (await discardDialog(page)).getByRole("button", { name: "Discard draft", exact: true }).click();
    await expect(keyInput(page)).toHaveCount(0); expect(state.puts).toHaveLength(0);
    if (destination === "Sign out") await expect(page.getByRole("button", { name: "Sign in", exact: true })).toBeVisible();
    else { await expect(nav(page)).toBeVisible(); await open(page); await keyAction(page).selectOption("replace"); await expect(keyInput(page)).toHaveValue(""); }
  });
}
test("native unload warns for drafts, cancellation keeps edits, accepted reload has no retained key", async ({ page }) => {
  const state = await fixture(page); await open(page); await keyAction(page).selectOption("replace"); await keyInput(page).fill("synthetic-unload-key");
  const dialog = page.waitForEvent("dialog"); const reload = page.reload({ timeout: 2000 }).catch(() => {});
  const warning = await dialog; expect(warning.type()).toBe("beforeunload"); await warning.dismiss(); await reload;
  await expect(keyInput(page)).toHaveValue("synthetic-unload-key"); expect(state.puts).toHaveLength(0);
  page.once("dialog", item => item.accept()); await page.reload();
  await expect(page.getByLabel("Chat model ID", { exact: true })).toHaveValue("custom-chat");
  await keyAction(page).selectOption("replace"); await expect(keyInput(page)).toHaveValue(""); expect(state.puts).toHaveLength(0);
});
test("pagehide clears memory before a page-cache restore and pageshow reloads only redacted settings", async ({ page }) => {
  const state = await fixture(page); await open(page); await keyAction(page).selectOption("replace"); await keyInput(page).fill("synthetic-page-cache-key");
  await page.getByRole("button", { name: "Find & ask", exact: true }).click(); await discardDialog(page);
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pagehide", { persisted: true })));
  await expect(keyInput(page)).toHaveCount(0);
  await expect(page.getByRole("dialog", { name: "Discard unsaved AI settings?", exact: true, includeHidden: true })).not.toHaveAttribute("open");
  await page.evaluate(() => window.dispatchEvent(new PageTransitionEvent("pageshow", { persisted: true })));
  await expect(page.getByLabel("Chat model ID", { exact: true })).toHaveValue("custom-chat");
  await expect(nav(page)).toHaveAttribute("aria-current", "page");
  await keyAction(page).selectOption("replace"); await expect(keyInput(page)).toHaveValue(""); expect(state.gets).toBe(2); expect(state.puts).toHaveLength(0);
});
test("mobile settings have large controls, no overflow, keyboard navigation and a compact default header", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 780 }); await fixture(page); await open(page);
  await expect(nav(page)).toHaveAttribute("aria-current", "page");
  for (const label of ["Chat provider", "Chat model ID", "Chat base URL", "Chat API key action"]) {
    const input = page.getByLabel(label, { exact: true }); expect((await input.boundingBox()).height).toBeGreaterThanOrEqual(44);
  }
  await page.getByLabel("Chat provider", { exact: true }).focus(); await page.keyboard.press("Tab");
  await expect(page.getByLabel("Chat model ID", { exact: true })).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: "/results/ai-settings-mobile-360.png", fullPage: true });
  await page.setViewportSize({ width: 320, height: 740 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.setViewportSize({ width: 1100, height: 850 });
  await page.screenshot({ path: "/results/ai-settings-desktop.png", fullPage: true });
});
