import { anomalyColour, useChartTheme } from "../../../utils/chartTheme";

/** An anomaly type as words: the backend's reason with its underscores
 *  spaced, and "unknown" for an event or a flag that names none. */
export function anomalyTypeLabel(type: string | null | undefined): string {
  return (type || "unknown").replace(/_/g, " ");
}

/** An anomaly type in its own colour, the one its bar has in the breakdown
 *  chart, so a type reads the same in every table and chart on the page. */
export function AnomalyTypeBadge({ type }: { type: string | null | undefined }) {
  const chart = useChartTheme();
  return (
    <span className="badge" style={{ background: anomalyColour(chart, type ?? undefined), color: chart.anomaly.ink }}>
      {anomalyTypeLabel(type)}
    </span>
  );
}
