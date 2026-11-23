/* ------------------------------------------------------------------ */
/*  One external store per stretch a hook is active: made on the      */
/*  render that turns it on, read through useSyncExternalStore, and   */
/*  disposed when it turns off or the component unmounts.             */
/* ------------------------------------------------------------------ */

import { useEffect, useState, useSyncExternalStore } from "react";

/** A store the hook reads; its members are handed to React detached, so they must be bound. */
export interface StoreSession<S> {
  readonly subscribe: (onChange: () => void) => () => void;
  readonly getSnapshot: () => S;
  /**
   * Cancels whatever it has out or scheduled. Under React.StrictMode the
   * dispose effect also runs once on a session that stays in use, so this must
   * leave the store able to resume on its next subscribe or sync.
   */
  readonly dispose: () => void;
}

const noWatch = () => () => {};

/**
 * The session `create` makes while `active`, and its snapshot; null and `idle`
 * while inactive. A new stretch of activity gets a new session.
 */
export function useStoreSession<S, T extends StoreSession<S>>(
  active: boolean,
  create: () => T,
  idle: S,
): [session: T | null, snapshot: S] {
  const [session, setSession] = useState<T | null>(() => (active ? create() : null));
  // Swapped in the render that sees `active` change, so the two never disagree.
  if (active !== (session !== null)) setSession(active ? create() : null);

  const snapshot = useSyncExternalStore(session ? session.subscribe : noWatch, session ? session.getSnapshot : () => idle);

  useEffect(() => (session ? session.dispose : undefined), [session]);

  return [session, snapshot];
}
