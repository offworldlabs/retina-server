import { DASH, formatRelativeTime } from "../utils/format";

interface Props {
  /** Epoch seconds, as the admin payloads send a moment. */
  at: number | null;
  /** What a missing moment reads as. */
  none?: string;
}

/** A moment as how long ago it was, with the full local date and time on
 *  hover. */
export function RelativeTime({ at, none = DASH }: Props) {
  if (!at) return <>{none}</>;
  const when = new Date(at * 1000);
  return (
    <time dateTime={when.toISOString()} title={when.toLocaleString()}>
      {formatRelativeTime(at)}
    </time>
  );
}
