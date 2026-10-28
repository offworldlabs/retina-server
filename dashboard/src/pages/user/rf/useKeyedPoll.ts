import { usePolling } from "../../../hooks/usePolling";

/**
 * One answer from an owner route, polled while the page needs it and not
 * asked for at all while it does not.
 *
 * usePolling keeps the last answer across a change of key until the next one
 * lands, and keeps an error until the next success. Turning from one node, or
 * one window, to another would show the first one's answer, or its failure,
 * as the second's; so each answer carries the key it was asked under, and
 * only the current key's answer and failure come back.
 */

/** What to ask for, under a key that tells it apart from whatever the page
 *  may ask for in its place: the node and the window, say. */
export interface KeyedRequest<T> {
  key: string;
  ask: () => Promise<T>;
}

export interface KeyedAnswer<T> {
  key: string;
  value: T;
  /** When it landed, epoch ms. */
  at: number;
}

export interface KeyedPoll<T> {
  /** The current key's latest answer; null until it has one. Never another key's. */
  answer: KeyedAnswer<T> | null;
  /** True until the current key's first answer or failure. */
  loading: boolean;
  /** The failure of the current key's latest request. None while a request
   *  is in flight after a change of key or a retry, as the answer it awaits
   *  would replace it. */
  error: Error | null;
  /** When the answer landed, for FetchNotice; null without one. */
  updatedAt: Date | null;
  refresh: () => void;
}

// Nothing to ask for: settles at once, so no request leaves the page.
const nothing = () => Promise.resolve(null);

/** `every` is the refresh period in milliseconds. */
export function useKeyedPoll<T>(request: KeyedRequest<T> | null, every: number): KeyedPoll<T> {
  const key = request === null ? null : request.key;
  const polled = usePolling<KeyedAnswer<T> | null>(
    request === null
      ? nothing
      : () => request.ask().then((value): KeyedAnswer<T> => ({ key: request.key, value, at: Date.now() })),
    request === null ? 0 : every,
    key === null ? "" : `key:${key}`,
  );
  const answer = key !== null && polled.data?.key === key ? polled.data : null;
  const error = key === null || polled.pending ? null : polled.error;
  return {
    answer,
    loading: key !== null && answer === null && error === null,
    error,
    updatedAt: answer ? polled.updatedAt : null,
    refresh: polled.refresh,
  };
}
