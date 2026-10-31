/**
 * The availability card: one bar per archived hour, on the vendored
 * @edsc/timeline. Dragging a range or focusing a day writes the date filters,
 * so the timeline is a second way to set what the date controls set.
 */

import { Component, lazy, Suspense, useMemo, useReducer, useRef, useState, type ReactNode } from "react";
import type { TimelineChange } from "@edsc/timeline";

import { useMediaQuery } from "../../../hooks/useMediaQuery";
import { COARSE_TABLET, PHONE } from "../../../styles/breakpoints";
import type { ArchiveFile } from "./keys";
import {
  clearRange,
  filtersFromFocus,
  filtersFromTemporal,
  MAX_ROWS,
  panStep,
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

/** Screens a finger drives: any phone, and a tablet with a coarse pointer,
 *  the same set styles/phone.css gives 44px targets. */
const TOUCH = `${PHONE}, ${COARSE_TABLET}`;

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
  const touch = useMediaQuery(TOUCH);
  const host = useRef<HTMLDivElement>(null);

  // Earlier and Later move the view by remounting the timeline under a new
  // key. The component reads `center` only as it mounts: once it has centred
  // itself it follows its own gestures and ignores the prop. A mount reads the
  // zoom and the range from the props below too, so a move keeps both, and
  // clears a focused day as the component's own zoom does. A remounted
  // timeline draws its first frames from the start of its list before it
  // centres, so the strip stays hidden (dataExplorer.css) from the press
  // until the timeline first reports where it drew itself, which is when
  // `landed` catches up with `moves`.
  const [moves, setMoves] = useState(0);
  const [landed, setLanded] = useState(0);
  const moving = moves !== landed;

  const sync = (change: TimelineChange) => {
    if (change?.center != null) view.center = change.center;
    if (change?.zoom != null) view.zoom = change.zoom;
    if (moving) setLanded(moves);
  };

  const rows = useMemo(
    () => timelineRows(ids, files, synthetic, anySynthetic),
    [ids, files, synthetic, anySynthetic],
  );
  const range = temporalRangeFor(filters);
  const home = (range.start + range.end) / 2;
  if (view.center === null) view.center = home;

  // Most of what the strip shows, measured as the button is pressed: the zoom
  // controls cover more of a narrow strip, and more again while a day is
  // focused.
  const move = (direction: 1 | -1) => {
    const strip = host.current;
    const covered = strip?.querySelector(".edsc-timeline-tools")?.getBoundingClientRect().width ?? 0;
    const step = panStep(view.zoom, strip?.clientWidth ?? 0, covered);
    view.center = (view.center ?? home) + direction * step;
    setMoves((n) => n + 1);
  };

  // A swipe the browser takes for a page scroll ends in pointercancel, which
  // the timeline's gesture library treats as the end of a drag: a vertical
  // swipe that began on the top strip would set the range to the sliver it
  // crossed, or clear it. The host sees the cancel before the strip does and
  // again after it, so a range reported in between is dropped, and the strip
  // redrawn with the filters' own.
  const scrolling = useRef(false);
  const [, redraw] = useReducer((n: number) => n + 1, 0);

  const onTemporalSet = (change: TimelineChange) => {
    sync(change);
    if (scrolling.current) {
      redraw();
      return;
    }
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
      <div
        ref={host}
        className="de-tl-host"
        data-moving={moving || undefined}
        onPointerCancelCapture={
          touch
            ? () => {
                scrolling.current = true;
              }
            : undefined
        }
        onPointerCancel={
          touch
            ? () => {
                scrolling.current = false;
              }
            : undefined
        }
      >
        <TimelineBoundary>
          <Suspense fallback={<div className="de-tl-state">Loading availability timeline…</div>}>
            <EDSCTimeline
              key={moves}
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
      {touch && (
        <div className="de-tl-nav">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            aria-label="Show earlier"
            onClick={() => move(-1)}
          >
            ← Earlier
          </button>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            aria-label="Show later"
            onClick={() => move(1)}
          >
            Later →
          </button>
        </div>
      )}
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
          {touch ? (
            <>
              Drag along the <b>top strip</b> to set the date range · tap a <b>date label</b> to
              focus that day · drag the middle or use Earlier and Later to move · ▲▼ to zoom
              (hour → month). The component draws at most {MAX_ROWS} rows.
            </>
          ) : (
            <>
              Drag along the <b>top strip</b> to set the date range · click a <b>date label</b> to
              focus that day · drag the middle to pan · wheel or ▲▼ to zoom (hour → month). The
              component draws at most {MAX_ROWS} rows.
            </>
          )}
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
