/**
 * The day scan: which days still need listing, how many list at once, and what
 * each one's state is. Listing starts from an effect, never from a render: it is
 * the synchronous `loading` entry written before the fetch runs that stops a
 * second pass over `days` from re-queueing one already claimed. Under a
 * development double mount, the first setup's fetch is aborted and the second
 * setup re-runs it against a fresh, empty map of entries; the abort path writes
 * no entry for exactly that reason, leaving the day to be picked up again
 * rather than stranded on a claim nothing will clear.
 *
 * Each archive keeps a cache of its own: the owner's listing of a day and the
 * public one are different answers, and neither may stand in for the other.
 * The files themselves are one map, since a key names the same file whichever
 * route listed it, so a basket filled in one view still resolves in the other.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  type ArchiveSource,
  cacheKey,
  type DayEntry,
  entryForScope,
  fetchDayListing,
  type Listed,
  listedKeys,
} from "./archive";
import type { ArchiveFile } from "./keys";

/** Enough to hide the latency of a range, few enough to leave the API to the
 *  rest of the page. */
export const MAX_INFLIGHT = 3;

/** Stands in for "nothing selected" in the cache guard. A shared, stable
 *  instance, so the effect's dependency list does not see a new object on
 *  every render when there is no selection. */
const EMPTY_SELECTION: Set<string> = new Set();

/** What a scan with no archive to read yet answers: nothing listed, nothing
 *  seen. Stable, for the same reason. */
const NO_ENTRIES: Map<string, DayEntry> = new Map();
const NO_NODE_IDS: Set<string> = new Set();

type BySource<T> = Record<ArchiveSource, T>;

/** One day's listing, queued or in flight, and the claim it holds. */
interface Claim {
  source: ArchiveSource;
  key: string;
}

interface Task extends Claim {
  run: () => Promise<void>;
}

export interface Scan {
  /** Per day-and-scope, keyed by `cacheKey`, for the archive being read. */
  entries: Map<string, DayEntry>;
  /** Every file seen, by archive key, from either archive. Never pruned: a
   *  selection made earlier must survive a filter change that hides the file. */
  files: Map<string, ArchiveFile>;
  /** Every node seen in a key of the archive being read, including ones the
   *  registry no longer lists. */
  nodeIds: Set<string>;
  /** Which archive listed which key, whichever is being read: a file is
   *  fetched through a route whose archive listed it. */
  listed: Listed;
  retry: (day: string) => void;
}

/**
 * `source` is the archive to list; null holds the scan, for a page that cannot
 * yet tell which one it wants and must not spend requests on a guess.
 */
export function useArchiveScan(
  days: string[],
  nodeSel: Set<string> | null,
  source: ArchiveSource | null = "public",
): Scan {
  const [entries, setEntries] = useState<BySource<Map<string, DayEntry>>>(() => ({
    public: new Map(),
    owner: new Map(),
  }));
  // One map for the hook's life, filled as listings land and handed out as is.
  const [files] = useState(() => new Map<string, ArchiveFile>());
  const [nodeIds, setNodeIds] = useState<BySource<Set<string>>>(() => ({
    public: new Set(),
    owner: new Set(),
  }));

  // Read by the effect without being part of what re-runs it.
  // Declared before the scan effect so it has caught up by the time that runs.
  const entriesRef = useRef(entries);
  useEffect(() => {
    entriesRef.current = entries;
  }, [entries]);

  const inflight = useRef(0);
  const queue = useRef<Task[]>([]);
  const aborts = useRef<Map<AbortController, Claim>>(new Map());

  const pump = useCallback(() => {
    while (inflight.current < MAX_INFLIGHT && queue.current.length) {
      const task = queue.current.shift();
      inflight.current += 1;
      task.run().finally(() => {
        inflight.current -= 1;
        pump();
      });
    }
  }, []);

  const start = useCallback(
    (day: string, nodeRef: string | null, from: ArchiveSource) => {
      const key = cacheKey(day, nodeRef);
      const write = (entry: DayEntry) =>
        setEntries((prev) => ({ ...prev, [from]: new Map(prev[from]).set(key, entry) }));

      // Written before the task runs, so the next pass over `days` sees this
      // day as claimed rather than queueing it again.
      write({ day, nodeRef, status: "loading", files: [] });

      queue.current.push({
        source: from,
        key,
        run: async () => {
          const controller = new AbortController();
          aborts.current.set(controller, { source: from, key });
          try {
            const listing = await fetchDayListing(day, nodeRef, controller.signal, from);
            for (const f of listing) files.set(f.key, f);
            setNodeIds((prev) => {
              const next = new Set(prev[from]);
              for (const f of listing) next.add(f.node);
              return next.size === prev[from].size ? prev : { ...prev, [from]: next };
            });
            write({ day, nodeRef, status: "done", files: listing });
          } catch (e) {
            if (controller.signal.aborted) return;
            write({
              day,
              nodeRef,
              status: "error",
              files: [],
              error: e instanceof Error ? e.message : String(e),
            });
          } finally {
            aborts.current.delete(controller);
          }
        },
      });
      pump();
    },
    [files, pump],
  );

  // Leaving an archive leaves its work: what is queued is dropped and what is
  // in flight is aborted, so the one now wanted is not kept waiting behind a
  // range nobody is looking at. Their claims are released at the same time,
  // so coming back lists those days again rather than reading them as busy.
  // A listing that landed meanwhile is kept: it is still that archive's answer.
  //
  // An aborted listing is struck off here rather than by its own `finally`,
  // which runs a microtask later: until then a second release would release
  // it again. And the state is left as it was when no claim was still
  // loading, so a release with nothing to do renders nothing; otherwise an
  // effect that runs on every render would never stop releasing.
  const release = useCallback((keep: ArchiveSource) => {
    const released: Claim[] = queue.current.filter((task) => task.source !== keep);
    if (released.length) queue.current = queue.current.filter((task) => task.source === keep);
    for (const [controller, claim] of aborts.current) {
      if (claim.source === keep) continue;
      released.push(claim);
      aborts.current.delete(controller);
      controller.abort();
    }
    if (!released.length) return;

    setEntries((prev) => {
      let next = prev;
      for (const { source: from, key } of released) {
        if (next[from].get(key)?.status !== "loading") continue;
        if (next === prev) next = { ...prev };
        if (next[from] === prev[from]) next[from] = new Map(prev[from]);
        next[from].delete(key);
      }
      return next;
    });
  }, []);

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
    if (source === null) return;
    release(source);
    for (const day of days) {
      if (entryForScope(entriesRef.current[source], day, scope)) continue;
      start(day, only, source);
    }
  }, [days, scope, only, source, start, release]);

  useEffect(() => {
    const controllers = aborts.current;
    return () => {
      for (const c of controllers.keys()) c.abort();
      controllers.clear();
      queue.current = [];
    };
  }, []);

  const retry = useCallback(
    (day: string) => {
      if (source !== null) start(day, only, source);
    },
    [only, source, start],
  );

  // Per archive, so a listing landing in one does not rebuild the other's.
  const listedPublic = useMemo(() => listedKeys(entries.public), [entries.public]);
  const listedOwner = useMemo(() => listedKeys(entries.owner), [entries.owner]);
  const listed = useMemo(() => ({ public: listedPublic, owner: listedOwner }), [listedPublic, listedOwner]);

  return {
    entries: source === null ? NO_ENTRIES : entries[source],
    files,
    nodeIds: source === null ? NO_NODE_IDS : nodeIds[source],
    listed,
    retry,
  };
}
