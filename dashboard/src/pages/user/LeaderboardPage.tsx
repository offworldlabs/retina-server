import { Fragment, useState } from "react";
import { api } from "../../api/client";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { DataTable } from "../../components/DataTable";
import { Pager, clampPage } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { StatusBadge } from "../../components/StatusBadge";
import { usePolling } from "../../hooks/usePolling";
import { useAuth } from "../../context/AuthContext";
import type { OwnedNode } from "../../types";
import { formatAvailability } from "../../utils/format";
import { shortRef } from "../../utils/nodes";
import { rankWindows } from "../../utils/rankWindows";

const PAGE_SIZE = 25;

/** How many nodes either side of each of theirs an owner is shown. */
const NEIGHBOURS = 2;

/** How often the owner's node list is asked for again. It changes on a claim,
 *  a release or a privacy choice, all made on other pages, and returning here
 *  from one of those asks afresh. */
const OWNED_EVERY_MS = 5 * 60_000;

/** One row as /api/admin/leaderboard sends it; the miss fields only to a
 *  caller with a session. */
interface Entry {
  node_ref: string;
  name?: string | null;
  detections: number;
  tracks: number;
  availability_7d: number | null;
  avg_snr: number;
  trust_score: number;
  online: boolean;
  in_range?: number;
  missed?: number;
  miss_rate?: number;
}

/** Each ranking the page offers: the button that picks it, how the owner's
 *  standing names it, and the order it puts the rows in. */
const SORTS: Record<string, { label: string; noun: string; compare: (a: Entry, b: Entry) => number }> = {
  detections: { label: "Detections", noun: "detections", compare: (a, b) => b.detections - a.detections },
  // A node not yet measured goes last.
  availability: {
    label: "Availability",
    noun: "availability",
    compare: (a, b) => (b.availability_7d ?? -1) - (a.availability_7d ?? -1),
  },
  trust: { label: "Trust", noun: "trust", compare: (a, b) => b.trust_score - a.trust_score },
  snr: { label: "SNR", noun: "SNR", compare: (a, b) => b.avg_snr - a.avg_snr },
  // Lowest first. Sent only to a caller with a session, so offered only to one.
  miss_rate: { label: "Miss Rate", noun: "miss rate", compare: (a, b) => (a.miss_rate || 0) - (b.miss_rate || 0) },
};

/** A row in the order the page is sorted by, and whether the caller owns it. */
interface Ranked {
  entry: Entry;
  rank: number;
  mine: boolean;
}

/** What a node calls itself. The routes fall back to the whole ref when a node
 *  carries no name of its own, and a ref reads as its short form everywhere
 *  else in the console. */
function rowName(node: { name?: string | null; node_ref: string | null }): string {
  return node.name && node.name !== node.node_ref ? node.name : shortRef(node.node_ref) || "Unnamed node";
}

/** Why an owner's node has no place here. The leaderboard ranks only what
 *  /api/radar/analytics publishes, which never includes a node registered
 *  private. Probation is given as a fact rather than the cause: whether it
 *  holds a radar back depends on a server switch the owner's list does not
 *  carry. */
function whyUnranked(node: OwnedNode): string {
  if (node.location_private) return "Its location is private, so it is not published";
  if (node.polled?.trust_state === "probation") return "Not ranked yet; the radar is still on probation";
  return "Not ranked yet";
}

function YoursTag() {
  return <span className="badge plain yours">Yours</span>;
}

function RankingRow({ ranked, showsMisses }: { ranked: Ranked; showsMisses: boolean }) {
  const { entry, rank, mine } = ranked;
  return (
    <tr className={mine ? "mine" : undefined}>
      <td style={{ fontWeight: 600, color: "var(--text-primary)" }}>{rank}</td>
      <td>
        <span className="rank-name">
          <span className="mono">{rowName(entry)}</span>
          {mine && <YoursTag />}
        </span>
      </td>
      <td>
        <StatusBadge online={entry.online} />
      </td>
      <td>{entry.detections.toLocaleString()}</td>
      <td>{entry.tracks}</td>
      {showsMisses && (
        <>
          <td>{entry.in_range || 0}</td>
          <td style={{ color: (entry.missed || 0) > 0 ? "var(--warning)" : undefined }}>
            {entry.missed || 0}
          </td>
          <td style={{
            fontWeight: 600,
            color: (entry.miss_rate || 0) > 0.5 ? "var(--error)"
              : (entry.miss_rate || 0) > 0.2 ? "var(--warning)" : "var(--success)",
          }}>
            {(entry.in_range || 0) > 0 ? ((entry.miss_rate || 0) * 100).toFixed(1) + "%" : "—"}
          </td>
        </>
      )}
      <td title="Share of the last 7 days' minutes it delivered in">
        {formatAvailability(entry.availability_7d)}
      </td>
      <td>{entry.avg_snr.toFixed(1)} dB</td>
      <td>{(entry.trust_score * 100).toFixed(0)}%</td>
    </tr>
  );
}

