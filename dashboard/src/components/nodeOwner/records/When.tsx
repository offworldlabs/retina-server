import { DASH, formatRelativeTime } from "../../../utils/format";

/** A moment as how long ago it was, with the exact local time under it: the
 *  relative reading is what a glance wants, and the exact one is what a
 *  report to anyone else needs. Both are drawn, since a title tooltip never
 *  reaches a touch screen or a keyboard. A stamp that does not parse is as
 *  missing as an absent one. */
export function When({ at }: { at: string | null | undefined }) {
  const ms = at ? Date.parse(at) : NaN;
  if (!at || Number.isNaN(ms)) return <>{DASH}</>;
  const moment = new Date(ms);
  return (
    <time className="node-records-when" dateTime={moment.toISOString()}>
      <span>{formatRelativeTime(at)}</span>{" "}
      <span className="node-records-exact">{moment.toLocaleString()}</span>
    </time>
  );
}
