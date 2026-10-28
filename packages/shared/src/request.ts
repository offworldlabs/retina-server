/** A non-2xx answer. `body` is the parsed JSON body, or null when there was
 *  none. The message is what a page can show: the backend's own `detail`
 *  where it wrote one (FastAPI's shape for every error it raises), otherwise
 *  the status line, which over HTTP/2 has no reason phrase. */
export class HttpError extends Error {
  readonly status: number;
  readonly body: unknown;
  /** How long the answer's Retry-After asked the caller to wait, in ms (see
   *  retryAfterMsOf); null when it sent none the page could read. */
  readonly retryAfterMs: number | null;

  constructor(status: number, statusText: string, body: unknown, retryAfterMs: number | null = null) {
    const detail = (body as { detail?: unknown } | null)?.detail;
    super(
      typeof detail === "string" ? detail : statusText ? `${status} ${statusText}` : `HTTP ${status}`,
    );
    this.name = "HttpError";
    this.status = status;
    this.body = body;
    this.retryAfterMs = retryAfterMs;
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

const DAY = "(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)";
const MONTH = "(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)";
const TIME = "\\d{2}:\\d{2}:\\d{2}";
/** IMF-fixdate, "Sun, 06 Nov 1994 08:49:37 GMT", the form every sender writes. */
const IMF_FIXDATE = new RegExp(`^${DAY}, \\d{2} ${MONTH} \\d{4} ${TIME} GMT$`);
/** The obsolete RFC 850 form, "Sunday, 06-Nov-94 08:49:37 GMT", which a recipient still reads. */
const RFC850_DATE = new RegExp(`^(?:Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day, \\d{2}-${MONTH}-\\d{2} ${TIME} GMT$`);
/** The obsolete asctime form, "Sun Nov  6 08:49:37 1994", in UTC though it does not say so. */
const ASCTIME_DATE = new RegExp(`^${DAY} ${MONTH} [ \\d]\\d ${TIME} \\d{4}$`);

/**
 * A Retry-After value (RFC 9110 section 10.2.3) as the milliseconds to wait
 * from `now`: delay-seconds as given, however long (a caller bounds its own
 * wait), or an HTTP-date less `now` (none for a date already past). Null for
 * no value, or for one that is neither: the shape is checked before
 * Date.parse sees it, which would read almost any text as some date.
 */
function retryAfterMsOf(value: string | null, now: number): number | null {
  if (value === null) return null;
  const text = value.trim();
  if (/^\d+$/.test(text)) return Number(text) * 1000;
  let at = Number.NaN;
  if (IMF_FIXDATE.test(text) || RFC850_DATE.test(text)) at = Date.parse(text);
  else if (ASCTIME_DATE.test(text)) at = Date.parse(`${text} GMT`);
  return Number.isFinite(at) ? Math.max(0, at - now) : null;
}

/** The answer's Retry-After, read as it lands. The browser shows a cross-origin
 *  answer's header only when CORS exposes it; without it this is null. */
function retryAfterOf(res: Response): number | null {
  // A stand-in for fetch may answer without headers; that is no Retry-After,
  // not a different failure.
  return retryAfterMsOf(res.headers?.get("Retry-After") ?? null, Date.now());
}

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
      throw res.status === 401
        ? new UnauthorizedError(body)
        : new HttpError(res.status, res.statusText, body, retryAfterOf(res));
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
