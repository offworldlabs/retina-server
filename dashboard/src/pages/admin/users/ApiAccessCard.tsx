import { useId, useState, type ReactNode } from "react";

import { api } from "../../../api/client";
import { DataTable, type ColumnHint } from "../../../components/DataTable";
import { Notice } from "../../../components/Notice";
import { useFetch } from "../../../hooks/usePolling";
import type { AdminUserApiAccess, ApiAccessStatus, ApiEntitlement, ApiKeyInfo } from "../../../types";
import { formatRelativeTime } from "../../../utils/format";
import { describeFailure } from "../../../utils/requestFailure";
import "../../user/ApiAccessPage.css";
import { GrantAccessDialog } from "./GrantAccessDialog";
import { messageOf, sentence } from "./userChange";

const HINTS: readonly ColumnHint[] = ["title", "primary", "secondary", "secondary", "primary", "primary"];

const NO_ACCESS: Record<NonNullable<ApiAccessStatus["reason"]>, string> = {
  lapsed: "API access has lapsed.",
  grant_ended: "Granted access has ended.",
  not_entitled: "No access yet.",
};

/** A day, or with `time` a moment, in the reader's own calendar and clock. */
function When({ at, time = false }: { at: string; time?: boolean }) {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return <>{at}</>;
  const text = time
    ? date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })
    : date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
  return <time dateTime={at}>{text}</time>;
}

function until(at: string | null) {
  return at ? (
    <>
      until <When at={at} />
    </>
  ) : (
    "no end date"
  );
}

/** Seconds as days, or hours or minutes where they do not make whole days. */
function spanOf(seconds: number): string {
  const [n, unit] =
    seconds % 86400 === 0 ? [seconds / 86400, "day"] : seconds % 3600 === 0 ? [seconds / 3600, "hour"] : [Math.round(seconds / 60), "minute"];
  return `${n} ${unit}${n === 1 ? "" : "s"}`;
}

function limitsOf(limits: NonNullable<ApiAccessStatus["limits"]>): string | null {
  const { requests_per_minute: rpm, max_concurrent_streams: streams, max_history_span_s: span } = limits;
  const parts = [
    rpm !== null && `${rpm} requests a minute`,
    streams !== null && `${streams} open streams`,
    span !== null && `history spans up to ${spanOf(span)}`,
  ].filter(Boolean);
  return parts.length ? `${parts.join(", ")}.` : null;
}

function isActiveGrant(entitlement: ApiEntitlement): boolean {
  return (
    entitlement.kind === "customer" &&
    entitlement.lapsed_at === null &&
    (entitlement.ends_at === null || new Date(entitlement.ends_at).getTime() > Date.now())
  );
}

function StatusBody({ access }: { access: ApiAccessStatus }) {
  const { contributor } = access;
  const limits = access.limits && limitsOf(access.limits);
  return (
    <>
      <div className="api-access-badges">
        <span className={access.entitled ? "badge online" : "badge offline"}>{access.entitled ? "Active" : "Not active"}</span>
        {(access.kind === "customer" || access.kind === "contributor") && (
          <span className="badge info">{access.kind === "customer" ? "Customer" : "Contributor"}</span>
        )}
      </div>
      {!access.entitled && <p className="api-access-text">{NO_ACCESS[access.reason ?? "not_entitled"]}</p>}
      {contributor && (
        <p className="api-access-text">
          Best node reported on {contributor.active_days} of the last {contributor.window_days} days (needs {contributor.threshold_days}).
          {contributor.grace_ends_at && (
            <>
              {" "}
              Access continues until <When at={contributor.grace_ends_at} time />, then pauses.
            </>
          )}
        </p>
      )}
      {access.entitled && access.kind === "customer" && (
        <p className="api-access-text">Granted access, {until(access.grant_ends_at)}.</p>
      )}
      {limits && <p className="api-access-text">Limits: {limits}</p>}
    </>
  );
}

function ConfirmRow({
  question,
  consequence,
  action,
  busy,
  onConfirm,
  onKeep,
}: {
  question: string;
  consequence: string;
  action: string;
  busy: boolean;
  onConfirm: () => void;
  onKeep: () => void;
}) {
  const questionId = useId();
  return (
    <div className="api-access-confirm" role="group" aria-labelledby={questionId}>
      <p id={questionId}>
        <strong>{question}</strong> {consequence}
      </p>
      <div className="btn-row">
        <button type="button" className="btn btn-danger" disabled={busy} onClick={onConfirm}>
          {busy ? "Revoking…" : action}
        </button>
        <button type="button" className="btn btn-secondary" disabled={busy} onClick={onKeep}>
          Keep
        </button>
      </div>
    </div>
  );
}

