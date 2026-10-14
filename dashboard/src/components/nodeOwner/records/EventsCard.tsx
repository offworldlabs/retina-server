import { api } from "../../../api/client";
import type { OwnerEvent } from "../../../types";
import { DASH } from "../../../utils/format";
import { RecordCard, useRecord } from "./RecordCard";
import { When } from "./When";
import { humanize } from "./values";

/** How many entries the card asks for, which is the route's default. */
export const EVENTS_LIMIT = 100;

/** What happened, in the owner's words. A Map rather than an object, so a kind
 *  the console does not know yet, whatever it is called, falls through to its
 *  own name and never to something Object carries. */
const KIND_LABEL = new Map([
  ["registered", "Registered"],
  ["removed", "Removed"],
  ["graduated", "Graduated from probation"],
  ["returned_to_probation", "Returned to probation"],
  ["renamed", "Label changed"],
  ["made_private", "Location made private"],
  ["made_public", "Location made public"],
]);

/** The dot's tone: what took a node off the network, back towards it, or onto
 *  it. A change of label or privacy is the owner's own housekeeping. */
const KIND_TONE = new Map([
  ["registered", "accent"],
  ["graduated", "success"],
  ["returned_to_probation", "warning"],
  ["removed", "error"],
]);

/** Whose side a decision came from. The route never says who, since the stored
 *  actor can be an administrator's email: an owner from before the node
 *  changed hands is "an earlier owner", and the server's own jobs act
 *  automatically. */
const ACTOR_LABEL = new Map([
  ["you", "by you"],
  ["owner", "by an earlier owner"],
  ["admin", "by an administrator"],
  ["system", "automatically"],
]);

function eventLabel(kind: string): string {
  return KIND_LABEL.get(kind) ?? (humanize(String(kind ?? "")) || DASH);
}

function Events({ events }: { events: OwnerEvent[] }) {
  if (!events.length) return <p className="node-records-note">Nothing recorded since this node joined your account.</p>;
  // In the order the route sends them, which is newest first by when each was
  // recorded. Their stamps are to the second, so sorting on them here could
  // only put two decisions made in one second the wrong way round.
  return (
    <ol className="node-events">
      {events.map((event, i) => {
        const actor = ACTOR_LABEL.get(event.actor);
        return (
          <li key={i} className="node-event">
            <span className="node-event-dot" data-tone={KIND_TONE.get(event.kind) ?? "neutral"} aria-hidden="true" />
            <p className="node-event-what">
              <strong>{eventLabel(event.kind)}</strong>
              {actor && ` ${actor}`}
            </p>
            <When at={event.at} />
          </li>
        );
      })}
    </ol>
  );
}

/** The decisions recorded about the node since it joined the caller's
 *  account: its trust, its label and its privacy, and who made each. */
export function EventsCard({ nodeId, refreshKey, onGone }: { nodeId: string; refreshKey: number; onGone: () => void }) {
  const polled = useRecord(() => api.myNodeEvents(nodeId, EVENTS_LIMIT), nodeId, refreshKey);
  const eventsOf = (data: { events: OwnerEvent[] } | null) => (Array.isArray(data?.events) ? data.events : []);
  return (
    <RecordCard
      title="History"
      what="this node's history"
      polled={polled}
      onGone={onGone}
      aside={(data) => {
        const count = eventsOf(data).length;
        if (!count) return null;
        return (
          <span className="card-note">
            {count >= EVENTS_LIMIT ? `Newest ${EVENTS_LIMIT} entries` : `${count} ${count === 1 ? "entry" : "entries"}`}
          </span>
        );
      }}
    >
      {(data) => <Events events={eventsOf(data)} />}
    </RecordCard>
  );
}
