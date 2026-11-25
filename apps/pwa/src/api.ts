const BASE = "/api/assistant/v1";
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
export function createApi(getCsrf: () => string | undefined, getContext: () => unknown = () => undefined, getCollection: () => string | undefined = () => undefined, getContextVersion: () => string | number | undefined = () => undefined) {
  return async function request<T>(path: string, options: {
    method?: "GET" | "POST" | "PATCH";
    body?: unknown;
    idempotencyKey?: string;
    timeoutMs?: number;
    signal?: AbortSignal;
  } = {}): Promise<T> {
    const method = options.method || "GET";
    const context = getContext();
    const headers = new Headers({ Accept: "application/json" });
    if (!["/auth/login", "/auth/logout", "/session"].includes(path) && getCollection()) {
      headers.set("X-Collection-ID", getCollection()!);
      const version = getContextVersion();
      if (version !== undefined) headers.set("X-Context-Version", String(version));
    }
    const multipart = options.body instanceof FormData;
    if (options.body !== undefined && !multipart) headers.set("Content-Type", "application/json");
    if (method !== "GET" && getCsrf()) headers.set("X-CSRF-Token", getCsrf()!);
    if (options.idempotencyKey) headers.set("Idempotency-Key", options.idempotencyKey);
    let response: Response;
    try {
      response = await fetch(`${BASE}${path}`, {
        method, headers, credentials: "same-origin", cache: "no-store",
        body: options.body === undefined ? undefined : multipart ? options.body as FormData : JSON.stringify(options.body),
        signal: options.signal
          ? AbortSignal.any([options.signal, AbortSignal.timeout(options.timeoutMs ?? 180_000)])
          : AbortSignal.timeout(options.timeoutMs ?? 180_000),
      });
    } catch (cause) {
      if (options.signal?.aborted) throw new ApiError(0, "request_cancelled", "This screen stopped checking the request.");
      if (path === "/captures" || path === "/capture-jobs") {
        const timedOut = cause instanceof Error && cause.name === "TimeoutError";
        throw new ApiError(0, timedOut ? "capture_timeout" : "capture_connection_lost",
          `${timedOut ? "The photo request timed out." : "The connection to your photo upload was interrupted."} Your selected photo and entered details are still here. No inventory item was created. Check photo progress before sending it again.`);
      }
      if (path === "/chat" || path.startsWith("/conversations/")) {
        throw new ApiError(0, "chat_connection_lost", "The reply was interrupted. Your conversation and draft may already be saved. Check this turn before sending it again. No inventory change is confirmed by a chat message.");
      }
      throw new ApiError(0, "connection_lost", method === "GET"
        ? "Could not reach your inventory. Check your connection and try again."
        : "The connection was interrupted. If you confirmed a change, check its status before trying again.");
    }
    const data = await response.json().catch(() => null);
    if (context !== getContext()) throw new ApiError(0, "stale_response", "The active inventory session changed.");
    if (!response.ok) throw new ApiError(response.status, data?.error?.code || "request_failed",
      data?.error?.message || `The inventory service could not complete this request (${response.status}).`);
    if (!data) throw new ApiError(response.status, "invalid_response", "The inventory service returned an unexpected response.");
    return data as T;
  };
}
