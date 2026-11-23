import { useEffect, useId, useRef, type ReactNode } from "react";
import { FetchNotice, nothingLoaded } from "../../Notice";
import { useFetch, type Polled } from "../../../hooks/usePolling";
import { isGone } from "../gone";

/** One owner route for one node, fetched again whenever `refreshKey` moves:
 *  the node page bumps it after the owner edits the node, so an edit shows up
 *  here without a reload. */
export function useRecord<T>(fetcher: () => Promise<T>, nodeId: string, refreshKey: number): Polled<T> {
  return useFetch(fetcher, `${nodeId}\u0000${refreshKey}`);
}

interface RecordCardProps<T> {
  title: string;
  /** What the failure notice calls the record: "the last report". */
  what: string;
  polled: Polled<T>;
  /** Called when the route answers 404: the node is no longer on the caller's
   *  account, which is the page's to say, not this card's. */
  onGone: () => void;
  /** Small print beside the title. Given the answer, once there is one. */
  aside?: (data: T) => ReactNode;
  /** The answer, drawn. `data` may legitimately be null, as a report is for a
   *  node that has never sent one, so it is handed over as the route typed it. */
  children: (data: T) => ReactNode;
}

/** A card for one record, which waits, fails and refreshes on its own: a route
 *  that fails costs its own card and never the cards beside it. A 404 is the
 *  exception, as it is for the node itself: it hands the node to the page. */
export function RecordCard<T>({ title, what, polled, onGone, aside, children }: RecordCardProps<T>) {
  const headingId = useId();
  // Read when the failure lands rather than captured, so the page can pass an
  // inline closure without the card calling it again on every render.
  const onGoneRef = useRef(onGone);
  useEffect(() => {
    onGoneRef.current = onGone;
  });
  const gone = isGone(polled.error);
  useEffect(() => {
    if (gone) onGoneRef.current();
  }, [gone, polled.error]);
  // The first answer, or a retry after a first failure: there is nothing to
  // show yet, and the notice would only repeat itself until the retry lands.
  const waiting = polled.loading || (polled.pending && polled.updatedAt === null);
  const answered = !waiting && !gone && !nothingLoaded(polled);
  const data = polled.data as T;
  const note = answered && aside ? aside(data) : null;

  let body: ReactNode;
  if (gone) {
    // Not an error of the card's own: the page takes it down under its note.
    body = null;
  } else if (waiting) {
    body = <p className="node-records-note">Loading…</p>;
  } else if (!answered) {
    body = <FetchNotice polled={polled} what={what} />;
  } else {
    // A failed refresh keeps the last answer up, under a notice that says how
    // old it is.
    body = (
      <>
        <FetchNotice polled={polled} what={what} />
        {children(data)}
      </>
    );
  }

  return (
    <section className="card node-records-card" aria-labelledby={headingId} aria-busy={polled.pending}>
      <div className="card-header">
        <h3 id={headingId}>{title}</h3>
        {note != null && note !== false && <div className="card-aside">{note}</div>}
      </div>
      <div className="card-body">{body}</div>
    </section>
  );
}