function keyStatus(row: ApiKeyInfo, entitled: boolean): ReactNode {
  if (row.revoked_at) {
    return (
      <span className="badge offline">
        Revoked <When at={row.revoked_at} />
      </span>
    );
  }
  return entitled ? <span className="badge online">Active</span> : <span className="badge warning">Paused</span>;
}

async function loadAccess(userId: string): Promise<{ access: AdminUserApiAccess; keys: ApiKeyInfo[] }> {
  const [access, keys] = await Promise.all([api.adminUserApiAccess(userId), api.adminUserApiKeys(userId)]);
  return { access, keys };
}

/** An account's standing on the data API: whether it has access, the customer
 *  access an administrator granted, and its keys. */
export function ApiAccessCard({ userId }: { userId: string }) {
  const { data, loading, error, refresh } = useFetch(() => loadAccess(userId), userId);
  const [granting, setGranting] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  async function revoke(run: () => Promise<unknown>, what: string) {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    try {
      await run();
      setConfirming(null);
      refresh();
    } catch (e) {
      setFailure(sentence(`Could not revoke ${what}: ${messageOf(e)}`));
    }
    setBusy(false);
  }

  const status = data?.access.access;
  const grants = data?.access.entitlements.filter(isActiveGrant) ?? [];
  const keys = data?.keys ?? [];

  let body: ReactNode;
  if (data && status) {
    body = (
      <>
        <StatusBody access={status} />
        {grants.map((grant) => (
          <div key={grant.id} className="api-access-grant">
            <p className="api-access-text">
              Customer access since <When at={grant.starts_at} />, {until(grant.ends_at)}.
            </p>
            {confirming === `grant:${grant.id}` ? (
              <ConfirmRow
                question="Revoke this grant?"
                consequence="The account's keys are kept and refused until it has access again."
                action="Revoke grant"
                busy={busy}
                onConfirm={() => void revoke(() => api.adminRevokeApiGrant(grant.id), "the grant")}
                onKeep={() => setConfirming(null)}
              />
            ) : (
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => setConfirming(`grant:${grant.id}`)}>
                Revoke grant
              </button>
            )}
          </div>
        ))}
        <DataTable headers={["Name", "Key", "Created", "Last used", "Status", "Actions"]} hints={HINTS} count={keys.length} empty="No keys">
          {keys.map((row) => (
            <tr key={row.id}>
              <td className="cell-primary">{row.name}</td>
              <td>
                <code className="mono api-access-prefix">rtk_{row.prefix}_…</code>
              </td>
              <td>
                <When at={row.created_at} />
              </td>
              <td>{row.last_used_at ? formatRelativeTime(row.last_used_at) : "Never"}</td>
              <td>{keyStatus(row, status.entitled)}</td>
              <td>
                {!row.revoked_at &&
                  (confirming === `key:${row.id}` ? (
                    <ConfirmRow
                      question={`Revoke ${row.name}?`}
                      consequence="Anything using it stops working at once."
                      action="Revoke key"
                      busy={busy}
                      onConfirm={() => void revoke(() => api.adminRevokeApiKey(row.id), row.name)}
                      onKeep={() => setConfirming(null)}
                    />
                  ) : (
                    <button type="button" className="btn btn-secondary btn-sm" aria-label={`Revoke ${row.name}`} onClick={() => setConfirming(`key:${row.id}`)}>
                      Revoke
                    </button>
                  ))}
              </td>
            </tr>
          ))}
        </DataTable>
        {failure && <Notice tone="error">{failure}</Notice>}
      </>
    );
  } else if (loading) body = <div className="empty-state">Loading…</div>;
  else {
    body = (
      <Notice tone="error" onRetry={refresh}>
        Could not load API access. {error ? describeFailure(error).message : ""}
      </Notice>
    );
  }

  return (
    <div className="card">
      <div className="card-header">
        <h3>API access</h3>
        {data && !grants.length && (
          <button type="button" className="btn btn-primary btn-sm" onClick={() => setGranting(true)}>
            Grant access
          </button>
        )}
      </div>
      <div className="card-body api-access-body">{body}</div>
      {granting && <GrantAccessDialog userId={userId} onClose={() => setGranting(false)} onGranted={refresh} />}
    </div>
  );
}
