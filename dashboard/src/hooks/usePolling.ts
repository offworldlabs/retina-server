import { useCallback, useEffect, useRef, useState } from "react";

/**
 * Fetch-and-show, the shape every dashboard page has: one request on mount,
 * the same request again on a timer, and the last good answer kept on screen
 * while the next one is in flight or has failed.
 *
 * The page hands over the request and reads back the answer. What it no
 * longer owns is the timer, the cleanup, the loading flag and the "settled
 * after unmount" race, which is where the hand-rolled copies drifted.
 */

export interface Polled<T> {
  /** The last successful result, or null before the first one lands. Kept
   *  across a failed refresh, so a page keeps showing what it had. */
  data: T | null;
  /** True until the first request settles: the page-level "Loading…". */
  loading: boolean;
  /** True while the request for the current key is in flight: on mount, after
   *  the key changes and after refresh(). A timed refresh does not set it, so
   *  a page never blanks its own rows to poll. */
  pending: boolean;
  /** The most recent failure, cleared by the next success. Also logged. */
  error: Error | null;
  /** When `data` last changed. */
  updatedAt: Date | null;
  /** Fetch now, outside the schedule. */
  refresh: () => void;
}

/** Which key the last settled answer belongs to; null means none has. */
interface Settled<T> {
  key: string | null;
  data: T | null;
  error: Error | null;
  updatedAt: Date | null;
}

function asError(reason: unknown): Error {
  return reason instanceof Error ? reason : new Error(String(reason));
}

/**
 * `every` is the refresh period in milliseconds; 0 fetches once. `key` names
 * what is being fetched: change it and the hook fetches again at once and
 * restarts the schedule, the way a page keyed on a route parameter or a page
 * number needs. The fetcher itself may change freely; the next request, timed
 * or not, calls the latest one.
 */
export function usePolling<T>(
  fetcher: () => Promise<T>,
  every: number,
  key: string | number = "",
): Polled<T> {
  const [nonce, setNonce] = useState(0);
  // The nonce is part of the key so refresh() is just another key change.
  const runKey = `${key}\u0000${nonce}`;
  const [settled, setSettled] = useState<Settled<T>>({
    key: null,
    data: null,
    error: null,
    updatedAt: null,
  });

  // Read at call time rather than captured by the effect, so a page can pass
  // an inline closure without restarting the schedule on every render.
  const fetcherRef = useRef(fetcher);
  useEffect(() => {
    fetcherRef.current = fetcher;
  });

  useEffect(() => {
    let cancelled = false;
    let nextRequest = 0;
    let latestSettled = 0;
    const run = () => {
      // Ignore completions older than the newest settled request. Requests
      // still in flight must not suppress progress on a slow connection.
      const request = ++nextRequest;
      fetcherRef.current().then(
        (data) => {
          if (cancelled || request < latestSettled) return;
          latestSettled = request;
          setSettled({ key: runKey, data, error: null, updatedAt: new Date() });
        },
        (reason) => {
          if (cancelled || request < latestSettled) return;
          latestSettled = request;
          console.error(reason);
          setSettled((s) => ({ ...s, key: runKey, error: asError(reason) }));
        },
      );
    };
    run();
    const timer = every > 0 ? setInterval(run, every) : undefined;
    return () => {
      cancelled = true;
      if (timer !== undefined) clearInterval(timer);
    };
  }, [every, runKey]);

  const refresh = useCallback(() => setNonce((n) => n + 1), []);

  return {
    data: settled.data,
    loading: settled.key === null,
    pending: settled.key !== runKey,
    error: settled.error,
    updatedAt: settled.updatedAt,
    refresh,
  };
}

/** One request per key, no timer. */
export function useFetch<T>(fetcher: () => Promise<T>, key: string | number = ""): Polled<T> {
  return usePolling(fetcher, 0, key);
}
