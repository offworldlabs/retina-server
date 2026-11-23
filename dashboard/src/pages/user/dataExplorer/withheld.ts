/**
 * The private nodes this document has been told of, and whose list told it.
 *
 * Held here rather than by the page, which the console remounts on every
 * change of user: the session after is the one that needs it most. It
 * inherits the address bar the last one left, whose link can name the last
 * owner's private nodes, and a visitor has no list of their own to tell those
 * from anyone else's. It lasts as long as that address bar, the document, and
 * is only ever read to hold refs back, never shown.
 */

const told = new Map<string, ReadonlySet<string>>();

const NONE: ReadonlySet<string> = new Set();

/** Records a viewer's private nodes as their latest list has them. */
export function rememberPrivate(viewer: string, refs: ReadonlySet<string>): void {
  told.set(viewer, refs);
}

export interface Withheld {
  /** Every other viewer's private nodes. */
  others: ReadonlySet<string>;
  /** This viewer's own, as their last list in this document had them. */
  own: ReadonlySet<string>;
}

/** What the sessions this document has seen say to hold back from `viewer`. */
export function withheldFrom(viewer: string): Withheld {
  const others = new Set<string>();
  for (const [who, refs] of told) {
    if (who === viewer) continue;
    for (const ref of refs) others.add(ref);
  }
  return { others, own: told.get(viewer) ?? NONE };
}

/** Forgets every list, as a new document starts out. The tests in one file
 *  share a document, so each starts with this. */
export function forgetPrivate(): void {
  told.clear();
}
