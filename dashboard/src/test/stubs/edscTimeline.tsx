/* Stands in for the vendored @edsc/timeline under vitest (see vite.config.js).
   The real one animates and measures its own width, neither of which jsdom
   does, so tests assert on what the page hands it and drive its callbacks. */
import type { TimelineProps } from "@edsc/timeline";

export const timelineProps: { last: TimelineProps | null } = { last: null };

export default function EDSCTimelineStub(props: TimelineProps) {
  timelineProps.last = props;
  return (
    <ul data-testid="edsc-timeline">
      {props.data.map((row) => (
        <li key={row.id} data-colour={row.color}>
          {row.title}: {row.intervals.length}
        </li>
      ))}
    </ul>
  );
}
