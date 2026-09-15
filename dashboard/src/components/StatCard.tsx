import type { ReactNode } from "react";

export type StatTone = "accent" | "success" | "warning" | "error";

interface Props {
  label: ReactNode;
  value: ReactNode;
  /** Colours the value with a status token; without one it stays in the
   *  text colour. */
  tone?: StatTone;
  /** A smaller line under the value. */
  sub?: ReactNode;
  /** Set beside the value at a fraction of its size. */
  unit?: ReactNode;
  /** `small` for a reading that is words rather than a number. */
  size?: "small";
}

/** One tile of a stats-grid: a muted uppercase label over a large value. The
 *  markup is App.css's `.stat-card` vocabulary, so the rules there apply. */
export function StatCard({ label, value, tone, sub, unit, size }: Props) {
  return (
    <div className={tone ? `stat-card ${tone}` : "stat-card"}>
      <div className="stat-label">{label}</div>
      <div className={size ? `stat-value ${size}` : "stat-value"}>
        {value}
        {unit != null && <span className="stat-unit">{unit}</span>}
      </div>
      {sub != null && <div className="stat-sub">{sub}</div>}
    </div>
  );
}
