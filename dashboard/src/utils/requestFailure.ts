export type FailureKind =
  | "throttled"
  | "invalid"
  | "unavailable"
  | "unreachable"
  | "timeout"
  | "server"
  | "unauthorized"
  | "refused"
  | "aborted"
  | "unexpected";

export interface RequestFailure {
  kind: FailureKind;
  /** The HTTP status, or null when no answer arrived. */
  status: number | null;
  /** One sentence a card can show as is. */
  message: string;
  /** Whether trying again soon can help. */
  retryable: boolean;
}

const MESSAGES: Record<FailureKind, (status: number | null) => string> = {
  throttled: () => "Too many attempts from this network. Wait a minute, then try again.",
  invalid: () => "The server could not accept that request.",
  unavailable: () => "This service is unavailable right now. Try again later.",
  unreachable: () => "Could not reach the server. Check your connection, then try again.",
  timeout: () => "The server did not answer in time. Try again.",
  server: (status) => `The server ran into a problem (HTTP ${status}). Try again in a moment.`,
  unauthorized: () => "Your session has ended. Sign in again.",
  refused: (status) => `The server refused the request (HTTP ${status}).`,
  aborted: () => "The request was cancelled.",
  unexpected: () => "Something went wrong. Try again.",
};

/** A repeat of the same request would get the same answer. */
const FINAL: ReadonlySet<FailureKind> = new Set(["invalid", "unauthorized", "refused"]);

function kindOfStatus(status: number): FailureKind {
  if (status === 429) return "throttled";
  if (status === 422) return "invalid";
  if (status === 503) return "unavailable";
  // The proxy answers these when the app is down or too slow to answer, so to
  // the user they are the same as no answer at all.
  if (status === 502 || status === 504) return "unreachable";
  if (status === 401) return "unauthorized";
  if (status >= 500 && status < 600) return "server";
  if (status >= 400 && status < 500) return "refused";
  return "unexpected";
}

function statusOf(err: unknown): number | null {
  if (typeof err !== "object" || err === null) return null;
  const status = (err as { status?: unknown }).status;
  return typeof status === "number" && Number.isInteger(status) ? status : null;
}

function nameOf(err: unknown): string | null {
  if (typeof err !== "object" || err === null) return null;
  const name = (err as { name?: unknown }).name;
  return typeof name === "string" ? name : null;
}

/**
 * What a failed request means for the person looking at the page.
 *
 * Every card that makes a request (sign-in, claim, sign-out) reports its
 * failures through here, so a rate limit, a validation error, an app that is
 * down and a connection that dropped read the same wherever they happen, and
 * none of them is shown as the cause of another: a 429 is not "wrong code",
 * a timeout is not "offline".
 *
 * The cases are told apart by the answer's status where there is one, then by
 * the error's `name`, which is how the request helper marks a timeout
 * (TimeoutError) and a caller's abort (AbortError), and how fetch reports a
 * connection that never answered (TypeError). Names rather than `instanceof`
 * because a DOMException from another realm, jsdom's among them, is not an
 * instance of this one's. The status is read off any object that carries one,
 * not only HttpError, so a test can reject with `{ status: 429 }`.
 *
 * `messages` replaces the text for a kind where the page knows more than the
 * status does, such as a 422 from a form with one field; it is shown as given.
 */
export function describeFailure(
  err: unknown,
  messages: Partial<Record<FailureKind, string>> = {},
): RequestFailure {
  const status = statusOf(err);
  let kind: FailureKind;
  if (status !== null) kind = kindOfStatus(status);
  else {
    const name = nameOf(err);
    if (name === "TimeoutError") kind = "timeout";
    else if (name === "AbortError") kind = "aborted";
    else if (name === "TypeError") kind = "unreachable";
    else kind = "unexpected";
  }
  return {
    kind,
    status,
    message: messages[kind] ?? MESSAGES[kind](status),
    retryable: !FINAL.has(kind),
  };
}
