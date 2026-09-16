/** A non-2xx answer. `body` is the parsed JSON body, or null when there was
 *  none. The message is what a page can show: the backend's own `detail`
 *  where it wrote one (FastAPI's shape for every error it raises), otherwise
 *  the status line, which over HTTP/2 has no reason phrase. */
export class HttpError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, statusText: string, body: unknown) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    super(
      typeof detail === "string" ? detail : statusText ? `${status} ${statusText}` : `HTTP ${status}`,
    );
    this.name = "HttpError";
    this.status = status;
    this.body = body;
  }
}

/** A 401: an answer, not a failure to obtain one. Distinct from a network or
 *  timeout error so callers can tell "not signed in" from "no reply yet" and
 *  decline to retry the first. */
export class UnauthorizedError extends HttpError {
  constructor(body: unknown = null) {
    super(401, "Unauthorized", body);
    this.name = "UnauthorizedError";
  }
}

export interface RequestOptions extends Omit<RequestInit, "headers"> {
  /** Give up when the whole answer, body included, has not arrived in this long. */
  timeoutMs?: number;
  headers?: Record<string, string>;
}

const DEFAULT_TIMEOUT_MS = 10_000;

/** `fetch` with the conventions every surface wants: same-origin credentials,
 *  a JSON content type, a timeout, the caller's own AbortSignal honoured
 *  beside it, a typed error for every non-2xx answer, and the parsed JSON on
 *  success. A caller's abort surfaces as its own reason (an AbortError unless
 *  it gave one); the timeout surfaces as a TimeoutError. */
export async function request<T = any>(path: string, opts: RequestOptions = {}): Promise<T> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, signal, headers, ...init } = opts;
  // Composed by hand rather than with AbortSignal.any and AbortSignal.timeout:
  // the build's Safari floor predates both.
  const controller = new AbortController();
  const forward = () => controller.abort(signal?.reason);
  if (signal?.aborted) forward();
  else signal?.addEventListener("abort", forward);
  const timer = setTimeout(
    () => controller.abort(new DOMException(`No response within ${timeoutMs} ms`, "TimeoutError")),
    timeoutMs,
  );
  try {
    const res = await fetch(path, {
      credentials: "same-origin",
      ...init,
      signal: controller.signal,
      headers: { "Content-Type": "application/json", ...headers },
    });
    if (!res.ok) {
      const body = await res.json().catch(() => null);
      throw res.status === 401 ? new UnauthorizedError(body) : new HttpError(res.status, res.statusText, body);
    }
    // Awaited here so the abort and the timeout cover the body as well as the
    // headers; a bare return would run the finally before the body arrived.
    // Every route behind this answers a 2xx with a JSON body; one that answered
    // 204 would surface here as a parse error rather than an empty result.
    return await res.json();
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", forward);
  }
}
