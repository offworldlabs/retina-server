import { afterEach, describe, expect, it, vi } from "vitest";
import { HttpError, UnauthorizedError, request } from "./request";

function reply(status: number, body: unknown = null, statusText = "", headers: Record<string, string> = {}): Response {
  return new Response(body === null ? null : JSON.stringify(body), {
    status,
    statusText,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

/** A fetch that never answers on its own and rejects with the signal's reason
 *  when aborted, at once for a signal that already is, which is what the real
 *  one does. */
function hanging() {
  return vi.fn(
    (_path: string, init: RequestInit) =>
      new Promise<Response>((_, reject) => {
        const signal = init.signal!;
        if (signal.aborted) reject(signal.reason);
        else signal.addEventListener("abort", () => reject(signal.reason));
      }),
  );
}

/** A fetch whose headers land at once and whose body never does unless the
 *  signal aborts, the way a stalled download behaves. */
function stalledBody() {
  return vi.fn(async (_path: string, init: RequestInit) => {
    const signal = init.signal!;
    return {
      ok: true,
      status: 200,
      json: () =>
        new Promise((_, reject) => {
          if (signal.aborted) reject(signal.reason);
          else signal.addEventListener("abort", () => reject(signal.reason));
        }),
    } as unknown as Response;
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("request", () => {
  it("sends same-origin credentials and a JSON content type, and returns the parsed body", async () => {
    const fetchMock = vi.fn(async () => reply(200, { nodes: 3 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(request("/api/radar/status")).resolves.toEqual({ nodes: 3 });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/radar/status",
      expect.objectContaining({
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        signal: expect.any(AbortSignal),
      }),
    );
  });

  it("passes the caller's method, body and extra headers through", async () => {
    const fetchMock = vi.fn(async () => reply(200, { ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    await request("/api/simulation/config", {
      method: "PUT",
      body: JSON.stringify({ n_nodes: 4 }),
      headers: { "X-Trace": "t1" },
    });
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/simulation/config",
      expect.objectContaining({
        method: "PUT",
        body: JSON.stringify({ n_nodes: 4 }),
        headers: { "Content-Type": "application/json", "X-Trace": "t1" },
      }),
    );
  });

  it("answers a 401 with UnauthorizedError, which is also an HttpError", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => reply(401, { detail: "Not authenticated" }, "Unauthorized")));
    const error = await request("/api/auth/me").catch((e) => e);
    expect(error).toBeInstanceOf(UnauthorizedError);
    expect(error).toBeInstanceOf(HttpError);
    expect(error.status).toBe(401);
    expect(error.body).toEqual({ detail: "Not authenticated" });
  });

  it("answers any other non-2xx with HttpError carrying status, text and body", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => reply(502, { upstream: "down" }, "Bad Gateway")));
    const error = await request("/api/admin/infrastructure").catch((e) => e);
    expect(error).toBeInstanceOf(HttpError);
    expect(error).not.toBeInstanceOf(UnauthorizedError);
    expect(error.status).toBe(502);
    expect(error.message).toBe("502 Bad Gateway");
    expect(error.body).toEqual({ upstream: "down" });
  });

  it("makes the backend's detail the message where it wrote one", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => reply(400, { detail: "That sign-in link is no longer valid" }, "Bad Request")));
    const error = await request("/api/auth/magic-link/consume", { method: "POST" }).catch((e) => e);
    expect(error.message).toBe("That sign-in link is no longer valid");
  });

  it("falls back to the status line when detail is not a string, and to HTTP <status> without a reason phrase", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => reply(422, { detail: [{ loc: ["body"], msg: "required" }] }, "Unprocessable Entity")));
    const validation = await request("/api/auth/magic-link/consume", { method: "POST" }).catch((e) => e);
    expect(validation.message).toBe("422 Unprocessable Entity");

    // HTTP/2, which every deployed origin speaks, carries no reason phrase.
    vi.stubGlobal("fetch", vi.fn(async () => reply(502, null)));
    const edge = await request("/api/radar/status").catch((e) => e);
    expect(edge.message).toBe("HTTP 502");
  });

  it("reports a null body when an error answer carries no JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 401 })));
    const error = await request("/api/auth/me").catch((e) => e);
    expect(error).toBeInstanceOf(UnauthorizedError);
    expect(error.body).toBeNull();
  });

  it("gives up after timeoutMs with a TimeoutError", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", hanging());
    const pending = request("/api/radar/status", { timeoutMs: 50 }).catch((e) => e);
    await vi.advanceTimersByTimeAsync(50);
    const error = await pending;
    expect(error.name).toBe("TimeoutError");
  });

  it("forwards the caller's abort, reason included, to the request it made", async () => {
    const fetchMock = hanging();
    vi.stubGlobal("fetch", fetchMock);
    const caller = new AbortController();
    const pending = request("/api/radar/status", { signal: caller.signal }).catch((e) => e);
    caller.abort(new Error("gone"));
    const error = await pending;
    expect(error).toBe(caller.signal.reason);
    expect(fetchMock.mock.calls[0][1].signal!.aborted).toBe(true);
  });

  it("starts already aborted when the caller's signal is", async () => {
    vi.stubGlobal("fetch", hanging());
    const caller = new AbortController();
    caller.abort();
    const error = await request("/api/radar/status", { signal: caller.signal }).catch((e) => e);
    expect(error.name).toBe("AbortError");
  });

  it("still honours the caller's abort and the timeout while the body is downloading", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", stalledBody());
    const caller = new AbortController();
    const aborted = request("/api/radar/analytics", { signal: caller.signal }).catch((e) => e);
    // Let the headers land, so the abort arrives while the body is being read.
    await vi.advanceTimersByTimeAsync(0);
    caller.abort();
    expect((await aborted).name).toBe("AbortError");

    const timedOut = request("/api/radar/analytics", { timeoutMs: 50 }).catch((e) => e);
    await vi.advanceTimersByTimeAsync(50);
    expect((await timedOut).name).toBe("TimeoutError");
  });

  it("leaves no timer behind once the answer has landed", async () => {
    vi.useFakeTimers();
    vi.stubGlobal("fetch", vi.fn(async () => reply(200, {})));
    await request("/api/radar/status");
    expect(vi.getTimerCount()).toBe(0);
  });
});

