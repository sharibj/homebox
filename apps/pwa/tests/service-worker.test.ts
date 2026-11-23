import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";
import { describe, expect, it, vi } from "vitest";

function worker() {
  const handlers: Record<string, (event: unknown) => void> = {};
  const cache = { addAll: vi.fn().mockResolvedValue(undefined) };
  const caches = { open: vi.fn().mockResolvedValue(cache), keys: vi.fn().mockResolvedValue([]),
    match: vi.fn().mockResolvedValue("static-shell"), delete: vi.fn() };
  const network = vi.fn().mockResolvedValue("network");
  runInNewContext(readFileSync("public/sw.js", "utf8").replace("__SHELL_FILES__", '["/","/index.html","/assets/app.js"]')
    .replace("__SHELL_VERSION__", "homebox-companion-test"), {
    self: { location: { origin: "https://inventory.test" }, addEventListener: (name: string, handler: typeof handlers[string]) => { handlers[name] = handler; },
      clients: { claim: vi.fn() } }, caches, fetch: network, URL,
  });
  return { handlers, cache, caches, network };
}
describe("static-only offline shell", () => {
  it("never handles API reads, private images, writes, or foreign origins", () => {
    const { handlers, caches } = worker();
    for (const [url, method, mode] of [
      ["https://inventory.test/api/assistant/v1/session", "GET", "cors"],
      ["https://inventory.test/api/assistant/v1/items", "GET", "navigate"],
      ["https://inventory.test/photo/private", "GET", "cors"],
      ["https://inventory.test/api/assistant/v1/auth/login", "POST", "cors"],
      ["https://elsewhere.test/assets/app.js", "GET", "cors"],
    ]) {
      const respondWith = vi.fn();
      handlers.fetch({ request: { url, method, mode }, respondWith });
      expect(respondWith).not.toHaveBeenCalled();
    }
    expect(caches.match).not.toHaveBeenCalled();
  });
  it("loads navigation from network without putting private responses in caches", async () => {
    const { handlers, network, cache } = worker();
    let response: unknown;
    handlers.fetch({ request: { url: "https://inventory.test/t/t_private", method: "GET", mode: "navigate" }, respondWith: (promise: Promise<unknown>) => { response = promise; } });
    expect(await response).toBe("network");
    expect(network).toHaveBeenCalledOnce();
    expect(cache.addAll).not.toHaveBeenCalled();
  });
  it("falls back to just the public shell when a tag navigation is offline", async () => {
    const { handlers, network, caches } = worker();
    network.mockRejectedValueOnce(new Error("offline"));
    let response: unknown;
    handlers.fetch({ request: { url: "https://inventory.test/t/t_private", method: "GET", mode: "navigate" }, respondWith: (promise: Promise<unknown>) => { response = promise; } });
    expect(await response).toBe("static-shell");
    expect(caches.match).toHaveBeenCalledWith("/index.html");
  });
});
