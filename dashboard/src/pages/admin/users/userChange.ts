/** What an action on an account did, for the page that lists or shows it. */
export type UserChange =
  | { kind: "deactivated" }
  | { kind: "reactivated" }
  /** The account is gone; `released` is how many nodes it owned were handed back. */
  | { kind: "deleted"; released: number }
  /** A confirmation link went to `email`. The account keeps its address
   *  until the link is opened. */
  | { kind: "email-sent"; email: string }
  /** The server answered 404: the account was deleted elsewhere. */
  | { kind: "gone" };

/** A thrown value as a line a page can show. */
export function messageOf(error: unknown): string {
  return error instanceof Error && error.message ? error.message : String(error);
}

/** `text` with a full stop, unless it already ends a sentence. */
export function sentence(text: string): string {
  return /[.!?]$/.test(text) ? text : `${text}.`;
}
