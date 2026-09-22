import { useId, type ReactNode } from "react";

/** Where a filling resource turns amber and then red, in percent. One pair for
 *  every bar, so a colour means the same headroom on every page. */
export const USAGE_WARNING_PCT = 70;
export const USAGE_ERROR_PCT = 90;

export type UsageTone = "success" | "warning" | "error";

export function usageTone(pct: number): UsageTone {
  if (pct > USAGE_ERROR_PCT) return "error";
  if (pct > USAGE_WARNING_PCT) return "warning";
  return "success";
}

interface Props {
  label: ReactNode;
  /** Set to the right of the label, muted: the reading in words. */
  value: ReactNode;
  /** How full, 0–100. Null for a reading that is missing, which leaves the
   *  track empty rather than drawing it as zero. */
  pct: number | null;
  /** A muted line under the bar. */
  note?: ReactNode;
}

/** A labelled horizontal bar for how full something is: a disk, a queue, a
 *  droplet's CPU. The markup is App.css's `.usage-bar`. */
export function UsageBar({ label, value, pct, note }: Props) {
  const labelId = useId();
  const fill = pct === null ? 0 : Math.max(0, Math.min(100, pct));
  return (
    <div className="usage-bar">
      <div className="usage-bar-head">
        <span id={labelId}>{label}</span>
        <span className="usage-bar-value">{value}</span>
      </div>
      <div
        className="usage-bar-track"
        role="progressbar"
        aria-labelledby={labelId}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct === null ? undefined : Math.round(fill)}
      >
        {pct !== null && <div className={`usage-bar-fill ${usageTone(pct)}`} style={{ width: `${fill}%` }} />}
      </div>
      {note != null && <div className="usage-bar-note">{note}</div>}
    </div>
  );
}
