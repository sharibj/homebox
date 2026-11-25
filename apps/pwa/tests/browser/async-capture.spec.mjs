import { test, expect } from "@playwright/test";
const photo = "/workspace/public/icon-192.png";
const progress = page => page.getByRole("region", { name: "Photo progress" });
async function login(page, path = "/") {
  await page.goto(path);
  await page.getByLabel("Email or username").fill("demo@example.test");
  await page.getByLabel("Password", { exact: true }).fill("disposable-test-only");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("button", { name: "Add an item", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "Add an item", exact: true }).click();
}
async function upload(page, hint) {
  await page.getByLabel("Take or choose a photo").setInputFiles(photo);
  await page.getByLabel("Optional hint").fill(hint);
  await page.getByRole("button", { name: "Suggest details from photo", exact: true }).click();
  await expect(progress(page)).toBeVisible();
}
test("a real 184-second job uses short polls and keeps edits without a second upload", async ({ page }) => {
  test.setTimeout(225000);
  let uploads = 0, writes = 0, legacy = 0, reads = 0;
  const starts = new Map(), durations = [];
  page.on("request", request => {
    if (request.url().endsWith("/capture-jobs") && request.method() === "POST") uploads++;
    if (request.url().endsWith("/captures")) legacy++;
    if (request.url().includes("/actions") && request.method() === "POST") writes++;
    if (/\/capture-jobs\/[^/?]+$/.test(request.url()) && request.method() === "GET") {
      reads++; starts.set(request, Date.now());
    }
  });
  page.on("requestfinished", request => {
    if (starts.has(request)) { durations.push(Date.now() - starts.get(request)); starts.delete(request); }
  });
  await login(page);
  const started = Date.now();
  await upload(page, "slow-184");
  await expect(progress(page).getByRole("heading", { name: "Analyzing photo", exact: true })).toBeVisible();
  await page.getByLabel("Item name", { exact: true }).fill("My edited lantern");
  await page.getByLabel("Quantity", { exact: true }).fill("7");
  await page.getByLabel("Manufacturer", { exact: true }).fill("My maker");
  await page.getByLabel("Model", { exact: true }).fill("Temporary edit");
  await page.getByLabel("Model", { exact: true }).fill("");
  await expect(page.getByRole("button", { name: "Review new item", exact: true })).toBeDisabled();
  await expect(page.getByLabel("Notes", { exact: true })).toBeEnabled();
  await expect(progress(page).getByRole("heading", { name: "Photo ready to review", exact: true })).toBeVisible({ timeout: 200000 });
  const elapsed = Date.now() - started;
  expect(elapsed).toBeGreaterThanOrEqual(184000);
  expect(uploads).toBe(1); expect(legacy).toBe(0); expect(writes).toBe(0);
  expect(reads).toBeGreaterThan(80);
  expect(Math.max(...durations)).toBeLessThan(15000);
  await expect(page.getByLabel("Item name", { exact: true })).toHaveValue("My edited lantern");
  await expect(page.getByLabel("Quantity", { exact: true })).toHaveValue("7");
  await expect(page.getByLabel("Manufacturer", { exact: true })).toHaveValue("My maker");
  await expect(page.getByLabel("Model", { exact: true })).toHaveValue("");
  await expect(page.getByLabel("Category suggestion", { exact: true })).toHaveValue("Camping");
  await expect(page.getByRole("button", { name: "Review new item", exact: true })).toBeEnabled();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: "/results/async-184s-preserved-edits.png", fullPage: true });
  console.log(`Slow-job evidence: elapsed=${elapsed}ms uploads=${uploads} legacy=${legacy} inventoryWrites=${writes} polls=${reads} slowestPoll=${Math.max(...durations)}ms`);
});
test("a lost submission response recovers the exact key after reload without another POST", async ({ page }) => {
  let count = 0, key = "", jobId = "";
  await page.route("**/capture-jobs", async route => {
    if (route.request().method() !== "POST") return route.continue();
    count++; key = route.request().headers()["idempotency-key"];
    const response = await route.fetch(); jobId = (await response.json()).id;
    await route.abort("failed");
  });
  await login(page);
  await upload(page, "delayed");
  await page.goto(`/?captureSubmission=${encodeURIComponent(key)}`);
  await expect(progress(page)).toBeVisible();
  await expect(progress(page).getByRole("heading", { name: "Photo ready to review", exact: true })).toBeVisible({ timeout: 12000 });
  expect(count).toBe(1);
  expect(page.url()).toContain(`captureJob=${jobId}`);
  await page.getByRole("button", { name: "Add an item", exact: true }).click();
  await expect(page.getByLabel("Item name", { exact: true })).toHaveValue("Camping mug");
  await expect(page.getByRole("img", { name: "Selected item photo preview" })).toHaveCount(0);
  await expect(page.getByText(/Your uploaded photo is retained on the server/)).toBeVisible();
  const proposed = page.waitForRequest(request => request.url().endsWith("/actions") && request.method() === "POST");
  await page.getByRole("button", { name: "Review new item", exact: true }).click();
  expect((await proposed).postDataJSON().captureId).toBeTruthy();
});
test("transient status errors keep the same job and preserve manual fields", async ({ page }) => {
  let uploads = 0, failures = 0;
  page.on("request", request => { if (request.url().endsWith("/capture-jobs") && request.method() === "POST") uploads++; });
  await page.route("**/capture-jobs/*", async route => {
    if (failures++ < 2) await route.abort("failed");
    else await route.continue();
  });
  await login(page);
  await upload(page, "delayed");
  await page.getByLabel("Notes", { exact: true }).fill("Keep my notes during a connection issue");
  await expect(progress(page)).toContainText("Nothing was resent");
  await expect(progress(page).getByRole("heading", { name: "Photo ready to review", exact: true })).toBeVisible({ timeout: 14000 });
  await expect(page.getByLabel("Notes", { exact: true })).toHaveValue("Keep my notes during a connection issue");
  expect(uploads).toBe(1);
  await expect(page.getByText("If you confirmed a change", { exact: false })).toHaveCount(0);
});
test("unknown submission does not re-upload and can be dismissed for manual entry", async ({ page }) => {
  let uploads = 0;
  await page.route("**/capture-jobs", async route => {
    if (route.request().method() === "POST") { uploads++; await route.abort("failed"); }
    else await route.continue();
  });
  await login(page);
  await page.getByLabel("Item name", { exact: true }).fill("Manual draft survives");
  await upload(page, "delayed");
  await expect(progress(page)).toContainText("no matching photo job");
  await expect(page.getByLabel("Item name", { exact: true })).toHaveValue("Manual draft survives");
  expect(page.url()).toContain("captureSubmission=");
  await progress(page).getByRole("button", { name: "Stop checking and remove photo", exact: true }).click();
  expect(page.url()).not.toContain("captureSubmission=");
  await expect(page.getByLabel("Item name", { exact: true })).toHaveValue("Manual draft survives");
  await expect(page.getByRole("button", { name: "Review new item", exact: true })).toBeEnabled();
  expect(uploads).toBe(1);
});
test("changing the photo rejects a late prior result and offline fallback keeps the new photo", async ({ page }) => {
  await login(page);
  const firstResponse = page.waitForResponse(response => response.url().endsWith("/capture-jobs") && response.request().method() === "POST");
  await upload(page, "delayed");
  const firstJob = await (await firstResponse).json();
  await page.getByRole("button", { name: "Remove photo", exact: true }).click();
  await upload(page, "offline");
  await page.getByLabel("Item name", { exact: true }).fill("New photo manual name");
  await expect(progress(page).getByRole("heading", { name: "Photo saved for manual review", exact: true })).toBeVisible();
  await expect(page.getByLabel("Item name", { exact: true })).toHaveValue("New photo manual name");
  await expect(page.getByLabel("Manufacturer", { exact: true })).toHaveValue("");
  await expect.poll(async () => {
    const reply = await page.request.get(`/api/assistant/v1/capture-jobs/${firstJob.id}`, { headers: { "X-Collection-ID": "30000000-0000-4000-8000-000000000001" } });
    return (await reply.json()).status;
  }, { timeout: 10000 }).toBe("succeeded");
  await expect(page.getByLabel("Item name", { exact: true })).toHaveValue("New photo manual name");
  await expect(page.getByLabel("Manufacturer", { exact: true })).toHaveValue("");
  await page.getByRole("button", { name: "Review new item", exact: true }).click();
  await expect(page.getByRole("region", { name: "Inventory change confirmation" })).toContainText("New photo manual name");
});
test("collection change stops and discards an in-flight photo result", async ({ page }) => {
  await login(page);
  await upload(page, "delayed");
  await page.getByLabel("Item name", { exact: true }).fill("Private old collection photo");
  await page.getByLabel("Collection", { exact: true }).selectOption("30000000-0000-4000-8000-000000000002");
  await expect(page.getByRole("heading", { name: "Workshop", exact: true })).toBeVisible();
  await expect(progress(page)).toHaveCount(0);
  await expect(page.getByLabel("Item name", { exact: true })).toHaveValue("");
  await expect(page.getByRole("img", { name: "Selected item photo preview" })).toHaveCount(0);
  expect(page.url()).not.toContain("captureJob=");
  await page.getByLabel("Collection", { exact: true }).selectOption("30000000-0000-4000-8000-000000000001");
  await expect(progress(page)).toBeVisible();
  await expect(page.getByLabel("Item name", { exact: true })).toHaveValue("");
});
test("an expired or inaccessible photo URL warns without blocking a fresh session", async ({ page }) => {
  await login(page, "/?captureJob=prior-session-unavailable");
  await expect(progress(page)).toContainText("previous photo job is unavailable");
  expect(page.url()).not.toContain("captureJob=");
  await expect(page.getByLabel("Item name", { exact: true })).toBeEnabled();
  await upload(page, "offline");
  await expect(progress(page).getByRole("heading", { name: "Photo saved for manual review", exact: true })).toBeVisible();
});
test("public provider metadata labels the configured endpoint without exposing secrets", async ({ page }) => {
  await page.route("**/auth/login", async route => {
    const response = await route.fetch(), data = await response.json();
    await route.fulfill({ response, json: { ...data, ai: { provider: "openai-compatible", model: "operator-model", external: true } } });
  });
  await login(page);
  await page.getByRole("button", { name: "AI details", exact: true }).click();
  await expect(page.getByLabel("Configured AI provider")).toContainText("Chat: OpenAI-compatible · operator-model");
  await expect(page.getByLabel("Configured AI provider")).toContainText("Images: OpenAI-compatible · operator-model");
  await expect(page.getByLabel("Configured AI provider")).toContainText("Photos are sent to the image AI endpoint configured by your administrator");
  await expect(page.getByLabel("API key", { exact: true })).toHaveCount(0);
});
test("separate provider roles disclose only the configured photo or chat destination", async ({ page }) => {
  await page.route("**/auth/login", async route => {
    const response = await route.fetch(), data = await response.json();
    await route.fulfill({ response, json: { ...data, ai: {
      chat: { provider: "ollama", model: "chat-only-model" }, vision: { provider: "openai-compatible", model: "image-only-model" },
    } } });
  });
  await login(page);
  await page.getByRole("button", { name: "AI details", exact: true }).click();
  const label = page.getByLabel("Configured AI provider");
  await expect(label).toContainText("Chat: Ollama · chat-only-model");
  await expect(label).toContainText("Images: OpenAI-compatible · image-only-model");
  await expect(label).toContainText("Photos are sent to the image AI endpoint");
  await expect(label).not.toContainText("Chat messages are sent");
});
test("native Anthropic chat is labeled and disclosed without changing Ollama images", async ({ page }) => {
  await page.route("**/auth/login", async route => {
    const response = await route.fetch(), data = await response.json();
    await route.fulfill({ response, json: { ...data, ai: {
      chat: { provider: "anthropic-messages", model: "claude-chat" }, vision: { provider: "ollama", model: "vision-unchanged" },
    } } });
  });
  await login(page);
  await page.getByRole("button", { name: "AI details", exact: true }).click();
  const label = page.getByLabel("Configured AI provider");
  await expect(label).toContainText("Chat: Anthropic Messages · claude-chat");
  await expect(label).toContainText("Images: Ollama · vision-unchanged");
  await expect(label).toContainText("Chat messages are sent to the chat AI endpoint configured by your administrator");
  await expect(label).not.toContainText("Photos are sent");
  await expect(label).not.toContainText("local");
  await expect(page.getByLabel("API key", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: "/results/native-anthropic-chat-label.png", fullPage: true });
});
test("unknown provider metadata is not mislabeled as a known or local provider", async ({ page }) => {
  await page.route("**/auth/login", async route => {
    const response = await route.fetch(), data = await response.json();
    await route.fulfill({ response, json: { ...data, ai: {
      chat: { provider: "future-provider", model: "future-chat" }, vision: { provider: "ollama", model: "vision-unchanged" },
    } } });
  });
  await login(page);
  await page.getByRole("button", { name: "AI details", exact: true }).click();
  const label = page.getByLabel("Configured AI provider");
  await expect(label).toContainText("Chat: Configured AI provider · future-chat");
  await expect(label).toContainText("Chat messages are sent to the chat AI endpoint");
  await expect(label).not.toContainText("OpenAI-compatible");
  await expect(label).not.toContainText("local");
});
test("Gemini vision metadata names its model and discloses photos without changing Ollama chat", async ({ page }) => {
  await page.route("**/auth/login", async route => {
    const response = await route.fetch(), data = await response.json();
    await route.fulfill({ response, json: { ...data, ai: {
      chat: { provider: "ollama", model: "chat-unchanged" }, vision: { provider: "gemini", model: "gemini-3.1-flash-lite" },
    } } });
  });
  await login(page);
  await page.getByRole("button", { name: "AI details", exact: true }).click();
  const label = page.getByLabel("Configured AI provider");
  await expect(label).toContainText("Chat: Ollama · chat-unchanged");
  await expect(label).toContainText("Images: Gemini · gemini-3.1-flash-lite");
  await expect(label).toContainText("Photos are sent to the image AI endpoint configured by your administrator");
  await expect(label).not.toContainText("Chat messages are sent");
  await expect(page.getByLabel("API key", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: "/results/native-gemini-vision-metadata.png", fullPage: true });
});
test("a content-key conflict cannot recover a different photo as this upload", async ({ page }) => {
  let keyLookups = 0;
  page.on("request", request => { if (request.url().includes("/capture-jobs?submissionKey=")) keyLookups++; });
  await page.route("**/capture-jobs", async route => {
    if (route.request().method() === "POST") {
      await route.fulfill({ status: 409, json: { error: { code: "idempotency_conflict", message: "This submission key belongs to different photo content." } } });
    } else await route.continue();
  });
  await login(page);
  await page.getByLabel("Item name", { exact: true }).fill("Keep my photo fields");
  await page.getByLabel("Take or choose a photo").setInputFiles(photo);
  await page.getByRole("button", { name: "Suggest details from photo", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("different photo content");
  await expect(page.getByLabel("Item name", { exact: true })).toHaveValue("Keep my photo fields");
  await expect(page.getByRole("img", { name: "Selected item photo preview" })).toBeVisible();
  expect(keyLookups).toBe(0);
  expect(page.url()).not.toContain("captureSubmission=");
});
test("an authentication failure during polling clears the old private photo state", async ({ page }) => {
  await login(page);
  await page.route("**/capture-jobs/*", async route => {
    await route.fulfill({ status: 401, json: { error: { code: "session_expired", message: "Session ended." } } });
  });
  await page.getByLabel("Item name", { exact: true }).fill("Private photo draft");
  await page.getByLabel("Take or choose a photo").setInputFiles(photo);
  await page.getByRole("button", { name: "Suggest details from photo", exact: true }).click();
  await expect(page.getByRole("heading", { name: "Welcome home", exact: true })).toBeVisible();
  await expect(progress(page)).toHaveCount(0);
  await expect(page.getByRole("img", { name: "Selected item photo preview" })).toHaveCount(0);
  expect(page.url()).not.toContain("captureJob=");
});
