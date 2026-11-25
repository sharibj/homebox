import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createApi } from "../src/api";
import { useConversation } from "../src/useConversation";

beforeEach(() => {
  vi.useFakeTimers();
  // Native AbortSignal.timeout uses an internal clock, not Vitest's fake timers.
  vi.spyOn(AbortSignal, "timeout").mockImplementation(ms => {
    const controller = new AbortController();
    setTimeout(() => controller.abort(new DOMException("Timed out", "TimeoutError")), ms);
    return controller.signal;
  });
});
afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function setup(delay: number | null = 119_999) {
  const receiveAction = vi.fn(), boundaryError = vi.fn(), signals: AbortSignal[] = [];
  const fetcher = vi.fn((url: string, options: RequestInit) => {
    if (url.endsWith("/conversations/reset")) return Promise.resolve(Response.json({ conversation: null, expired: false }));
    signals.push(options.signal!);
    return new Promise<Response>((resolve, reject) => {
      options.signal!.addEventListener("abort", () => reject(options.signal!.reason), { once: true });
      if (delay !== null) setTimeout(() => resolve(Response.json({
        conversationId: "synthetic-conversation", turnId: "synthetic-turn", revision: 1,
        outcome: "answered", message: "Synthetic slow reply", items: [],
      })), delay);
    });
  });
  vi.stubGlobal("fetch", fetcher);
  const conversation = useConversation({ api: createApi(() => "synthetic-csrf"), context: () => 1,
    scope: () => undefined, online: () => true, receiveAction, boundaryError, hasUnsafeAction: () => false });
  return { conversation, fetcher, signals, receiveAction, boundaryError, respondImmediately: () => { delay = 0; } };
}

describe("chat deadline and explicit cancellation", () => {
  it("accepts a valid 119999ms reply without aborting at the former 75-second deadline", async () => {
    const { conversation: c, signals } = setup();
    const work = c.send("Synthetic slow question");
    await vi.advanceTimersByTimeAsync(75_001);
    expect(c.state.sending).toBe(true);
    expect(signals[0].aborted).toBe(false);
    c.updateComposer("Keep my next question");
    await vi.advanceTimersByTimeAsync(44_998);
    await work;
    expect(c.state.messages.at(-1)).toMatchObject({ role: "assistant", text: "Synthetic slow reply", status: "complete" });
    expect(c.state).toMatchObject({ sending: false, unsettled: false, composer: "Keep my next question" });
  });

  it("still aborts the real API signal on reset and ignores its later reply", async () => {
    const { conversation: c, signals, receiveAction, boundaryError } = setup();
    const work = c.send("Old-context question");
    await vi.advanceTimersByTimeAsync(60_000);
    expect(signals[0].aborted).toBe(false);
    c.reset();
    expect(signals[0].aborted).toBe(true);
    await work;
    await vi.advanceTimersByTimeAsync(59_999);
    expect(c.state).toMatchObject({ messages: [], sending: false, unsettled: false, issue: "" });
    expect(receiveAction).not.toHaveBeenCalled();
    expect(boundaryError).not.toHaveBeenCalled();
  });

  it("keeps New chat blocked in flight, then starts cleanly after explicit cancellation", async () => {
    const { conversation: c, fetcher, signals, respondImmediately } = setup();
    const old = c.send("Old question");
    await c.fresh();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(c.state.issue).toContain("Finish or check");
    expect(signals[0].aborted).toBe(false);
    c.reset(); await old; await c.fresh();
    expect(signals[0].aborted).toBe(true);
    expect(fetcher.mock.calls[1][0]).toBe("/api/assistant/v1/conversations/reset");
    respondImmediately();
    const fresh = c.send("Fresh question");
    await vi.advanceTimersByTimeAsync(0); await fresh;
    expect(JSON.parse(fetcher.mock.calls[2][1].body as string)).toMatchObject({ message: "Fresh question" });
    expect(JSON.parse(fetcher.mock.calls[2][1].body as string)).not.toHaveProperty("conversationId");
    expect(c.state.messages.filter(turn => turn.role === "user")).toHaveLength(1);
  });

  it("retains the bounded API default and exact retry body when that deadline really expires", async () => {
    const { conversation: c, fetcher, respondImmediately } = setup(null);
    const work = c.send("Unanswered question");
    const original = fetcher.mock.calls[0][1].body;
    await vi.advanceTimersByTimeAsync(179_999);
    expect(c.state.sending).toBe(true);
    await vi.advanceTimersByTimeAsync(1); await work;
    expect(c.state.unsettled).toBe(true);
    respondImmediately();
    const retried = c.retry(c.state.messages[0].id);
    await vi.advanceTimersByTimeAsync(0); await retried;
    expect(fetcher.mock.calls[1][1].body).toBe(original);
    expect(c.state.unsettled).toBe(false);
    expect(c.state.messages.filter(turn => turn.role === "user")).toHaveLength(1);
  });
});
