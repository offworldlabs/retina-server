import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { HttpError } from "@retina/shared";

import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { FetchNotice, nothingLoaded } from "../../components/Notice";
import { Pager, clampPage } from "../../components/Pager";
import { StatCard } from "../../components/StatCard";
import { useFetch } from "../../hooks/usePolling";
import type { AdminClaim, AdminUser } from "../../types";
import { DASH, formatRelativeTime } from "../../utils/format";
import { severityTone } from "../../utils/severity";
import { challengeExpired, claimBadge, describeChallenge } from "./claims/claimState";
import { UserActions, type UserChange } from "./users/UserActions";
import "./users/userDetail.css";
import "./users/users.css";

const EVENTS_LIMIT = 200;
const EVENTS_PAGE_SIZE = 25;

/** Where the list was left, as the list wrote it into the link; the bare list
 *  for an address opened directly. Only a path on this site is followed. */
function backTarget(state: unknown): string {
  const from = (state as { from?: unknown } | null)?.from;
  return typeof from === "string" && /^\/(?!\/)/.test(from) ? from : "/users";
}

function count(n: number, noun: string): string {
  return `${n.toLocaleString()} ${noun}${n === 1 ? "" : "s"}`;
}

/** A moment as time ago, with the full date on hover. */
function When({ at, none = DASH }: { at: number | null; none?: string }) {
  if (!at) return <>{none}</>;
  const when = new Date(at * 1000);
  return (
    <time dateTime={when.toISOString()} title={when.toLocaleString()}>
      {formatRelativeTime(at)}
    </time>
  );
}

function StateBadges({ user }: { user: AdminUser }) {
  return (
    <div className="user-detail-badges">
      <span className={user.is_active ? "badge online" : "badge offline"}>{user.is_active ? "Active" : "Deactivated"}</span>
      <span className={user.is_verified ? "badge online" : "badge warning"}>
        {user.is_verified ? "Verified" : "Unverified"}
      </span>
    </div>
  );
}

function NodeCell({ claim }: { claim: AdminClaim }) {
  return claim.node_ref ? (
    <Link to={`/nodes/${encodeURIComponent(claim.node_ref)}`} className="mono detail-link">
      {claim.node_ref}
    </Link>
  ) : (
    <span className="mono">{claim.node_id}</span>
  );
}

function ClaimBadge({ claim }: { claim: AdminClaim }) {
  const { label, tone } = claimBadge(claim);
  return <span className={`badge ${tone}`}>{label}</span>;
}

function NodesCard({ nodes }: { nodes: AdminClaim[] }) {
  return (
    <div className="card detail-card">
      <div className="card-header">
        <h3>Nodes</h3>
        <span className="card-note">{count(nodes.length, "node")}</span>
      </div>
      <DataTable headers={["Node", "Claim", "Address", "Verified", "Owned since"]} count={nodes.length} empty="No nodes">
        {nodes.map((claim) => (
          <tr key={claim.node_id}>
            <td className="cell-primary" data-label="Node">
              <NodeCell claim={claim} />
            </td>
            <td data-label="Claim">
              <ClaimBadge claim={claim} />
            </td>
            <td className="detail-wrap" data-label="Address">
              {claim.email ?? DASH}
            </td>
            <td data-label="Verified">
              {claim.email === null ? (
                DASH
              ) : (
                <span className={claim.verified ? "badge online" : "badge warning"}>
                  {claim.verified ? "Verified" : "Unverified"}
                </span>
              )}
            </td>
            <td data-label="Owned since">
              <When at={claim.owned_since} />
            </td>
          </tr>
        ))}
      </DataTable>
    </div>
  );
}

function ClaimsCard({ claims }: { claims: AdminClaim[] }) {
  return (
    <div className="card detail-card">
      <div className="card-header">
        <h3>Claims to this address</h3>
        <span className="card-note">Other nodes that carry this address</span>
      </div>
      <DataTable
        headers={["Node", "Claim", "Address", "Link"]}
        count={claims.length}
        empty="No other claims to this address"
      >
        {claims.map((claim) => (
          <tr key={claim.node_id}>
            <td className="cell-primary" data-label="Node">
              <NodeCell claim={claim} />
            </td>
            <td data-label="Claim">
              <ClaimBadge claim={claim} />
            </td>
            <td className="detail-wrap" data-label="Address">
              <div>
                {claim.email ?? claim.challenge?.email ?? DASH}
                {claim.undeliverable && <span className="detail-warning">Mail to this address bounced</span>}
              </div>
            </td>
            <td className="detail-wrap" data-label="Link">
              {claim.challenge ? (
                <span className={challengeExpired(claim.challenge) ? "detail-warning-inline" : undefined}>
                  {describeChallenge(claim.challenge)}
                </span>
              ) : (
                DASH
              )}
            </td>
          </tr>
        ))}
      </DataTable>
    </div>
  );
}

