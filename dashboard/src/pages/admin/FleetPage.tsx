import { useState, type ReactNode } from "react";

import { api } from "../../api/client";
import { DataTable } from "../../components/DataTable";
import { FetchNotice, Notice, nothingLoaded } from "../../components/Notice";
import { Pager, clampPage } from "../../components/Pager";
import { RelativeTime } from "../../components/RelativeTime";
import { StatCard } from "../../components/StatCard";
import { usePolling } from "../../hooks/usePolling";
import type {
  AdminFleet,
  AdminFleetEmailState,
  AdminFleetMirror,
  AdminFleetPresence,
  AdminFleetRow,
} from "../../types";
import { DASH, formatDuration } from "../../utils/format";
import "./fleet/fleet.css";

const PAGE_SIZE = 25;
const POLL_MS = 30_000;

type PresenceFilter = AdminFleetPresence | "all";

/** The table's columns, in order. fleet.css sizes them by position, so a
 *  change here is a change there too. */
const COLUMNS = ["Node", "Registry", "Last seen", "Email", "Mender", "Device", "Inventory", "Presence"];

/** A registry status (NODE_STATUSES in backend/core/nodes.py) as a badge
 *  tone. A blocked node wants a look; a retired one is out of service on
 *  purpose, not in trouble, so it takes the neutral tone rather than red. */
const REGISTRY_TONE: Record<string, string> = {
  active: "online",
  blocked: "warning",
  retired: "private",
};

const PRESENCE: Record<AdminFleetPresence, { label: string; tone: string }> = {
  both: { label: "Both", tone: "online" },
  registry_only: { label: "Registry only", tone: "warning" },
  mender_only: { label: "Mender only", tone: "warning" },
};

const EMAIL_LABEL: Record<AdminFleetEmailState, string> = {
  claimed_verified: "claimed & verified",
  contact_only: "contact only",
  none: "none",
};

const AUTH_TONE: Record<string, string> = {
  accepted: "online",
  pending: "warning",
  ambiguous: "warning",
  rejected: "offline",
  preauthorized: "info",
};

const MENDER_SOURCES = ["api", "shadow", "mirror"];

/** The address an email_state speaks of, or null with none on file. */
function addressOf(row: AdminFleetRow): string | null {
  if (row.email_state === "claimed_verified") return row.claim_email;
  if (row.email_state === "contact_only") return row.contact_email;
  return null;
}

/** Everything the search looks in, lower-cased. */
function haystack(row: AdminFleetRow): string {
  return [
    row.node_id,
    row.node_ref,
    row.mender_device_id,
    row.claim_email,
    row.contact_email,
    row.artifact_name,
    row.device_type,
    row.snapshot_issue,
  ]
    .filter(Boolean)
    .join("\n")
    .toLowerCase();
}

/** How old the last snapshot is, in seconds: the server's figure, else worked
 *  out from its time. Negative when the server clock went back. */
function snapshotAge(mirror: AdminFleetMirror): number | null {
  if (mirror.age_s !== null) return mirror.age_s;
  return mirror.last_snapshot_at ? Math.max(0, Date.now() / 1000 - mirror.last_snapshot_at) : null;
}

const UNKNOWN_SOURCE = "Registration is refusing nodes because MENDER_SOURCE is not recognised.";

interface MirrorBanner {
  tone: "warning" | "error";
  text: string;
  /** The server's own reason, set on a line of its own. */
  reason: string | null;
}

/** What the mirror and MENDER_SOURCE mean for registration, and how loudly to
 *  say so, or null while registration works. The cases follow the server: a
 *  value other than api, shadow or mirror refuses outright however fresh the
 *  snapshot, a mirror source refuses without a fresh snapshot, and api and
 *  shadow still ask Mender. */
