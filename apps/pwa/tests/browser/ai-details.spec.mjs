import { test, expect } from "@playwright/test";

test.use({ hasTouch: true });

const shared = { provider: "anthropic-messages", model: "claude-haiku-4-5" };
async function login(page, ai) {
  await page.route("**/auth/login", async route => {
    const response = await route.fetch(), data = await response.json();
    await route.fulfill({ response, json: { ...data, ai } });
  });
  await page.goto("/");
  await page.getByLabel("Email or username").fill("demo@example.test");
  await page.getByLabel("Password", { exact: true }).fill("disposable-test-only");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByRole("button", { name: "Find & ask", exact: true })).toBeEnabled();
  return page.getByRole("dialog", { name: "AI details", exact: true, includeHidden: true });
}

const trigger = page => page.getByRole("button", { name: "AI details", exact: true });
const noOverflow = page => page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth);

test("AI details: compact desktop header hides models and supports keyboard, close and backdrop dismissal", async ({ page }) => {
  await page.setViewportSize({ width: 1100, height: 850 });
  const details = await login(page, { chat: shared, vision: shared });
  const button = trigger(page), close = details.getByRole("button", { name: "Close AI details", exact: true });
  await expect(page.locator(".app-header").getByRole("button", { name: "AI details", exact: true })).toBeVisible();
  await expect(button).toHaveText("AI details");
  await expect(button).toHaveAttribute("aria-haspopup", "dialog");
  await expect(button).toHaveAttribute("aria-expanded", "false");
  await expect(details).not.toHaveAttribute("open");
  await expect(details).toBeHidden();
  await expect(page.locator(".ai-models, main > .ai-details")).toHaveCount(0);
  const bounds = await button.boundingBox(), collection = await page.locator(".collection-bar").boundingBox(), nav = await page.getByRole("navigation", { name: "Companion tools" }).boundingBox();
  expect(bounds.height).toBeGreaterThanOrEqual(44);
  expect(bounds.width).toBeLessThan(100);
  expect(nav.y - collection.y - collection.height).toBeLessThanOrEqual(32);
  expect(await noOverflow(page)).toBe(true);
  await page.screenshot({ path: "/results/ai-details-desktop-default.png", fullPage: false });

  await button.focus();
  await page.keyboard.press("Enter");
  await expect(details).toHaveAttribute("open", "");
  await expect(button).toHaveAttribute("aria-expanded", "true");
  await expect(details.getByRole("heading", { name: "AI details", exact: true })).toBeVisible();
  await expect(close).toBeFocused();
  expect(await details.evaluate(element => element.matches(":modal"))).toBe(true);
  await expect(details.locator(".ai-disclosure")).toBeVisible();
  await expect(details).toContainText("Chat: Anthropic Messages · claude-haiku-4-5");
  await expect(details).toContainText("Images: Anthropic Messages · claude-haiku-4-5");
  await expect(details).toContainText("Chat messages are sent to the chat AI endpoint configured by your administrator.");
  await expect(details).toContainText("Photos are sent to the image AI endpoint configured by your administrator.");
  await page.screenshot({ path: "/results/ai-details-desktop-open.png", fullPage: false });
  await page.keyboard.press("Tab");
  await expect(page.getByLabel("Collection", { exact: true })).not.toBeFocused();
  await page.keyboard.press("Shift+Tab");
  await expect(close).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(details).toBeHidden();
  await expect(button).toBeFocused();
  await expect(button).toHaveAttribute("aria-expanded", "false");
  await page.keyboard.press("Space");
  await expect(details).toBeVisible();
  await close.click();
  await expect(details).toBeHidden();
  await expect(button).toBeFocused();
  await button.click();
  const panel = await details.boundingBox();
  await page.mouse.click(panel.x + 6, panel.y + 6);
  await expect(details).toBeVisible();
  await page.mouse.click(8, 8);
  await expect(details).toBeHidden();
  await expect(button).toBeFocused();
});

test("AI details: different providers remain distinct even when their model names match", async ({ page }) => {
  const details = await login(page, {
    chat: { provider: "ollama", model: "operator-model" },
    vision: { provider: "gemini", model: "operator-model" },
  });
  await expect(trigger(page)).toHaveText("AI details");
  await trigger(page).click();
  await expect(details).toContainText("Chat: Ollama · operator-model");
  await expect(details).toContainText("Images: Gemini · operator-model");
  await expect(details).not.toContainText("Chat messages are sent");
  await expect(details).toContainText("Photos are sent to the image AI endpoint");
});

