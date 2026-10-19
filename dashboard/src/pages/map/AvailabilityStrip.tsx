/**
 * Which hours of the archive hold solves, on the same vendored @edsc/timeline
 * the Data Explorer draws, so an empty hour is visible before it is scrubbed
 * into. Dragging a range or clicking a unit seeks the replay to the middle of
 * it; panning or zooming asks the owner for the availability the view now
 * shows, by its centre and zoom.
 */

import { Component, lazy, Suspense, useMemo, useState, type ReactNode } from "react";
import type { TimelineChange } from "@edsc/timeline";

import { availabilityRows, instantFromSpan, type AvailabilityHourLike } from "./archiveTimeline";
import { ARCHIVE_LAG_MS } from "./replay/useArchiveReplay";

// Its own chunk, as in the Data Explorer. Vite's CommonJS interop hands back
// the component; Node's, under vitest, hands back `module.exports`, whose
// `default` is the component.
const EDSCTimeline = lazy(() =>
  import("@edsc/timeline").then((m) => {
    const exported = m.default as unknown as { default?: typeof m.default };
    return { default: typeof exported === "function" ? m.default : (exported.default ?? m.default) };
  }),
);

/* The library's zoom levels are 0-based units: 0 minute, 1 hour, 2 day. At
   each one the strip spans one unit of the next, so the hour view shows a day
   in 24 distinct bars, the minute view an hour (the loaded window fills about
   half of it), and the day view a month to find a day with data in. */
const INITIAL_ZOOM = 1;
const MIN_ZOOM = 0;
const MAX_ZOOM = 2;

export interface AvailabilityStripProps {
  clockMs: number;
  windowRange: { from: number; to: number } | null;
  availability: {
    hours: AvailabilityHourLike[];
    status: "idle" | "loading" | "ready" | "error";
    error: string | null;
    /** The view settled on this instant at this zoom (see the zoom levels above). */
    onViewCenter(centerMs: number, zoom?: number): void;
  };
  onSeek(ms: number): void;
}

export default function AvailabilityStrip({ clockMs, windowRange, availability, onSeek }: AvailabilityStripProps) {
  // The timeline reports where its view is through every callback and has to
  // be handed that back on each render, or it snaps to where it started. One
  // object, mutated in place, so a pan does not re-render the bar; not a ref,
  // because render reads it. It starts on the replay instant and from then on
  // is the visitor's to move, except that a far seek takes it along.
  const [view, setView] = useState<{ center: number; zoom: number }>(() => ({
    center: clockMs,
    zoom: INITIAL_ZOOM,
  }));
  // A window clear of the last one is a far seek: the view moves to the clock
  // there, at the zoom the visitor left it on. A window that overlaps the last
  // one (playback moving on) leaves the view where the visitor put it.
  // The timeline reads its centre once, at mount, so a far seek remounts it
  // (a new key) to put it there.
  const [viewKey, setViewKey] = useState(0);
  const [seenWindow, setSeenWindow] = useState(windowRange);
  if (windowRange && windowRange !== seenWindow) {
    setSeenWindow(windowRange);
    if (seenWindow && (windowRange.to <= seenWindow.from || windowRange.from >= seenWindow.to)) {
      setView({ center: clockMs, zoom: view.zoom });
      setViewKey(viewKey + 1);
    }
  }
  const sync = (change: TimelineChange) => {
    if (change?.center != null) view.center = change.center;
    if (change?.zoom != null) view.zoom = change.zoom;
  };
  // A pan or a zoom reports where the view is and how wide: a month across
  // shows days a day across does not.
  const report = (change: TimelineChange) => {
    sync(change);
    availability.onViewCenter(view.center, view.zoom);
  };
  // A seek stops at now less ARCHIVE_LAG_MS: the last two minutes are not
  // archived yet, and the replay engine clamps there.
  const archiveEdge = () => Date.now() - ARCHIVE_LAG_MS;
  const seekTo = (ms: number | null) => {
    if (ms !== null) onSeek(ms);
  };

  const { hours } = availability;
  const rows = useMemo(() => availabilityRows(hours ?? []), [hours]);
  const from = windowRange?.from;
  const to = windowRange?.to;
  const temporalRange = useMemo(
    () => (from != null && to != null ? { start: from, end: to } : {}),
    [from, to],
  );

  return (
    <div className="archive-strip">
      <div className="archive-strip-host">
        <StripBoundary>
          <Suspense fallback={<div className="archive-strip-state">Loading timeline…</div>}>
            <EDSCTimeline
              key={viewKey}
              data={rows}
              center={view.center}
              zoom={view.zoom}
              minZoom={MIN_ZOOM}
              maxZoom={MAX_ZOOM}
              temporalRange={temporalRange}
              onTemporalSet={(change) => {
                sync(change);
                seekTo(instantFromSpan(change?.temporalStart, change?.temporalEnd, archiveEdge()));
              }}
              onFocusedSet={(change) => {
                sync(change);
                seekTo(instantFromSpan(change?.focusedStart, change?.focusedEnd, archiveEdge()));
              }}
              onTimelineMoveEnd={report}
              onButtonPan={report}
              onArrowKeyPan={report}
              onDragPan={report}
              onScrollPan={report}
              onButtonZoom={report}
              onScrollZoom={report}
            />
          </Suspense>
        </StripBoundary>
      </div>
      <div className="archive-strip-meta">
        {availability.status === "loading" && (
          <span className="archive-strip-state">Loading availability…</span>
        )}
        {availability.status === "error" && (
          <span className="archive-strip-state is-error">
            {availability.error ? `Couldn't load availability: ${availability.error}` : "Couldn't load availability."}
          </span>
        )}
        <ul className="archive-legend" aria-label="Legend">
          <li>
            <span className="archive-swatch is-solves" aria-hidden="true" /> Solves
          </li>
          <li>
            <span className="archive-swatch is-unclassified" aria-hidden="true" /> Unclassified (fleet unknown)
          </li>
          <li>
            <span className="archive-swatch is-gap" aria-hidden="true" /> No data
          </li>
        </ul>
      </div>
    </div>
  );
}

/** A timeline that fails to load or draw costs the strip, not the bar: the
 *  field, the scrubber and the controls do not depend on it. */
class StripBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;
    return <div className="archive-strip-state is-error">The availability timeline could not be shown.</div>;
  }
}
