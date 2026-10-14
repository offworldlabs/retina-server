import { ConfigHistoryCard } from "./records/ConfigHistoryCard";
import { EventsCard } from "./records/EventsCard";
import { ReportCard } from "./records/ReportCard";
import "./nodeOwnerRecords.css";

/** What an owner alone sees of their node beyond its settings: the last
 *  heartbeat report, every configuration it has run under, and what has been
 *  decided about it. Each card fetches its own route, so one that fails or is
 *  slow costs only itself.
 *
 *  `refreshKey` is for the page to bump after the owner changes the node: all
 *  three fetch again, and the history picks up the entry the change recorded.
 *  A card whose route answers 404 calls `onGone`: the node is no longer on the
 *  caller's account, and the page says so in place of every card.
 *
 *  A different node is a different set of cards, so nothing from the last one
 *  stays up while the new one loads. */
export function NodeOwnerRecords({
  nodeId,
  refreshKey = 0,
  onGone,
}: {
  nodeId: string;
  refreshKey?: number;
  onGone: () => void;
}) {
  return (
    <div className="node-records">
      <ReportCard key={`report:${nodeId}`} nodeId={nodeId} refreshKey={refreshKey} onGone={onGone} />
      <ConfigHistoryCard key={`config:${nodeId}`} nodeId={nodeId} refreshKey={refreshKey} onGone={onGone} />
      <EventsCard key={`events:${nodeId}`} nodeId={nodeId} refreshKey={refreshKey} onGone={onGone} />
    </div>
  );
}
