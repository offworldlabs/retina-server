import { useId, useMemo, useState, type ReactNode } from "react";
import { api } from "../../api/client";
import { ClaimPathNote } from "../../components/ClaimPath";
import { FetchNotice, Notice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { Pager, clampPage } from "../../components/Pager";
import { ScopeSwitch } from "../../components/ScopeSwitch";
import { StatCard } from "../../components/StatCard";
import { useOwnedNodes, type OwnedNodes } from "../../hooks/useOwnedNodes";
import { useFetch, usePolling } from "../../hooks/usePolling";
import { useScopeParam, type Scope } from "../../hooks/useScopeParam";
import type { AircraftFrame, FeedAircraft } from "../../types";
import { SeenBy } from "./detections/SeenBy";
import {
  filterOptions,
  listedNames,
  nodeNamer,
  nodesBehind,
  seenByNode,
  type FilterOption,
  type NameOf,
} from "./detections/nodes";

import "./detections/detections.css";

const PAGE_SIZE = 25;
const REFRESH_MS = 3000;

/** The aircraft the owner's nodes see, private ones included, from
 *  /api/auth/me/aircraft; or every aircraft the public feed carries. Both
 *  name a node by its node_ref only. */
function fetchFeed(scope: Scope): Promise<FeedAircraft[]> {
  const frame: Promise<AircraftFrame | null> = scope === "mine" ? api.myAircraft() : api.aircraft();
  return frame.then((f) => (Array.isArray(f?.aircraft) ? f.aircraft : []));
}

/** Which feed is on screen, in the owner's words. The public feed leaves a
 *  private node out, so what only such a node sees is not on it. */
function feedDescription(scope: Scope, hasPrivate: boolean): string {
  const what =
    scope === "mine"
      ? hasPrivate
        ? "Aircraft your nodes see, private ones included"
        : "Aircraft your nodes see"
      : hasPrivate
        ? "Every aircraft on the network, except what only your private nodes see"
        : "Every aircraft on the network";
  return `${what} · updates every ${REFRESH_MS / 1000} s`;
}

export default function DetectionsPage() {
  const owned = useOwnedNodes();
  const hasNodes = owned.status === "ready" && owned.nodes.length > 0;
  const [scope, setScope] = useScopeParam(hasNodes);
  // Names for the nodes that are not the owner's. A failure costs only the
  // names: such a node goes by its ref.
  const { data: listing } = useFetch(() => api.nodes());
  const listed = useMemo(() => listedNames(listing), [listing]);
  const nameOf = useMemo(() => nodeNamer(owned.nodes, listed), [owned.nodes, listed]);

  // Which feed is the owner's to see is not known until their list is in, so
  // neither is asked for before then: the network's aircraft never show under
  // the owner's heading, nor theirs under the network's.
  const feed = owned.status === "loading" ? null : scope;
  const hasPrivate = owned.nodes.some((n) => n.location_private);

  let banner: ReactNode = null;
  if (owned.status === "error") {
    banner = (
      <Notice onRetry={owned.refresh}>
        Could not load your nodes: {owned.error?.message}. Showing the whole network.
      </Notice>
    );
  } else if (owned.status === "ready" && owned.nodes.length === 0) {
    banner = <ClaimPathNote lead="No nodes on your account yet, so this is the whole network." />;
  }

  return (
    <>
      <div className="page-header detections-header">
        <div className="detections-heading">
          <h1>Live Detections</h1>
          {feed && <p>{feedDescription(feed, hasPrivate)}</p>}
        </div>
        {hasNodes && (
          <ScopeSwitch
            value={scope}
            onChange={setScope}
            mineCount={owned.nodes.length}
            label="Show detections from"
          />
        )}
      </div>
      {feed === null ? (
        <div className="empty-state">Loading…</div>
      ) : (
        // Keyed on the feed, so a switch starts the other feed afresh: its own
        // loading line and failures, and the filter and page back at the start.
        <DetectionsFeed key={feed} scope={feed} owned={owned} listed={listed} nameOf={nameOf} banner={banner} />
      )}
    </>
  );
}

interface FeedProps {
  scope: Scope;
  /** The signed-in owner's nodes, listed first in the filter and marked in
   *  the table. Empty when there is no owner view. */
  owned: OwnedNodes;
  /** The public listing's names, whose refs the network filter offers. */
  listed: ReadonlyMap<string, string | null>;
  nameOf: NameOf;
  /** What the page says above the feed about the owner's view. Shown with
   *  the feed's first answer, so the page appears whole. */
  banner: ReactNode;
}

function DetectionsFeed({ scope, owned, listed, nameOf, banner }: FeedProps) {
  const [filterNode, setFilterNode] = useState("");
  const [page, setPage] = useState(0);
  const filterId = useId();
  const polled = usePolling(() => fetchFeed(scope), REFRESH_MS);
  const { data, loading } = polled;
  const mine = owned.refs;

  // A node released from the account since it was chosen is on neither the
  // owner feed nor the list of choices any more.
  if (scope === "mine" && filterNode !== "" && !mine.has(filterNode)) setFilterNode("");

  if (loading) return <div className="empty-state">Loading…</div>;

  const notices = (
    <>
      {banner}
      <FetchNotice polled={polled} what="detections" />
    </>
  );
  if (nothingLoaded(polled)) return notices;

  const aircraft = data ?? [];
  const filtered = filterNode ? aircraft.filter((a) => seenByNode(a, filterNode)) : aircraft;

  const yours = filterOptions(mine, nameOf);
  // The rest of the network: every listed node, and any node behind an
  // aircraft that the listing has not caught up with. The chosen node stays
  // a choice while nothing names it, rather than the field showing another.
  let others: FilterOption[] = [];
  if (scope === "network") {
    const refs = new Set([...listed.keys(), ...aircraft.flatMap(nodesBehind)]);
    if (filterNode) refs.add(filterNode);
    for (const ref of mine) refs.delete(ref);
    others = filterOptions(refs, nameOf);
  }

  const chosen = filterNode ? nameOf(filterNode).text : null;
  const chosenPrivate = owned.nodes.some((n) => n.node_ref === filterNode && n.location_private);
  let empty: string;
  if (!chosen) empty = scope === "mine" ? "None of your nodes sees an aircraft right now" : "No detections at this time";
  else if (scope === "network" && chosenPrivate)
    empty = `${chosen} is private, so it is not on the network feed. My nodes shows what it sees.`;
  else empty = `No detections from ${chosen} right now`;

  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  const current = clampPage(page, totalPages);
  const paged = filtered.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);

  return (
    <>
      {notices}

      <div className="stats-grid">
        <StatCard label="Aircraft Tracked" value={filtered.length} tone="accent" />
        <StatCard
          label="With ADS-B Match"
          value={filtered.filter((a) => a.flight || a.hex).length}
          tone="success"
        />
        <StatCard label={scope === "mine" ? "All My Nodes" : "Total Network"} value={aircraft.length} />
      </div>

      <div className="card detections-card">
        <div className="card-header">
          <h3>Detection Feed</h3>
          <div className="card-aside">
            <div className="detections-filter">
              <label htmlFor={filterId}>Node</label>
              <select
                className="input input-sm"
                id={filterId}
                value={filterNode}
                onChange={(e) => { setFilterNode(e.target.value); setPage(0); }}
              >
                <option value="">{scope === "mine" ? "All my nodes" : "All nodes"}</option>
                {/* An option holds text only, so a group names the owner's
                    nodes. On the network each says so itself as well: the
                    closed field shows the choice without its group. */}
                {yours.length > 0 && (
                  <optgroup label="Your nodes">
                    {yours.map((o) => (
                      <option key={o.ref} value={o.ref}>
                        {scope === "network" ? `${o.text} · yours` : o.text}
                      </option>
                    ))}
                  </optgroup>
                )}
                {others.length > 0 && (
                  <optgroup label="Network">
                    {others.map((o) => (
                      <option key={o.ref} value={o.ref}>{o.text}</option>
                    ))}
                  </optgroup>
                )}
              </select>
            </div>
          </div>
        </div>
        <DataTable
          headers={["Hex", "Flight", "Seen by", "Lat", "Lon", "Alt (ft)", "Speed (kt)", "Track", "Seen (s)"]}
          count={paged.length}
          empty={empty}
        >
          {paged.map((ac, i) => (
            <tr key={ac.hex || current * PAGE_SIZE + i}>
              <td className="mono detections-hex">{ac.hex || "—"}</td>
              <td className="detections-flight">{ac.flight?.trim() || "—"}</td>
              <td>
                <SeenBy refs={nodesBehind(ac)} nameOf={nameOf} mine={mine} />
              </td>
              <td>{ac.lat?.toFixed(4) ?? "—"}</td>
              <td>{ac.lon?.toFixed(4) ?? "—"}</td>
              <td>{ac.alt_baro ?? "—"}</td>
              <td>{ac.gs?.toFixed(0) ?? "—"}</td>
              <td>{ac.track != null ? `${ac.track.toFixed(0)}°` : "—"}</td>
              <td>{ac.seen?.toFixed(0) ?? "—"}</td>
            </tr>
          ))}
        </DataTable>
        <Pager page={current} totalPages={totalPages} onPage={setPage} note={`${filtered.length} aircraft`} />
      </div>
    </>
  );
}