function EventsCard({ userId, reload }: { userId: string; reload: number }) {
  const [page, setPage] = useState(0);
  const polled = useFetch(
    () => api.adminEvents(EVENTS_LIMIT, userId).then((events) => (Array.isArray(events) ? events : [])),
    `${userId}\u0000${reload}`,
  );
  const { data, loading } = polled;
  const events = data ?? [];
  const totalPages = Math.ceil(events.length / EVENTS_PAGE_SIZE);
  const shown = clampPage(page, totalPages);
  const rows = events.slice(shown * EVENTS_PAGE_SIZE, (shown + 1) * EVENTS_PAGE_SIZE);

  return (
    <div className="card detail-card">
      <div className="card-header">
        <h3>Events</h3>
        {events.length > 0 && (
          <span className="card-note">
            {events.length >= EVENTS_LIMIT ? `Latest ${count(EVENTS_LIMIT, "event")}` : count(events.length, "event")}
          </span>
        )}
      </div>
      <FetchNotice polled={polled} what="their events" />
      {!nothingLoaded(polled) && (
        <>
          <DataTable
            headers={["Time", "Severity", "Message"]}
            count={rows.length}
            loading={loading}
            empty="No events for this account"
          >
            {rows.map((event, i) => (
              <tr key={shown * EVENTS_PAGE_SIZE + i}>
                <td className="mono detail-time" data-label="Time">
                  {event.ts ? new Date(event.ts * 1000).toLocaleString() : DASH}
                </td>
                <td data-label="Severity">
                  <span className={`badge ${severityTone(event.severity)}`}>{event.severity}</span>
                </td>
                <td className="detail-wrap detail-message" data-label="Message">
                  {event.message}
                </td>
              </tr>
            ))}
          </DataTable>
          <Pager page={shown} totalPages={totalPages} onPage={setPage} note={count(events.length, "event")} />
        </>
      )}
    </div>
  );
}

function UserDetail({ userId }: { userId: string }) {
  const { state } = useLocation();
  const back = backTarget(state);
  const polled = useFetch(() => api.adminUser(userId), userId);
  const { data, loading, error, refresh } = polled;
  const [reload, setReload] = useState(0);
  const [ended, setEnded] = useState<string | null>(null);

  const noticeRef = useRef<HTMLParagraphElement>(null);
  useEffect(() => {
    if (ended) noticeRef.current?.focus();
  }, [ended]);

  const backLink = (
    <Link to={back} className="detail-back">
      ← Back to users
    </Link>
  );

  const changed = (user: AdminUser, change: UserChange) => {
    if (change.kind === "deleted") {
      const released = change.released > 0 ? ` ${count(change.released, "node")} released.` : "";
      setEnded(`Deleted ${user.email}.${released}`);
      return;
    }
    if (change.kind === "gone") {
      setEnded(`The account for ${user.email} no longer exists.`);
      return;
    }
    refresh();
    setReload((n) => n + 1);
  };

  if (ended) {
    return (
      <>
        <div className="page-header">{backLink}</div>
        <p ref={noticeRef} className="users-notice" role="status" tabIndex={-1}>
          {ended}
        </p>
      </>
    );
  }

  const missing = error instanceof HttpError && error.status === 404;
  if (missing) {
    return (
      <>
        <div className="page-header">
          {backLink}
          <h1>No such account</h1>
          <p>There is no account with this id. It may have been deleted.</p>
        </div>
      </>
    );
  }

  if (!data) {
    return (
      <>
        <div className="page-header">{backLink}</div>
        {nothingLoaded(polled) ? <FetchNotice polled={polled} what="this account" /> : loading && <div className="empty-state">Loading…</div>}
      </>
    );
  }

  const { user, nodes, claims } = data;
  return (
    <>
      <div className="page-header user-detail-head">
        {backLink}
        <div className="user-detail-who">
          <div>
            <h1>{user.name || user.email}</h1>
            {user.name && <p className="user-detail-email">{user.email}</p>}
            <StateBadges user={user} />
          </div>
          <UserActions user={user} onChanged={(change) => changed(user, change)} />
        </div>
      </div>
      <FetchNotice polled={polled} what="this account" />

      <div className="stats-grid">
        <StatCard
          size="small"
          label="Account created"
          value={<When at={user.created_at} />}
          sub={user.created_at ? new Date(user.created_at * 1000).toLocaleDateString() : undefined}
        />
        <StatCard size="small" label="Last visit" value={<When at={user.last_seen_at} none="Never" />} />
        <StatCard label="Nodes" value={user.node_count.toLocaleString()} tone="accent" />
      </div>

      <NodesCard nodes={nodes ?? []} />
      <ClaimsCard claims={claims ?? []} />
      <EventsCard userId={user.id} reload={reload} />
    </>
  );
}

export default function UserDetailPage() {
  const { userId = "" } = useParams();
  // Keyed on the account, so a move to another one starts from nothing
  // instead of showing the last one's rows until the next answer lands.
  return <UserDetail key={userId} userId={userId} />;
}