export default function LeaderboardPage() {
  const [sortBy, setSortBy] = useState("detections");
  const [page, setPage] = useState(0);
  const [search, setSearch] = useState("");
  // The server sends the miss-detection fields only to a caller with a
  // session, so the columns over them exist only for one. Rendering them
  // regardless would report every node as having missed nothing.
  const { user } = useAuth();
  const showsMisses = Boolean(user);
  const polled = usePolling(() => api.leaderboard(), 30000);
  // Polled apart from the rankings, so a failed ask keeps the last list rather
  // than taking the owner's standing off the page. Keyed on the account, so a
  // change of session asks at once. A caller with no session owns nothing,
  // and asking would only be a 401.
  const account = user ? `user:${user.id}` : "";
  const owned = usePolling(
    async () => ({ account, nodes: user ? ((await api.myNodes()) as OwnedNode[]) : [] }),
    OWNED_EVERY_MS,
    account,
  );
  const { data, loading } = polled;

  if (loading) return <div className="empty-state">Loading…</div>;

  const entries: Entry[] = data?.leaderboard || [];
  // usePolling keeps the last answer across a change of key until the next
  // lands, and for good if that fails, so a list counts only while it is this
  // account's.
  const myNodes = owned.data?.account === account && Array.isArray(owned.data.nodes) ? owned.data.nodes : [];

  const sorted = [...entries].sort(SORTS[sortBy].compare);
  // Both lists are keyed on node_ref: the owner's also carries node_id, which
  // no public row does.
  const myRefs = new Set(myNodes.map((n) => n.node_ref).filter(Boolean));
  const ranked: Ranked[] = sorted.map((entry, i) => ({ entry, rank: i + 1, mine: myRefs.has(entry.node_ref) }));
  const standing = rankWindows(
    ranked.length,
    ranked.flatMap((r, i) => (r.mine ? [i] : [])),
    NEIGHBOURS,
  );
  const onBoard = new Set(entries.map((e) => e.node_ref));
  const unranked = myNodes.filter((n) => !n.node_ref || !onBoard.has(n.node_ref));

  const top3 = ranked.slice(0, 3);
  const headers = [
    "#",
    "Node",
    "Status",
    "Detections",
    "Tracks",
    ...(showsMisses ? ["In Range", "Missed", "Miss Rate"] : []),
    "Availability",
    "Avg SNR",
    "Trust",
  ];

  const header = (
    <div className="page-header">
      <h1>Leaderboard & Community</h1>
      <p>Network-wide rankings and community links</p>
    </div>
  );
  if (nothingLoaded(polled)) {
    return (
      <>
        {header}
        <FetchNotice polled={polled} what="the leaderboard" />
      </>
    );
  }

  return (
    <>
      {header}
      <FetchNotice polled={polled} what="the leaderboard" />

      <div className="stats-grid">
        <StatCard label="Total Nodes" value={entries.length} tone="accent" />
        <StatCard label="Online Now" value={entries.filter((e) => e.online).length} tone="success" />
        <StatCard
          label="Total Detections"
          value={entries.reduce((s, e) => s + e.detections, 0).toLocaleString()}
          tone="warning"
        />
      </div>

      {/* Podium for top 3 */}
      {top3.length > 0 && (
        <div className="card">
          <div className="card-header"><h3>Top Performers</h3></div>
          <div className="card-body" style={{ display: "flex", gap: 16, justifyContent: "center", flexWrap: "wrap" }}>
            {top3.map(({ entry, mine }, i) => (
              <div key={entry.node_ref} style={{
                textAlign: "center",
                padding: "20px 24px",
                borderRadius: "var(--radius)",
                border: "1px solid var(--border)",
                background: i === 0 ? "var(--warning-light)" : "var(--bg-card-hover)",
                minWidth: 180,
              }}>
                <div style={{ fontSize: 28, fontWeight: 700, marginBottom: 4 }}>
                  {i === 0 ? "🥇" : i === 1 ? "🥈" : "🥉"}
                </div>
                <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
                  #{i + 1}
                </div>
                <div className="mono" style={{ color: "var(--accent)", marginBottom: 4 }}>
                  {rowName(entry)}
                </div>
                <div style={{ fontSize: 20, fontWeight: 700 }}>{entry.detections.toLocaleString()}</div>
                <div className="card-note">detections</div>
                {mine && <YoursTag />}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Sort control */}
      <div className="toolbar">
        <span className="card-note">Sort by:</span>
        {Object.keys(SORTS).filter((key) => showsMisses || key !== "miss_rate").map((key) => (
          <button
            key={key}
            className={`btn ${sortBy === key ? "btn-primary" : "btn-secondary"} btn-sm`}
            onClick={() => { setSortBy(key); setPage(0); }}
          >
            {SORTS[key].label}
          </button>
        ))}
        <input
          type="text"
          placeholder="Search…"
          value={search}
          onChange={(e) => { setSearch(e.target.value); setPage(0); }}
          className="input input-sm"
          style={{ marginLeft: "auto" }}
        />
      </div>

      {myNodes.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Your nodes</h3>
            <span className="card-note">
              Ranked by {SORTS[sortBy].noun} among {ranked.length} nodes, with {NEIGHBOURS} either side
            </span>
          </div>
          <DataTable headers={headers} count={standing.flat().length + unranked.length}>
            {standing.map((run, k) => (
              <Fragment key={run[0]}>
                {k > 0 && (
                  <tr className="rank-gap">
                    <td colSpan={headers.length}>{run[0] - standing[k - 1][standing[k - 1].length - 1] - 1} more</td>
                  </tr>
                )}
                {run.map((i) => (
                  <RankingRow key={ranked[i].entry.node_ref} ranked={ranked[i]} showsMisses={showsMisses} />
                ))}
              </Fragment>
            ))}
            {unranked.map((node) => (
              <tr key={node.node_id} className="mine">
                <td>—</td>
                <td>
                  <span className="rank-name">
                    <span className="mono">{rowName(node)}</span>
                    <YoursTag />
                  </span>
                </td>
                <td colSpan={headers.length - 2}>{whyUnranked(node)}</td>
              </tr>
            ))}
          </DataTable>
        </div>
      )}

      <div className="card">
        <div className="card-header"><h3>Rankings</h3></div>
        {(() => {
          const filtered = search
            ? ranked.filter(({ entry: e }) => ((e.name || e.node_ref || "")).toLowerCase().includes(search.toLowerCase()))
            : ranked;
          const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
          const current = clampPage(page, totalPages);
          const paged = filtered.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);
          return (
            <>
              <DataTable headers={headers} count={paged.length} empty="No nodes found">
                {paged.map((r) => (
                  <RankingRow key={r.entry.node_ref} ranked={r} showsMisses={showsMisses} />
                ))}
              </DataTable>
              <Pager page={current} totalPages={totalPages} onPage={setPage} note={`${filtered.length} nodes`} />
            </>
          );
        })()}
      </div>

      {/* Community Links */}
      <div className="card">
        <div className="card-header"><h3>Community</h3></div>
        <div className="card-body">
          <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
            <a
              href="https://discord.gg/retina"
              target="_blank"
              rel="noopener noreferrer"
              className="btn btn-primary"
            >
              <svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor">
                <path d="M20.317 4.37a19.791 19.791 0 00-4.885-1.515.074.074 0 00-.079.037c-.21.375-.444.864-.608 1.25a18.27 18.27 0 00-5.487 0 12.64 12.64 0 00-.617-1.25.077.077 0 00-.079-.037A19.736 19.736 0 003.677 4.37a.07.07 0 00-.032.027C.533 9.046-.32 13.58.099 18.057a.082.082 0 00.031.057 19.9 19.9 0 005.993 3.03.078.078 0 00.084-.028c.462-.63.874-1.295 1.226-1.994a.076.076 0 00-.041-.106 13.107 13.107 0 01-1.872-.892.077.077 0 01-.008-.128 10.2 10.2 0 00.372-.292.074.074 0 01.077-.01c3.928 1.793 8.18 1.793 12.062 0a.074.074 0 01.078.01c.12.098.246.198.373.292a.077.077 0 01-.006.127 12.299 12.299 0 01-1.873.892.077.077 0 00-.041.107c.36.698.772 1.362 1.225 1.993a.076.076 0 00.084.028 19.839 19.839 0 006.002-3.03.077.077 0 00.032-.054c.5-5.177-.838-9.674-3.549-13.66a.061.061 0 00-.031-.03z"/>
              </svg>
              Join Discord
            </a>
            <a
              href="https://retina.fm"
              target="_blank"
              rel="noopener noreferrer"
              className="btn btn-secondary"
            >
              Website
            </a>
          </div>
        </div>
      </div>
    </>
  );
}
