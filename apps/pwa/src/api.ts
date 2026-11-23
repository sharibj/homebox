const BASE = "/api/assistant/v1";
export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
export function createApi(getCsrf: () => string | undefined, getContext: () => unknown = () => undefined, getCollection: () => string | undefined = () => undefined) {
  return async function request<T>(path: string, options: {
    method?: "GET" | "POST" | "PATCH";
    body?: unknown;
    idempotencyKey?: string;
  } = {}): Promise<T> {
    const method = options.method || "GET";
    const context = getContext();
    const headers = new Headers({ Accept: "application/json" });
    if (!["/auth/login", "/auth/logout", "/session"].includes(path) && getCollection()) {
      headers.set("X-Collection-ID", getCollection()!);
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
        signal: AbortSignal.timeout(180_000),
      });
    } catch {
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
