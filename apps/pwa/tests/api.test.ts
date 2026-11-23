import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError, createApi } from "../src/api";
afterEach(() => vi.unstubAllGlobals());
describe("private API boundary", () => {
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
  it("does not accept proxy HTML as a successful JSON response", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("<html>offline</html>", { status: 200 })));
    await expect(createApi(() => "")("/items")).rejects.toBeInstanceOf(ApiError);
  });
});
