import { describe, it, expect } from "vitest";
import { HttpError, UnauthorizedError } from "@retina/shared";
import { describeFailure, linkUnspent, retryDelayMs } from "../utils/requestFailure";

const http = (status: number) => new HttpError(status, "", null);

describe("describeFailure by status", () => {
  it("reports a 429 as throttled", () => {
    expect(describeFailure(http(429))).toEqual({
      kind: "throttled",
      status: 429,
      message: "Too many attempts from this network. Wait a minute, then try again.",
      retryable: true,
    });
  });

  it("reports a 422 as invalid and not worth repeating", () => {
    expect(describeFailure(http(422))).toEqual({
      kind: "invalid",
      status: 422,
      message: "The server could not accept that request.",
      retryable: false,
    });
  });

  it("reports a 503 as unavailable", () => {
    expect(describeFailure(http(503))).toEqual({
      kind: "unavailable",
      status: 503,
      message: "This service is unavailable right now. Try again later.",
      retryable: true,
    });
  });

  it.each([502, 504])("reports a proxy %i as unreachable", (status) => {
    expect(describeFailure(http(status))).toEqual({
      kind: "unreachable",
      status,
      message: "Could not reach the server. Check your connection, then try again.",
      retryable: true,
    });
  });

  it("reports any other 5xx as a server problem with its status", () => {
    expect(describeFailure(http(500))).toEqual({
      kind: "server",
      status: 500,
      message: "The server ran into a problem (HTTP 500). Try again in a moment.",
      retryable: true,
    });
    expect(describeFailure(http(501)).message).toBe(
      "The server ran into a problem (HTTP 501). Try again in a moment.",
    );
  });

  it("reports a 401 as unauthorized and not worth repeating", () => {
    expect(describeFailure(new UnauthorizedError())).toEqual({
      kind: "unauthorized",
      status: 401,
      message: "Your session has ended. Sign in again.",
      retryable: false,
    });
  });

  it.each([400, 404, 409])("reports a %i as refused with its status", (status) => {
    expect(describeFailure(http(status))).toEqual({
      kind: "refused",
      status,
      message: `The server refused the request (HTTP ${status}).`,
      retryable: false,
    });
  });

  it("ignores the backend's own detail text", () => {
    const err = new HttpError(429, "Too Many Requests", { detail: "rate limited" });
    expect(describeFailure(err).message).toBe(
      "Too many attempts from this network. Wait a minute, then try again.",
    );
  });

  it("reads the status off a plain object", () => {
    expect(describeFailure({ status: 429 })).toEqual({
      kind: "throttled",
      status: 429,
      message: "Too many attempts from this network. Wait a minute, then try again.",
      retryable: true,
    });
  });

  it("does not take a non-numeric status for an answer", () => {
    expect(describeFailure({ status: "429" }).kind).toBe("unexpected");
  });
});

describe("describeFailure without an answer", () => {
  it("reports a TimeoutError as a timeout", () => {
    expect(describeFailure(new DOMException("No response within 10000 ms", "TimeoutError"))).toEqual({
      kind: "timeout",
      status: null,
      message: "The server did not answer in time. Try again.",
      retryable: true,
    });
  });

  it("reports an AbortError as cancelled", () => {
    expect(describeFailure(new DOMException("The operation was aborted", "AbortError"))).toEqual({
      kind: "aborted",
      status: null,
      message: "The request was cancelled.",
      retryable: true,
    });
  });

  it("reports fetch's TypeError as unreachable", () => {
    expect(describeFailure(new TypeError("Failed to fetch"))).toEqual({
      kind: "unreachable",
      status: null,
      message: "Could not reach the server. Check your connection, then try again.",
      retryable: true,
    });
  });
});

describe("describeFailure for anything else", () => {
  const unexpected = {
    kind: "unexpected",
    status: null,
    message: "Something went wrong. Try again.",
    retryable: true,
  };

  it("reports an unreadable body as unexpected", () => {
    expect(describeFailure(new SyntaxError("Unexpected end of JSON input"))).toEqual(unexpected);
  });

  it("reports a thrown string as unexpected", () => {
    expect(describeFailure("boom")).toEqual(unexpected);
  });

  it("reports null and undefined as unexpected", () => {
    expect(describeFailure(null)).toEqual(unexpected);
    expect(describeFailure(undefined)).toEqual(unexpected);
  });
});

describe("describeFailure with messages", () => {
  it("replaces the text for the kind it names and keeps the rest", () => {
    const messages = { invalid: "Enter a valid email address." };
    expect(describeFailure(http(422), messages)).toEqual({
      kind: "invalid",
      status: 422,
      message: "Enter a valid email address.",
      retryable: false,
    });
    expect(describeFailure(http(429), messages).message).toBe(
      "Too many attempts from this network. Wait a minute, then try again.",
    );
  });
});

/** A failure of every kind describeFailure tells apart. */
const EVERY_KIND: [string, unknown][] = [
  ["a 429", http(429)],
  ["a 422", http(422)],
  ["a 503", http(503)],
  ["a 502", http(502)],
  ["a 500", http(500)],
  ["a 401", new UnauthorizedError()],
  ["a 403", http(403)],
  ["a timeout", new DOMException("No response within 10000 ms", "TimeoutError")],
  ["an abort", new DOMException("The operation was aborted", "AbortError")],
  ["a dropped connection", new TypeError("Failed to fetch")],
  ["anything else", "boom"],
];

describe("linkUnspent", () => {
  // The preview reads a one-time link; only the press spends it.
  it.each(EVERY_KIND)("calls a link unspent after %s met a request that only reads it", (_case, err) => {
    expect(linkUnspent(describeFailure(err), { spends: false })).toBe(true);
  });

  // nginx's 429 refuses the request before the app, which spends the token,
  // sees it.
  it("calls a link unspent after a 429 met a request that spends it", () => {
    expect(linkUnspent(describeFailure(http(429)), { spends: true })).toBe(true);
  });

  // Any other failure may have come after the app spent the token.
  it.each(EVERY_KIND.filter(([name]) => name !== "a 429"))(
    "cannot call a link unspent after %s met a request that spends it",
    (_case, err) => {
      expect(linkUnspent(describeFailure(err), { spends: true })).toBe(false);
    },
  );
});

describe("retryDelayMs", () => {
  const limited = (retryAfterMs: number | null) => new HttpError(429, "Too Many Requests", null, retryAfterMs);

  it.each([
    ["the wait the answer named", limited(12_000), 12_000],
    ["a second for a wait of zero", limited(0), 1_000],
    ["five seconds when the answer named none", limited(null), 5_000],
    ["a minute for anything longer", limited(3_600_000), 60_000],
    ["five seconds for a failure without the field", { status: 429 }, 5_000],
  ])("waits %s", (_case, err, ms) => {
    expect(retryDelayMs(err)).toBe(ms);
  });
});
