import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, createApi } from "../src/api";
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
describe("private API boundary", () => {
  it("sends the rendered context version even after returning to the same collection", async () => {
    const fetcher = vi.fn().mockImplementation(async () => Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetcher);
    let version = "A-1";
    const api = createApi(() => "csrf", () => version, () => "A", () => version);
    await api("/chat", { method: "POST", body: { message: "Overview" } });
    version = "A-3"; await api("/items");
    expect(fetcher.mock.calls[0][1].headers.get("X-Context-Version")).toBe("A-1");
    expect(fetcher.mock.calls[1][1].headers.get("X-Context-Version")).toBe("A-3");
  });
  it("does not use a confirmed-change warning for an interrupted chat read", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed")));
    await expect(createApi(() => "csrf")("/chat", { method: "POST", body: { message: "Show inventory", mode: "read_only" } }))
      .rejects.toMatchObject({ code: "chat_connection_lost", message: expect.not.stringContaining("If you confirmed") });
  });
  it("binds every tenant request and collection switch to the displayed collection", async () => {
    const fetcher = vi.fn().mockImplementation(async () => Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetcher);
    const api = createApi(() => "csrf", () => 1, () => "displayed-one");
    await api("/items");
    await api("/session/collection", { method: "POST", body: { collectionId: "requested-two" } });
    await api("/session");
    expect(fetcher.mock.calls[0][1].headers.get("X-Collection-ID")).toBe("displayed-one");
    expect(fetcher.mock.calls[1][1].headers.get("X-Collection-ID")).toBe("displayed-one");
    expect(fetcher.mock.calls[2][1].headers.has("X-Collection-ID")).toBe(false);
  });
  it("uses same-origin cookie authentication, no-store, and CSRF on writes", async () => {
    const fetcher = vi.fn().mockImplementation(async () => Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetcher);
    await createApi(() => "csrf-value")("/actions", { method: "POST", body: { type: "item.move" }, idempotencyKey: "stable-key" });
    const [url, options] = fetcher.mock.calls[0];
    expect(url).toBe("/api/assistant/v1/actions");
    expect(options.credentials).toBe("same-origin");
    expect(options.cache).toBe("no-store");
    expect(options.headers.get("X-CSRF-Token")).toBe("csrf-value");
    expect(options.headers.get("Idempotency-Key")).toBe("stable-key");
    expect(options.headers.has("Authorization")).toBe(false);
  });
  it("does not add CSRF to GET or a fake multipart content type", async () => {
    const fetcher = vi.fn().mockImplementation(async () => Response.json({ ok: true }));
    vi.stubGlobal("fetch", fetcher);
    const api = createApi(() => "csrf");
    await api("/items?q=stove");
    expect(fetcher.mock.calls[0][1].headers.has("X-CSRF-Token")).toBe(false);
    const form = new FormData(); form.append("hint", "A stove");
    await api("/captures", { method: "POST", body: form });
    expect(fetcher.mock.calls[1][1].body).toBe(form);
    expect(fetcher.mock.calls[1][1].headers.has("Content-Type")).toBe(false);
  });
  it("uses the latest in-memory CSRF token after collection/session changes", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ ok: true })));
    let token = "old";
    const api = createApi(() => token);
    token = "new";
    await api("/session/collection", { method: "POST", body: { collectionId: "two" } });
    expect(vi.mocked(fetch).mock.calls[0][1]?.headers).toHaveProperty("get");
    expect(new Headers(vi.mocked(fetch).mock.calls[0][1]?.headers).get("X-CSRF-Token")).toBe("new");
  });
  it("preserves safe errors and unauthorized status", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(Response.json({ error: { code: "session_expired", message: "Sign in again" } }, { status: 401 })));
    await expect(createApi(() => undefined)("/session")).rejects.toMatchObject({ status: 401, code: "session_expired", message: "Sign in again" });
  });
  it("discards a delayed response after the collection context changes", async () => {
    let context = "one";
    let finish!: (response: Response) => void;
    vi.stubGlobal("fetch", vi.fn().mockImplementation(() => new Promise<Response>(resolve => { finish = resolve; })));
    const pending = createApi(() => "csrf", () => context)("/items");
    context = "two";
    finish(Response.json({ items: [{ name: "Private from collection one" }] }));
    await expect(pending).rejects.toMatchObject({ code: "stale_response" });
  });
  it("reports interrupted writes without suggesting blind retries", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(createApi(() => "token")("/actions/id/confirm", { method: "POST", body: {} })).rejects.toThrow("check its status");
  });
  it("gives a photo-specific timeout without implying an inventory confirmation", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("Timed out", "TimeoutError")));
    await expect(createApi(() => "token")("/capture-jobs", { method: "POST", body: new FormData() }))
      .rejects.toMatchObject({ code: "capture_timeout", message: expect.stringContaining("No inventory item was created") });
  });
  it("keeps a photo upload connection loss distinct from a confirmed write", async () => {
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));
    await expect(createApi(() => "token")("/capture-jobs", { method: "POST", body: new FormData() }))
      .rejects.toMatchObject({ code: "capture_connection_lost", message: expect.not.stringContaining("If you confirmed") });
  });
  it("allows short status requests without extending confirmation deadlines", async () => {
    const timeout = vi.spyOn(AbortSignal, "timeout").mockImplementation(() => new AbortController().signal);
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async () => Response.json({ ok: true })));
    const api = createApi(() => "token");
    await api("/capture-jobs/opaque", { timeoutMs: 15_000 });
    await api("/actions/opaque/confirm", { method: "POST", body: {} });
    expect(timeout.mock.calls).toEqual([[15_000], [180_000]]);
  });
  it("distinguishes a deliberately cancelled old-context poll", async () => {
    const controller = new AbortController(); controller.abort();
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new DOMException("Aborted", "AbortError")));
    await expect(createApi(() => "token")("/capture-jobs/old", { signal: controller.signal }))
      .rejects.toMatchObject({ code: "request_cancelled" });
  });
  it("does not accept proxy HTML as a successful JSON response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<html>offline</html>", { status: 200 })));
    await expect(createApi(() => "")("/items")).rejects.toBeInstanceOf(ApiError);
  });
});
