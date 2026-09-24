/**
 * The day scan: which days still need listing, how many list at once, and what
 * each one's state is. Listing starts from an effect, never from a render: it is
 * the synchronous `loading` entry written before the fetch runs that stops a
 * second pass over `days` from re-queueing one already claimed. Under a
 * development double mount, the first setup's fetch is aborted and the second
 * setup re-runs it against a fresh, empty map of entries; the abort path writes
 * no entry for exactly that reason, leaving the day to be picked up again
 * rather than stranded on a claim nothing will clear.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { cacheKey, type DayEntry, entryForScope, fetchDayListing } from "./archive";
import type { ArchiveFile } from "./keys";

/** Enough to hide the latency of a range, few enough to leave the API to the
 *  rest of the page. */
export const MAX_INFLIGHT = 3;

/** Stands in for "nothing selected" in the cache guard. A shared, stable
 *  instance, so the effect's dependency list does not see a new object on
 *  every render when there is no selection. */
const EMPTY_SELECTION: Set<string> = new Set();

export interface Scan {
  /** Per day-and-scope, keyed by `cacheKey`. */
  entries: Map<string, DayEntry>;
  /** Every file seen, by archive key. Never pruned: a selection made earlier
   *  must survive a filter change that hides the file. */
  files: Map<string, ArchiveFile>;
  /** Every node seen in a key, including ones the registry no longer lists. */
  nodeIds: Set<string>;
  retry: (day: string) => void;
}

export function useArchiveScan(days: string[], nodeSel: Set<string> | null): Scan {
  const [entries, setEntries] = useState<Map<string, DayEntry>>(new Map());
  const filesRef = useRef<Map<string, ArchiveFile>>(new Map());
  const [nodeIds, setNodeIds] = useState<Set<string>>(new Set());

  // Read by the effect without being part of what re-runs it.
  const entriesRef = useRef(entries);
  entriesRef.current = entries;

  const inflight = useRef(0);
  const queue = useRef<(() => Promise<void>)[]>([]);
  const aborts = useRef<Set<AbortController>>(new Set());

  const pump = useCallback(() => {
    while (inflight.current < MAX_INFLIGHT && queue.current.length) {
      const task = queue.current.shift();
      inflight.current += 1;
      task().finally(() => {
        inflight.current -= 1;
        pump();
      });
    }
  }, []);

  const start = useCallback(
    (day: string, nodeRef: string | null) => {
      const key = cacheKey(day, nodeRef);
      // Written before the task runs, so the next pass over `days` sees this
      // day as claimed rather than queueing it again.
      setEntries((prev) => new Map(prev).set(key, { day, nodeRef, status: "loading", files: [] }));

      queue.current.push(async () => {
        const controller = new AbortController();
        aborts.current.add(controller);
        try {
          const files = await fetchDayListing(day, nodeRef, controller.signal);
          for (const f of files) filesRef.current.set(f.key, f);
          setNodeIds((prev) => {
            const next = new Set(prev);
            for (const f of files) next.add(f.node);
            return next.size === prev.size ? prev : next;
          });
          setEntries((prev) => new Map(prev).set(key, { day, nodeRef, status: "done", files }));
        } catch (e) {
          if (controller.signal.aborted) return;
          setEntries((prev) =>
            new Map(prev).set(key, {
              day,
              nodeRef,
              status: "error",
              files: [],
              error: e instanceof Error ? e.message : String(e),
            }),
          );
        } finally {
          aborts.current.delete(controller);
        }
      });
      pump();
    },
    [pump],
  );

  // A selection of exactly one node earns the scoped listing, which is an
  // order of magnitude faster. Having seen only one node is not the same
  // thing: before anything is selected the whole day is what is wanted.
  const only = nodeSel && nodeSel.size === 1 ? Array.from(nodeSel)[0] : null;

  // What a cached listing has to cover to answer, which is what was asked
  // for and not what has been seen. With no selection only a whole-day
  // listing will do: one node's listing cannot stand in for every node,
  // however few have been discovered.
  const scope = nodeSel || EMPTY_SELECTION;

  useEffect(() => {
    for (const day of days) {
      if (entryForScope(entriesRef.current, day, scope)) continue;
      start(day, only);
    }
  }, [days, scope, only, start]);

  useEffect(() => {
    const controllers = aborts.current;
    return () => {
      for (const c of controllers) c.abort();
      controllers.clear();
      queue.current = [];
    };
  }, []);

  const retry = useCallback((day: string) => start(day, only), [only, start]);

  return { entries, files: filesRef.current, nodeIds, retry };
}
