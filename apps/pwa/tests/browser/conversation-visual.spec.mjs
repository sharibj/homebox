import { test, expect } from "@playwright/test";
for (const [kind, viewport] of [["desktop", { width: 1280, height: 1024 }], ["mobile", { width: 360, height: 800 }]]) {
test(`capture ${kind} synthetic conversational UI for early visual review only`, async ({ page }) => {
  await page.setViewportSize(viewport);
  await page.goto("/");
  await page.getByLabel("Email or username").fill("demo@example.test");
  await page.getByLabel("Password", { exact: true }).fill("disposable-test-only");
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
  const chat = page.getByRole("region", { name: "Inventory conversation", exact: true });
  const ask = async text => {
    await page.getByLabel("Ask your inventory", { exact: true }).fill(text);
    await page.getByLabel("Ask your inventory", { exact: true }).press("Enter");
    await expect(chat.getByRole("button", { name: "Ask Homebox", exact: true })).toHaveText("Send");
  };
  await ask("Which inventory do I have?");
  await ask("Add a mobile charge to the entry.");
  if (kind === "mobile") {
    const bounds = await chat.locator(".chat-log").boundingBox();
    const composer = await chat.locator(".conversation-composer").boundingBox();
    for (const locator of [chat.locator(".turn.user").last(), chat.locator(".turn.assistant").last().locator(".message-text"), ...await chat.locator(".destination-choice").all()]) {
      const box = await locator.boundingBox();
      expect(box.y).toBeGreaterThanOrEqual(bounds.y);
      expect(box.y + box.height).toBeLessThanOrEqual(composer.y);
    }
    await expect(chat.getByLabel("Ask your inventory", { exact: true })).toHaveAttribute("aria-describedby", "chat-keyboard-help");
    expect(await chat.locator("#chat-keyboard-help").evaluate(node => getComputedStyle(node).clip)).not.toBe("auto");
    expect((await chat.locator(".conversation-status").boundingBox()).height).toBeLessThanOrEqual(1);
    expect((await chat.getByLabel("Pending conversation task").boundingBox()).height).toBeLessThan(85);
  }
  await chat.screenshot({ path: `/results/chat-early-${kind}-synthetic.png` });
  await chat.getByRole("button", { name: "Use House / Entry", exact: true }).last().click();
  await expect(page.getByRole("region", { name: "Inventory change confirmation" })).toBeVisible();
  await chat.screenshot({ path: `/results/chat-early-${kind}-review-synthetic.png` });
  await chat.locator(".chat-log").evaluate(node => { node.scrollTop = node.scrollHeight; });
  await chat.screenshot({ path: `/results/chat-${kind}-review-controls-synthetic.png` });
});
}
