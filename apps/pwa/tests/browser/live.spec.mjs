import { readFileSync } from "node:fs";
import { test, expect } from "@playwright/test";

test.use({ trace: "off", screenshot: "off", actionTimeout: 15000 });
test.setTimeout(120000);
const file = process.env.ACCEPTANCE_CREDENTIAL_FILE;
const credentials = file ? JSON.parse(readFileSync(file, "utf8")) : null;
const card = page => page.getByRole("region", { name: "Inventory change confirmation" });
async function signIn(page, user) {
  await page.goto("/");
  await page.getByLabel("Email or username").waitFor();
  // Keep disposable credentials out of command arguments, traces and failure logs.
  await page.evaluate(({ email, password }) => {
    for (const [selector, value] of [['input[autocomplete="username"]', email], ['input[autocomplete="current-password"]', password]]) {
      const input = document.querySelector(selector);
      input.value = value; input.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }, user);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(page.getByLabel("Collection", { exact: true })).toBeVisible();
  await expect(page.locator(".busy-status")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Places", exact: true })).toBeEnabled();
}
async function finish(page) {
  await card(page).getByRole("button", { name: "Confirm change" }).click();
  await expect(card(page)).toContainText("Saved to Homebox", { timeout: 30000 });
}
async function createLocation(page, name) {
  await page.getByRole("button", { name: "Places", exact: true }).click();
  if (!(await page.getByLabel("Location name", { exact: true }).isVisible())) {
    await page.getByText("Create a storage location", { exact: true }).click();
  }
  await page.getByLabel("Location name", { exact: true }).fill(name);
  await page.getByRole("button", { name: "Review new location" }).click();
  await finish(page);
  await card(page).getByRole("button", { name: "Done", exact: true }).click();
}
test("real production nginx + source Homebox: offline photo, reviewed create, search/chat, move, NFC, account boundary", async ({ page, browser }) => {
  test.skip(!credentials, "Requires protected disposable acceptance credentials, never a real inventory.");
  const pageErrors = []; page.on("pageerror", error => pageErrors.push(error.message));
  await signIn(page, credentials.users[0]);
  expect(await page.locator("script[src]").getAttribute("src")).toContain("index-BgA1FbF3.js");
  const run = Date.now().toString(36);
  const first = `Browser shelf ${run}`;
  const second = `Browser box ${run}`;
  const itemName = `Browser lantern ${run}`;
  await createLocation(page, first);
  await createLocation(page, second);
  await page.getByRole("button", { name: "Add an item", exact: true }).click();
  await page.getByLabel("Take or choose a photo").setInputFiles("/workspace/public/icon-192.png");
  await page.getByRole("button", { name: "Suggest details from photo" }).click();
  await expect(page.getByText("Photo uploaded. Enter the item details manually, then review before saving.")).toBeVisible({ timeout: 30000 });
  await expect(page.getByLabel("Item name", { exact: true })).toHaveValue("");
  await page.getByLabel("Item name", { exact: true }).fill(itemName);
  await page.getByLabel("Location", { exact: true }).selectOption({ label: first });
  await page.getByLabel("Quantity", { exact: true }).fill("2");
  await page.getByLabel("Manufacturer", { exact: true }).fill("Browser reviewed maker");
  await page.getByLabel("Model", { exact: true }).fill("Offline manual model");
  await page.getByLabel("Visible serial number").fill(`BROWSER-${run}`);
  await page.getByLabel("Notes", { exact: true }).fill("Synthetic browser acceptance photo and metadata.");
  await page.getByLabel("Tags, separated by commas", { exact: true }).fill("browser acceptance");
  const proposal = page.waitForResponse(response => response.url().endsWith("/actions") && response.request().method() === "POST");
  await page.getByRole("button", { name: "Review new item" }).click();
  const proposed = await (await proposal).json();
  expect(proposed.after.photoIncluded).toBe(true);
  expect(proposed.payload.quantity).toBe(2);
  for (const value of [itemName, first, "Browser reviewed maker", "Offline manual model", `BROWSER-${run}`, "Synthetic browser acceptance photo and metadata.", "browser acceptance"]) await expect(card(page)).toContainText(value);
  await expect(card(page).getByRole("img", { name: "Reviewed photo to attach" })).toBeVisible();
  await page.screenshot({ path: "/results/live-offline-photo-review.png", fullPage: true });
  await finish(page);
  await card(page).getByRole("button", { name: "Open saved record" }).click();
  await card(page).getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.getByRole("heading", { name: itemName, exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Move item", exact: true }).click();
  await page.getByLabel("Move to", { exact: true }).selectOption({ label: second });
  await page.getByRole("button", { name: "Review move", exact: true }).click();
  await expect(card(page)).toContainText(second);
  await finish(page);
  await card(page).getByRole("button", { name: "Open saved record" }).click();
  await card(page).getByRole("button", { name: "Done", exact: true }).click();
  await expect(page.locator(".location-path")).toContainText(second);
  await page.getByLabel("New label name").fill(`Browser sticker ${run}`);
  await page.getByRole("button", { name: "Create stable tag for item" }).click();
  await page.getByRole("button", { name: `Browser sticker ${run} · active`, exact: true }).click();
  await expect(page.getByRole("img", { name: "QR code for this stable tag URL" })).toBeVisible();
  const tagUrl = await page.locator("a.break-anywhere").getAttribute("href");
  expect(tagUrl).toMatch(/^http:\/\/localhost:8088\/t\//);
  await page.getByLabel("New location", { exact: true }).selectOption({ label: first });
  await page.getByRole("button", { name: "Confirm reassignment", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Tag mapping updated" })).toBeVisible();
  await page.goto(tagUrl);
  await expect(page.getByRole("heading", { name: first, exact: true })).toBeVisible();
  await page.getByRole("button", { name: `Browser sticker ${run} · active`, exact: true }).click();
  await expect(page.locator("a.break-anywhere")).toHaveAttribute("href", tagUrl);
  await page.screenshot({ path: "/results/live-stable-qr-tag.png", fullPage: true });
  await page.getByRole("button", { name: "Revoke tag", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Tag mapping updated" })).toBeVisible();
  await page.goto(tagUrl);
  await expect(page.getByRole("alert")).toContainText("Tag unavailable");
  await page.goto("/");
  await page.getByRole("searchbox", { name: "Search inventory" }).fill(itemName);
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.getByRole("heading", { name: "1 result", exact: true })).toBeVisible();
  await expect(page.locator(".entity-button").filter({ hasText: itemName })).toContainText(second);
  await page.getByLabel("Ask your inventory").fill(`Where is ${itemName}?`);
  await page.getByRole("button", { name: "Ask Homebox" }).click();
  await expect(page.locator(".transcript")).toContainText(itemName);
  await expect(page.locator(".transcript")).toContainText(second);
  await page.screenshot({ path: "/results/live-search-after-move.png", fullPage: true });
  const otherContext = await browser.newContext({ baseURL: "http://localhost:8088", viewport: { width: 390, height: 844 } });
  const other = await otherContext.newPage();
  await signIn(other, credentials.users[1]);
  await other.getByRole("searchbox", { name: "Search inventory" }).fill(itemName);
  await other.getByRole("button", { name: "Search", exact: true }).click();
  await expect(other.getByRole("heading", { name: "0 results", exact: true })).toBeVisible();
  await otherContext.close();
  expect(pageErrors).toEqual([]);
});
