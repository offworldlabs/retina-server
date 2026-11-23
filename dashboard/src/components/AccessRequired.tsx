import { Link } from "react-router-dom";
import { HttpError } from "@retina/shared";
import "./AccessRequired.css";

const REFUSAL_REASONS: ReadonlySet<unknown> = new Set(["not_entitled", "lapsed", "grant_ended"]);

/** The server's own sentence when `error` is its refusal of the data API to a
 *  signed-in account: a 403 whose detail names why. Any other failure is not
 *  one, and gets null. */
export function accessRefusal(error: unknown): string | null {
  if (!(error instanceof HttpError) || error.status !== 403) return null;
  const detail = (error.body as { detail?: unknown } | null)?.detail;
  if (typeof detail !== "object" || detail === null) return null;
  const { reason, message } = detail as { reason?: unknown; message?: unknown };
  return REFUSAL_REASONS.has(reason) && typeof message === "string" ? message : null;
}

/** What a network-wide page says in place of an error when the data API is
 *  closed to the account: why, and the way to ask for access. */
export function AccessRequired({ message }: { message: string }) {
  return (
    <div className="notice access-required" role="status">
      <div className="notice-text">
        <h2 className="access-required-title">API access required</h2>
        <p className="access-required-message">{message}</p>
      </div>
      <Link to="/api-access" className="btn btn-primary">
        API access
      </Link>
    </div>
  );
}
