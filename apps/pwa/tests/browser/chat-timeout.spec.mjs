import { test, expect } from "@playwright/test";

const chat = page => page.getByRole("region", { name: "Inventory conversation", exact: true });
async function login(page) {
  await page.goto("/");
  await page.getByLabel("Email or username").fill("demo@example.test");
  await page.getByLabel("Password", { exact: true }).fill("disposable-test-only");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  await expect(chat(page).getByRole("button", { name: "Show inventory", exact: true })).toBeEnabled();
}
async function controlledDeadlineClock(page) {
  await page.clock.install({ time: new Date("2026-11-25T00:00:00Z") });
  await page.clock.pauseAt(new Date("2026-11-25T00:00:01Z"));
  await page.evaluate(() => {
    // Browser-native timeout clocks are not controlled by page.clock. Only the
    // timeout scheduler is adapted; the real API, fetch and AbortSignal.any run.
    window.__deadlineSignals = [];
    window.__deadlineCalls = [];
    AbortSignal.timeout = ms => {
      const controller = new AbortController();
      window.__deadlineCalls.push(ms);
      setTimeout(() => controller.abort(new DOMException("Timed out", "TimeoutError")), ms);
      return controller.signal;
    };
    const originalFetch = window.fetch;
    window.fetch = function(input, options) {
      if (String(input).endsWith("/chat")) window.__deadlineSignals.push(options.signal);
      return originalFetch.call(this, input, options);
    };
  });
}
function gate() {
  let release;
  return { promise: new Promise(resolve => { release = resolve; }), release: () => release() };
}

test("chat deadline: a valid 119999ms synthetic reply survives 75001ms and preserves new input", async ({ page }) => {
  await login(page); await controlledDeadlineClock(page);
  const delivery = gate(), ready = gate();
  let abandoned = false;
  await page.route("**/chat", async route => {
    const response = await route.fetch();
    ready.release(); await delivery.promise;
    try { await route.fulfill({ response }); } catch (error) { if (!abandoned) throw error; }
  });
  try {
    await chat(page).getByRole("button", { name: "Show inventory", exact: true }).click();
    await ready.promise;
    await page.clock.fastForward(75_001);
    await expect(chat(page).getByText("Sent · waiting for a response", { exact: true })).toBeVisible({ timeout: 1000 });
    expect(await page.evaluate(() => window.__deadlineSignals[0].aborted)).toBe(false);
    await page.getByLabel("Ask your inventory", { exact: true }).fill("Keep my next question");
    await page.clock.fastForward(44_998);
    delivery.release();
    await expect(chat(page).getByLabel("Current collection overview")).toBeVisible();
    await expect(page.getByLabel("Ask your inventory", { exact: true })).toHaveValue("Keep my next question");
    await expect(chat(page).getByRole("button", { name: "New chat", exact: true })).toBeEnabled();
    expect(await page.evaluate(() => window.__deadlineCalls)).toEqual([180_000]);
    console.log(JSON.stringify({ check: "slow-valid-reply", virtualDelayMs: 119999, stillPendingAtMs: 75001, received: true, newInputPreserved: true, nativeTimeoutSchedulerAdapted: true }));
  } finally { abandoned = true; delivery.release(); }
});

test("chat deadline: collection cancellation aborts the real signal and a new chat ignores the old reply", async ({ page }) => {
  await login(page); await controlledDeadlineClock(page);
  const delivery = gate(), ready = gate(), finished = gate();
  let posts = 0, cancelled = false;
  await page.route("**/chat", async route => {
    if (++posts !== 1) return route.continue();
    const response = await route.fetch();
    ready.release(); await delivery.promise;
    try { await route.fulfill({ response }); } catch (error) { if (!cancelled) throw error; }
    finally { finished.release(); }
  });
  try {
    await chat(page).getByRole("button", { name: "Show inventory", exact: true }).click();
    await ready.promise; await page.clock.fastForward(60_000);
    expect(await page.evaluate(() => window.__deadlineSignals[0].aborted)).toBe(false);
    await expect(chat(page).getByRole("button", { name: "New chat", exact: true })).toBeDisabled();
    await page.getByLabel("Collection", { exact: true }).selectOption({ label: "Workshop" });
    await expect(chat(page).getByRole("button", { name: "Show inventory", exact: true })).toBeEnabled();
    expect(await page.evaluate(() => window.__deadlineSignals[0].aborted)).toBe(true);
    cancelled = true;
    await chat(page).getByRole("button", { name: "New chat", exact: true }).click();
    await expect(chat(page).getByRole("button", { name: "Show inventory", exact: true })).toBeEnabled();
    await chat(page).getByRole("button", { name: "Show inventory", exact: true }).click();
    await expect(chat(page).getByLabel("Current collection overview")).toContainText("Workshop");
    delivery.release(); await finished.promise;
    await expect(chat(page).getByLabel("Current collection overview")).toContainText("Workshop");
    await expect(chat(page).locator(".turn.user")).toHaveCount(1);
    await expect(chat(page).getByRole("button", { name: "Check and retry this turn", exact: true })).toHaveCount(0);
    console.log(JSON.stringify({ check: "cancel-and-new-chat", cancelledAtMs: 60000, oldApiSignalAborted: true, newCollection: "synthetic Workshop", newChatCompleted: true, oldReplyNotRendered: true }));
  } finally { cancelled = true; delivery.release(); }
});
