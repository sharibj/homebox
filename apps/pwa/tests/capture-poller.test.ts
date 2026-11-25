import { afterEach, describe, expect, it, vi } from "vitest";
import { createCapturePoller } from "../src/capture-poller";
afterEach(() => vi.useRealTimers());
describe("durable capture status polling", () => {
  it("keeps short reads going beyond 180 seconds without submitting new work", async () => {
    vi.useFakeTimers();
    const start = Date.now();
    const read = vi.fn().mockImplementation(async () => ({ done: Date.now() - start > 180_000 }));
    const receive = vi.fn(job => !job.done);
    const poller = createCapturePoller({ read, receive, context: () => "one", failed: () => true });
    poller.start("existing-job");
    await vi.advanceTimersByTimeAsync(184_000);
    expect(read.mock.calls.length).toBeGreaterThan(90);
    expect(receive.mock.lastCall?.[0]).toEqual({ done: true });
    const count = read.mock.calls.length;
    await vi.advanceTimersByTimeAsync(30_000);
    expect(read).toHaveBeenCalledTimes(count);
    poller.stop();
  });
  it("retries a transient status failure without changing the job ID", async () => {
    vi.useFakeTimers();
    const read = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ done: true });
    const receive = vi.fn(() => false), failed = vi.fn(() => true);
    const poller = createCapturePoller({ read, receive, failed, context: () => 1 });
    poller.start("same-job");
    await vi.advanceTimersByTimeAsync(2_000);
    expect(read.mock.calls.map(call => call[0])).toEqual(["same-job", "same-job"]);
    expect(failed).toHaveBeenCalledOnce();
    expect(receive).toHaveBeenCalledOnce();
    poller.stop();
  });
  it("aborts and ignores a late response from a different photo or collection", async () => {
    let resolve!: (value: string) => void;
    let context = "collection-one";
    const read = vi.fn((_id: string, _signal: AbortSignal) => new Promise<string>(done => { resolve = done; }));
    const receive = vi.fn(() => false);
    const poller = createCapturePoller({ read, receive, context: () => context, failed: () => true });
    poller.start("old-photo");
    context = "collection-two";
    poller.stop();
    expect(read.mock.calls[0][1].aborted).toBe(true);
    resolve("late-old-result");
    await Promise.resolve(); await Promise.resolve();
    expect(receive).not.toHaveBeenCalled();
  });
  it("never overlaps polls and stops on a permanent authorization error", async () => {
    vi.useFakeTimers();
    let reject!: (cause: Error) => void;
    const read = vi.fn(() => new Promise<string>((_resolve, fail) => { reject = fail; }));
    const poller = createCapturePoller({ read, receive: () => true, context: () => 1, failed: () => false });
    poller.start("job");
    await vi.advanceTimersByTimeAsync(30_000);
    expect(read).toHaveBeenCalledOnce();
    reject(new Error("unauthorized"));
    await vi.advanceTimersByTimeAsync(30_000);
    expect(read).toHaveBeenCalledOnce();
    poller.stop();
  });
});