describe("a 429's Retry-After", () => {
  /** A 429 from the archive, with `retryAfter` as its Retry-After header when given. */
  const limited = (retryAfter?: string) =>
    vi.fn(async () =>
      reply(
        429,
        { detail: "Too many archive requests" },
        "Too Many Requests",
        retryAfter === undefined ? {} : { "Retry-After": retryAfter },
      ),
    );

  it("is carried on the HttpError in milliseconds when given in seconds", async () => {
    vi.stubGlobal("fetch", limited("7"));
    const error = await request("/api/data/tracks").catch((e) => e);
    expect(error).toBeInstanceOf(HttpError);
    expect(error.status).toBe(429);
    expect(error.message).toBe("Too many archive requests");
    expect(error.retryAfterMs).toBe(7_000);
  });

  it.each([
    ["IMF-fixdate", "Mon, 26 Oct 2026 12:00:30 GMT", 30_000],
    ["RFC 850", "Monday, 26-Oct-26 12:00:30 GMT", 30_000],
    ["asctime, read as UTC", "Mon Oct 26 12:00:30 2026", 30_000],
    ["a date already past", "Mon, 26 Oct 2026 11:59:00 GMT", 0],
  ])("is the time left until an HTTP-date (%s)", async (_form, date, ms) => {
    vi.useFakeTimers();
    vi.setSystemTime(Date.UTC(2026, 9, 26, 12, 0, 0));
    vi.stubGlobal("fetch", limited(date));
    expect((await request("/api/data/tracks").catch((e) => e)).retryAfterMs).toBe(ms);
  });

  // Date.parse would read "-5" and "Nov 6" as dates in 2001.
  it.each([
    ["no header", undefined],
    ["a word", "soon"],
    ["a negative number", "-5"],
    ["a loose date", "Nov 6"],
  ])("is null for %s", async (_case, value) => {
    vi.stubGlobal("fetch", limited(value));
    const error = await request("/api/data/tracks").catch((e) => e);
    expect(error.status).toBe(429);
    expect(error.retryAfterMs).toBeNull();
  });
});
