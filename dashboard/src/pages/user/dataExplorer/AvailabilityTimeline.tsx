/**
 * The availability card: one bar per archived hour, on the vendored
 * @edsc/timeline. Dragging a range or focusing a day writes the date filters,
 * so the timeline is a second way to set what the date controls set.
 */

import { Component, lazy, Suspense, useMemo, useState, type ReactNode } from "react";
import type { TimelineChange } from "@edsc/timeline";

import type { ArchiveFile } from "./keys";
import {
  clearRange,
  filtersFromFocus,
  filtersFromTemporal,
  MAX_ROWS,
  rangeLabel,
  temporalRangeFor,
  timelineRows,
} from "./timeline";
import type { ExplorerFilters } from "./urlState";

// Its own chunk, which the rest of the page does not wait for. Vite's CommonJS
// interop hands back the component; Node's, under vitest, hands back
// `module.exports`, whose `default` is the component.
const EDSCTimeline = lazy(() =>
  import("@edsc/timeline").then((m) => {
    const exported = m.default as unknown as { default?: typeof m.default };
    return { default: typeof exported === "function" ? m.default : (exported.default ?? m.default) };
  }),
);

/** Day view: hour and month are a wheel turn either side. */
const INITIAL_ZOOM = 2;

interface Props {
  filters: ExplorerFilters;
  today: string;
  /** The nodes the filters admit, sorted. */
  ids: string[];
  /** Files passing every filter. */
  files: ArchiveFile[];
  synthetic: (id: string) => boolean;
  /** Whether any known node is synthetic, selected or not. */
  anySynthetic: boolean;
  onChange: (next: ExplorerFilters) => void;
}

export function AvailabilityTimeline({
  filters,
  today,
  ids,
  files,
  synthetic,
  anySynthetic,
  onChange,
}: Props) {
  // Every callback reports where the view is, and the view has to be handed
  // back on each render or it snaps to where it started. One object for the
  // component's lifetime, mutated in place and never set: feeding seven view
  // callbacks through state would re-render on every pan frame. Not a ref,
  // because it is read during render, which react-hooks/refs rejects.
  const [view] = useState<{ center: number | null; zoom: number }>(() => ({
    center: null,
    zoom: INITIAL_ZOOM,
  }));
  const sync = (change: TimelineChange) => {
    if (change?.center != null) view.center = change.center;
    if (change?.zoom != null) view.zoom = change.zoom;
  };

  const rows = useMemo(
    () => timelineRows(ids, files, synthetic, anySynthetic),
    [ids, files, synthetic, anySynthetic],
  );
  const range = temporalRangeFor(filters);
  if (view.center === null) view.center = (range.start + range.end) / 2;

  const onTemporalSet = (change: TimelineChange) => {
    sync(change);
    onChange(filtersFromTemporal(filters, change ?? {}, today));
  };
  const onFocusedSet = (change: TimelineChange) => {
    sync(change);
    const next = filtersFromFocus(filters, change ?? {}, today);
    if (next) onChange(next);
  };

  return (
    <div className="card de-timeline">
      <div className="card-header">
        <h3>Availability, one bar per archived hour</h3>
        <div className="de-frow">
          <span className="de-muted de-tl-range" data-testid="de-tl-range">
            {rangeLabel(filters)}
          </span>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => onChange(clearRange(filters, today))}
          >
            Clear range
          </button>
        </div>
      </div>
      <div className="de-tl-host">
        <TimelineBoundary>
          <Suspense fallback={<div className="de-tl-state">Loading availability timeline…</div>}>
            <EDSCTimeline
              data={rows}
              center={view.center}
              zoom={view.zoom}
              minZoom={1}
              maxZoom={3}
              temporalRange={range}
              onTemporalSet={onTemporalSet}
              onFocusedSet={onFocusedSet}
              onTimelineMoveEnd={sync}
              onButtonZoom={sync}
              onScrollZoom={sync}
              onButtonPan={sync}
              onArrowKeyPan={sync}
              onDragPan={sync}
              onScrollPan={sync}
            />
          </Suspense>
        </TimelineBoundary>
      </div>
      <div className="de-tl-legend">
        <span className="de-swatches">
          <span className="de-sw de-sw-real" aria-hidden="true" /> {anySynthetic ? "real node" : "node"}
        </span>
        {anySynthetic && (
          <span className="de-swatches">
            <span className="de-sw de-sw-synth" aria-hidden="true" /> synth node
          </span>
        )}
        <span className="de-swatches">
          <span className="de-sw de-sw-range" aria-hidden="true" /> selected range
        </span>
        <span>
          Drag along the <b>top strip</b> to set the date range · click a <b>date label</b> to
          focus that day · drag the middle to pan · wheel or ▲▼ to zoom (hour → month). The
          component draws at most {MAX_ROWS} rows.
          {ids.length > MAX_ROWS && ` Pick up to ${MAX_ROWS} nodes for one row each.`}
        </span>
      </div>
    </div>
  );
}

/** A timeline that fails to load or render costs the card, not the page: the
 *  results below it do not depend on it. */
class TimelineBoundary extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null };

  static getDerivedStateFromError(error: unknown) {
    return { error: error instanceof Error ? error.message : String(error) };
  }

  render() {
    if (this.state.error === null) return this.props.children;
    return (
      <div className="de-tl-state err" role="alert">
        Could not load the availability timeline ({this.state.error}). The results list below
        still works.
      </div>
    );
  }
}
