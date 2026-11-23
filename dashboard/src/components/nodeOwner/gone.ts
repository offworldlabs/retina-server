import { HttpError } from "@retina/shared";

/** The owner routes answer 404 for a node no longer on the caller's account,
 *  handed back or reassigned since the page read it. */
export function isGone(e: unknown): boolean {
  return e instanceof HttpError && e.status === 404;
}
