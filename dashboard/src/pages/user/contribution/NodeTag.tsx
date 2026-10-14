import { YoursBadge } from "../../../components/YoursBadge";

/** A node as a ranking names it, tagged when it is the owner's own. */
export function NodeTag({ name, mine }: { name: string; mine: boolean }) {
  return (
    <span className="rank-name">
      <span className="mono">{name}</span>
      {mine && <YoursBadge />}
    </span>
  );
}

/** One series' colour in a chart's key. The colour comes from the chart theme,
 *  which Recharts reads in place of the stylesheet. */
export function Swatch({ colour }: { colour: string }) {
  return (
    <svg className="contribution-swatch" viewBox="0 0 10 10" aria-hidden="true" focusable="false">
      <rect width="10" height="10" rx="2" fill={colour} />
    </svg>
  );
}