function mirrorBanner(mirror: AdminFleetMirror): MirrorBanner | null {
  const recognised = MENDER_SOURCES.includes(mirror.mender_source);
  if (!mirror.stale) {
    return recognised ? null : { tone: "error", text: UNKNOWN_SOURCE, reason: null };
  }

  const age = snapshotAge(mirror);
  const parts: string[] = [];
  if (mirror.last_snapshot_at === null) {
    parts.push("No Mender snapshot has arrived yet.");
  } else if (age !== null && age < 0) {
    parts.push("The last Mender snapshot is dated in the future.");
  } else {
    const limit = mirror.max_age_s === null ? "" : ` (limit ${formatDuration(mirror.max_age_s)})`;
    parts.push(`The last Mender snapshot arrived ${formatDuration(age)} ago${limit}.`);
  }
  if (mirror.max_age_s === null) {
    parts.push("MENDER_MIRROR_MAX_AGE_S is malformed, so the mirror counts as stale.");
  }

  let tone: "warning" | "error" = "warning";
  if (!recognised) {
    tone = "error";
    parts.push(UNKNOWN_SOURCE);
  } else if (mirror.mender_source === "mirror") {
    tone = "error";
    parts.push("Registration is refusing nodes until a fresh snapshot arrives.");
  } else {
    parts.push("Registration still asks Mender directly.");
  }
  parts.push("Check the mender-auto-accept timer.");
  return { tone, text: parts.join(" "), reason: mirror.stale_reason };
}

function MirrorLine({ mirror }: { mirror: AdminFleetMirror }) {
  const ahead = mirror.age_s !== null && mirror.age_s < 0;
  return (
    <p className="fleet-mirror">
      Mender mirror:{" "}
      {mirror.last_snapshot_at ? (
        <>
          last snapshot {ahead ? "dated in the future" : <RelativeTime at={mirror.last_snapshot_at} />}
          {mirror.source && <> from {mirror.source}</>}
          {mirror.device_count !== null && <>, {mirror.device_count.toLocaleString()} devices</>}
        </>
      ) : (
        "no snapshot yet"
      )}
      . Registration source: <span className="mono">{mirror.mender_source}</span>.
    </p>
  );
}

/** A value fleet.css keeps to one line and ends in an ellipsis when its
 *  column runs out of room, so the title holds the whole of it. */
function Line({ className, text }: { className: string; text: string }) {
  return (
    <span className={className} title={text}>
      {text}
    </span>
  );
}

function Who({ row }: { row: AdminFleetRow }) {
  const id = row.node_id ?? row.mender_device_id;
  return (
    <div className="fleet-who">
      {id ? <Line className="mono fleet-id" text={id} /> : DASH}
      {row.node_ref && <Line className="mono fleet-sub fleet-ref" text={row.node_ref} />}
    </div>
  );
}

/** The node's registry status as the registry words it, not its liveness,
 *  which Last seen gives. */
function RegistryStatus({ row }: { row: AdminFleetRow }) {
  if (row.presence === "mender_only" && row.never_registered) {
    return <span className="badge warning">enrolled, never registered</span>;
  }
  if (!row.status) return <>{DASH}</>;
  return <span className={`badge ${REGISTRY_TONE[row.status] ?? "private"}`}>{row.status}</span>;
}

function Owner({ row }: { row: AdminFleetRow }) {
  const address = addressOf(row);
  return (
    <div className="fleet-who">
      {address && <Line className="fleet-email" text={address} />}
      <span className="fleet-sub">{EMAIL_LABEL[row.email_state] ?? row.email_state}</span>
    </div>
  );
}

/** The artifact Mender last saw installed, the device type under it. */
function Device({ row }: { row: AdminFleetRow }) {
  if (!row.artifact_name && !row.device_type) return <>{DASH}</>;
  return (
    <div className="fleet-who">
      {row.artifact_name ? <Line className="mono fleet-artifact" text={row.artifact_name} /> : DASH}
      {row.device_type && <Line className="fleet-sub fleet-type" text={row.device_type} />}
    </div>
  );
}

function MenderState({ row }: { row: AdminFleetRow }) {
  let auth: ReactNode = null;
  if (row.present === false) auth = <span className="badge offline">removed from Mender</span>;
  else if (row.auth_status) {
    auth = <span className={`badge ${AUTH_TONE[row.auth_status] ?? "private"}`}>{row.auth_status}</span>;
  }
  if (!row.snapshot_issue) return auth ?? <>{DASH}</>;
  return (
    <div className="fleet-who">
      <div className="fleet-badges">
        {auth}
        <span className="badge warning">not usable</span>
      </div>
      <span className="fleet-sub">{row.snapshot_issue}</span>
    </div>
  );
}

