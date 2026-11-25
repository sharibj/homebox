/** Poll only an existing server job. This helper cannot submit photos or writes. */
export function createCapturePoller<T>(options: {
  read: (id: string, signal: AbortSignal) => Promise<T>;
  context: () => unknown;
  receive: (job: T) => boolean;
  failed: (cause: unknown) => boolean;
  intervalMs?: number;
}) {
  let generation = 0;
  let controller: AbortController | undefined;
  let timer: ReturnType<typeof setTimeout> | undefined;
  function stop() {
    generation++;
    controller?.abort(); controller = undefined;
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  }
  function start(id: string) {
    stop();
    const current = generation, context = options.context();
    const abort = new AbortController(); controller = abort;
    const valid = () => current === generation && context === options.context() && !abort.signal.aborted;
    async function check() {
      if (!valid()) return;
      try {
        const job = await options.read(id, abort.signal);
        if (!valid() || !options.receive(job)) return;
      } catch (cause) {
        if (!valid() || !options.failed(cause)) return;
      }
      if (valid()) timer = setTimeout(check, options.intervalMs ?? 2_000);
    }
    void check();
  }
  return { start, stop };
}