test("AI details: long distinct models wrap at 320px without changing the compact header", async ({ page }) => {
  await page.setViewportSize({ width: 320, height: 740 });
  const chat = `chat-${"long-model-name-".repeat(16)}`;
  const vision = `vision-${"different-model-name-".repeat(16)}`;
  const details = await login(page, {
    chat: { provider: "anthropic-messages", model: chat },
    vision: { provider: "gemini", model: vision },
  });
  expect((await trigger(page).boundingBox()).width).toBeLessThan(100);
  expect(await noOverflow(page)).toBe(true);
  await expect(trigger(page)).toHaveText("AI details");
  await trigger(page).tap();
  await expect(details.locator(".ai-disclosure")).toBeVisible();
  await expect(details).toContainText(`Chat: Anthropic Messages · ${chat}`);
  await expect(details).toContainText(`Images: Gemini · ${vision}`);
  expect(await noOverflow(page)).toBe(true);
  expect(await details.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
  await details.getByRole("button", { name: "Close AI details", exact: true }).tap();
  await expect(trigger(page)).toBeFocused();
});

test("AI details: 360px default prioritizes collection and navigation without a model row", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 780 });
  const details = await login(page, { chat: shared, vision: shared });
  await expect(details).toBeHidden();
  expect((await trigger(page).boundingBox()).height).toBeGreaterThanOrEqual(44);
  expect(await noOverflow(page)).toBe(true);
  await page.evaluate(() => { document.activeElement?.blur(); window.scrollTo(0, 0); });
  await page.screenshot({ path: "/results/ai-details-mobile-default.png", fullPage: false });
  await trigger(page).tap();
  await expect(details.locator(".ai-disclosure")).toBeVisible();
  expect(await noOverflow(page)).toBe(true);
  await page.screenshot({ path: "/results/ai-details-mobile-open.png", fullPage: false });
  await page.keyboard.press("Escape");
  await expect(trigger(page)).toBeFocused();
  await page.getByRole("button", { name: "Add an item", exact: true }).tap();
  await expect(page.getByRole("heading", { name: "Start with a photo. Or just the details." })).toBeVisible();
  await page.getByRole("button", { name: "Places", exact: true }).tap();
  await expect(page.getByRole("heading", { name: "Look inside." })).toBeVisible();
});

for (const role of ["chat", "vision"]) {
  test(`AI details: ${role}-only metadata never invents the other role`, async ({ page }) => {
    const details = await login(page, { [role]: shared });
    const label = role === "chat" ? "Chat" : "Images";
    const absent = role === "chat" ? "Images:" : "Chat:";
    await trigger(page).click();
    await expect(details).toContainText(`${label}: Anthropic Messages · claude-haiku-4-5`);
    await expect(details).not.toContainText(absent);
  });
}

for (const ai of [undefined, {}, { provider: "ollama" }]) {
  test(`AI details: missing metadata omits the control and disclosure: ${JSON.stringify(ai)}`, async ({ page }) => {
    await login(page, ai);
    await expect(trigger(page)).toHaveCount(0);
    await expect(page.getByLabel("Configured AI provider")).toHaveCount(0);
  });
}

test("AI details: missing role model is labeled honestly, not replaced by a guessed name", async ({ page }) => {
  const details = await login(page, { chat: { provider: "ollama" } });
  await trigger(page).click();
  await expect(details).toContainText("Chat: Ollama · Model not specified");
  await expect(details).not.toContainText("Images:");
});

test("AI details: legacy shared metadata retains both model roles", async ({ page }) => {
  const details = await login(page, { provider: "openai-compatible", model: "legacy-model" });
  await expect(trigger(page)).toHaveText("AI details");
  await trigger(page).click();
  await expect(details).toContainText("Chat: OpenAI-compatible · legacy-model");
  await expect(details).toContainText("Images: OpenAI-compatible · legacy-model");
  await expect(details).toContainText("Chat messages are sent to the chat AI endpoint");
  await expect(details).toContainText("Photos are sent to the image AI endpoint");
});

test("AI details: Gemini split-role metadata and privacy remain readable at 360px", async ({ page }) => {
  await page.setViewportSize({ width: 360, height: 780 });
  const details = await login(page, { chat: shared, vision: { provider: "gemini", model: "gemini-3.1-flash-lite" } });
  await expect(details).toBeHidden();
  await trigger(page).tap();
  await expect(details).toContainText("Chat: Anthropic Messages · claude-haiku-4-5");
  await expect(details).toContainText("Images: Gemini · gemini-3.1-flash-lite");
  await expect(details).toContainText("Chat messages are sent to the chat AI endpoint configured by your administrator.");
  await expect(details).toContainText("Photos are sent to the image AI endpoint configured by your administrator.");
  await expect(page.getByLabel("API key", { exact: true })).toHaveCount(0);
  expect(await noOverflow(page)).toBe(true);
  await page.screenshot({ path: "/results/ai-details-mobile-gemini-open.png", fullPage: false });
  await details.getByRole("button", { name: "Close AI details", exact: true }).tap();
  await expect(trigger(page)).toBeFocused();
});

test("AI details: unknown metadata keeps the honest provider fallback and endpoint disclosure", async ({ page }) => {
  const details = await login(page, { chat: { provider: "future-provider", model: "future-chat" }, vision: { provider: "ollama", model: "local-image" } });
  await trigger(page).click();
  await expect(details).toContainText("Chat: Configured AI provider · future-chat");
  await expect(details).toContainText("Images: Ollama · local-image");
  await expect(details).toContainText("Chat messages are sent to the chat AI endpoint");
  await expect(details).not.toContainText("Photos are sent");
  await expect(page.getByLabel("API key", { exact: true })).toHaveCount(0);
});
