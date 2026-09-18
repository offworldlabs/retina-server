/* The vendored @edsc/timeline (dashboard/vendor/edsc-timeline), typed from its
   propTypes. Only the members this dashboard passes are declared. */
declare module "@edsc/timeline" {
  import type { ComponentType } from "react";

  export interface TimelineRow {
    id: string;
    title: string;
    color: string;
    /** [startMs, endMs] pairs. */
    intervals: [number, number][];
  }

  /** What every callback is handed. */
  export interface TimelineChange {
    center?: number;
    zoom?: number;
    temporalStart?: number;
    temporalEnd?: number;
    focusedStart?: number;
    focusedEnd?: number;
  }

  export interface TimelineProps {
    data: TimelineRow[];
    center?: number;
    zoom?: number;
    minZoom?: number;
    maxZoom?: number;
    temporalRange?: { start?: number; end?: number };
    focusedInterval?: { start?: number; end?: number };
    onTemporalSet?: (change: TimelineChange) => void;
    onFocusedSet?: (change: TimelineChange) => void;
    onTimelineMoveEnd?: (change: TimelineChange) => void;
    onButtonZoom?: (change: TimelineChange) => void;
    onScrollZoom?: (change: TimelineChange) => void;
    onButtonPan?: (change: TimelineChange) => void;
    onArrowKeyPan?: (change: TimelineChange) => void;
    onDragPan?: (change: TimelineChange) => void;
    onScrollPan?: (change: TimelineChange) => void;
  }

  const EDSCTimeline: ComponentType<TimelineProps>;
  export default EDSCTimeline;
}