export default function FleetPage() {
  // Polled so the snapshot's age and the banner follow the clock.
  const polled = usePolling<AdminFleet>(() => api.adminFleet(), POLL_MS);
  const [q, setQ] = useState("");
  const [presence, setPresence] = useState<PresenceFilter>("all");
  const [page, setPage] = useState(0);

  const { data, loading } = polled;
  const all = Array.isArray(data?.rows) ? data.rows : [];
  const needle = q.trim().toLowerCase();
  const filtered = all.filter(
    (row) => (presence === "all" || row.presence === presence) && (!needle || haystack(row).includes(needle)),
  );
  const filtering = needle !== "" || presence !== "all";
  const totalPages = Math.ceil(filtered.length / PAGE_SIZE);
  const current = clampPage(page, totalPages);
  const start = current * PAGE_SIZE;
  const rows = filtered.slice(start, start + PAGE_SIZE);
  const banner = data?.mirror ? mirrorBanner(data.mirror) : null;
  const counts = data?.counts;
  const unusable = all.filter((row) => row.snapshot_issue).length;

  return (
    <>
      <div className="page-header">
        <h1>Fleet</h1>
        <p>Each node as the registry, its owner&rsquo;s claim and Mender report it.</p>
        {data?.mirror && <MirrorLine mirror={data.mirror} />}
      </div>
      <FetchNotice polled={polled} what="the fleet" />
      {banner && (
        <Notice tone={banner.tone}>
          {banner.text}
          {banner.reason && <div className="fleet-stale-reason">{banner.reason}</div>}
        </Notice>
      )}

      {!nothingLoaded(polled) && (
        <div className="stats-grid">
          <StatCard label="In both" value={counts ? counts.both.toLocaleString() : DASH} tone="success" />
          <StatCard
            label="Registry only"
            value={counts ? counts.registry_only.toLocaleString() : DASH}
            tone="warning"
          />
          <StatCard label="Mender only" value={counts ? counts.mender_only.toLocaleString() : DASH} tone="warning" />
          {unusable > 0 && <StatCard label="Not usable" value={unusable.toLocaleString()} tone="error" />}
        </div>
      )}

      <div className="toolbar">
        <input
          type="search"
          className="input"
          placeholder="Search nodes, emails, artifacts…"
          aria-label="Search the fleet"
          value={q}
          onChange={(e) => {
            setQ(e.target.value);
            setPage(0);
          }}
        />
        <select
          className="input"
          aria-label="Filter by presence"
          value={presence}
          onChange={(e) => {
            setPresence(e.target.value as PresenceFilter);
            setPage(0);
          }}
        >
          <option value="all">All</option>
          <option value="both">Both</option>
          <option value="registry_only">Registry only</option>
          <option value="mender_only">Mender only</option>
        </select>
        {rows.length > 0 && (
          <span className="card-note">
            Showing {(start + 1).toLocaleString()}–{(start + rows.length).toLocaleString()} of{" "}
            {filtered.length.toLocaleString()}
          </span>
        )}
      </div>

      {!nothingLoaded(polled) && (
        <div className="card fleet-card">
          <div className="card-header">
            <h3>Nodes</h3>
          </div>
          <DataTable
            headers={COLUMNS}
            count={rows.length}
            loading={loading}
            empty={filtering ? "No nodes match" : "No nodes in the registry or in Mender yet"}
          >
            {rows.map((row, i) => (
              <tr key={row.node_id ?? row.mender_device_id ?? `row-${start + i}`}>
                <td className="cell-primary" data-label="Node">
                  <Who row={row} />
                </td>
                <td data-label="Registry">
                  <RegistryStatus row={row} />
                </td>
                <td className="fleet-when" data-label="Last seen">
                  <RelativeTime at={row.last_seen_at} none="Never" />
                </td>
                <td data-label="Email">
                  <Owner row={row} />
                </td>
                <td data-label="Mender">
                  <MenderState row={row} />
                </td>
                <td data-label="Device">
                  <Device row={row} />
                </td>
                <td className="fleet-when" data-label="Inventory">
                  <RelativeTime at={row.inventory_updated_at} />
                </td>
                <td data-label="Presence">
                  <span className={`badge ${PRESENCE[row.presence]?.tone ?? "private"}`}>
                    {PRESENCE[row.presence]?.label ?? row.presence}
                  </span>
                </td>
              </tr>
            ))}
          </DataTable>
          <Pager
            page={current}
            totalPages={totalPages}
            onPage={setPage}
            note={`${filtered.length.toLocaleString()} node${filtered.length === 1 ? "" : "s"}`}
          />
        </div>
      )}
    </>
  );
}
