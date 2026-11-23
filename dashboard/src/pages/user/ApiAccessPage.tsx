import { useId, useState, type ReactNode } from "react";
import { HttpError } from "@retina/shared";

import { api } from "../../api/client";
import { Dialog } from "../../components/Dialog";
import { Notice } from "../../components/Notice";
import { DataTable, type ColumnHint } from "../../components/DataTable";
import { useFetch } from "../../hooks/usePolling";
import type { ApiAccessStatus, ApiKeyInfo, ApiTierLimits, CreatedApiKey } from "../../types";
import { formatRelativeTime } from "../../utils/format";
import { describeFailure } from "../../utils/requestFailure";
import { useCopy } from "./dataExplorer/useCopy";
import "./ApiAccessPage.css";

/** The longest name the server keeps for a key. */
const NAME_MAX_CHARS = 100;

const HINTS: readonly ColumnHint[] = ["title", "primary", "secondary", "secondary", "primary", "primary"];

/** The server's reason for a refusal where it wrote a sentence, else the
 *  failure's own words. */
function refusal(e: unknown): string {
  const detail = e instanceof HttpError ? (e.body as { detail?: unknown } | null)?.detail : null;
  if (typeof detail === "string" && detail.trim()) return detail;
  return describeFailure(e, { invalid: `Use a name of 1 to ${NAME_MAX_CHARS} characters.` }).message;
}

/** A moment, in the reader's own calendar and clock. */
function Moment({ at }: { at: string }) {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return <>{at}</>;
  return <time dateTime={at}>{date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" })}</time>;
}

/** A day, in the reader's own calendar. */
function Day({ at }: { at: string }) {
  const date = new Date(at);
  if (Number.isNaN(date.getTime())) return <>{at}</>;
  return <time dateTime={at}>{date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" })}</time>;
}

/** Seconds as days, or hours or minutes where they do not make whole days. */
function spanOf(seconds: number): string {
  const [n, unit] =
    seconds % 86400 === 0 ? [seconds / 86400, "day"] : seconds % 3600 === 0 ? [seconds / 3600, "hour"] : [Math.round(seconds / 60), "minute"];
  return `${n} ${unit}${n === 1 ? "" : "s"}`;
}

function limitsOf({ requests_per_minute: rpm, max_concurrent_streams: streams, max_history_span_s: span }: ApiTierLimits) {
  const parts = [
    rpm !== null && `${rpm} requests a minute`,
    streams !== null && `${streams} open streams`,
    span !== null && `history spans up to ${spanOf(span)}`,
  ].filter(Boolean);
  return parts.length ? `${parts.join(", ")}.` : null;
}

const NO_ACCESS: Record<NonNullable<ApiAccessStatus["reason"]>, string> = {
  lapsed: "Your API access has lapsed.",
  grant_ended: "The access granted to you has ended.",
  not_entitled: "You have no access yet.",
};

function StatusBody({ access }: { access: ApiAccessStatus }) {
  const { contributor } = access;
  if (access.kind === "admin") {
    return (
      <>
        <div className="api-access-badges">
          <span className="badge online">Full access</span>
        </div>
        <p className="api-access-text">
          You are signed in as an administrator, so the data API is open to you without a key. Keys belong to accounts:
          to create one, sign in with an account.
        </p>
      </>
    );
  }
  if (!access.entitled) {
    return (
      <>
        <div className="api-access-badges">
          <span className="badge offline">Not active</span>
        </div>
        <p className="api-access-text">{NO_ACCESS[access.reason ?? "not_entitled"]}</p>
        {contributor && (
          <p className="api-access-text">
            To qualify, a node you own needs to report data on at least {contributor.threshold_days} of the last{" "}
            {contributor.window_days} days. Your best node: {contributor.active_days} of {contributor.window_days} days.
          </p>
        )}
      </>
    );
  }
  const limits = access.limits && limitsOf(access.limits);
  return (
    <>
      <div className="api-access-badges">
        <span className="badge online">Active</span>
        <span className="badge info">{access.kind === "customer" ? "Customer" : "Contributor"}</span>
      </div>
      {access.kind === "customer" ? (
        <p className="api-access-text">
          Granted by an administrator,{" "}
          {access.grant_ends_at ? (
            <>
              until <Moment at={access.grant_ends_at} />.
            </>
          ) : (
            "with no end date."
          )}
        </p>
      ) : (
        contributor && (
          <p className="api-access-text">
            A node you own reported data on {contributor.active_days} of the last {contributor.window_days} days.
            {contributor.grace_ends_at && (
              <>
                {" "}
                That is below {contributor.threshold_days} days: access continues until <Moment at={contributor.grace_ends_at} />
                , then pauses.
              </>
            )}
          </p>
        )
      )}
      {limits && <p className="api-access-text">Limits: {limits}</p>}
    </>
  );
}

function StatusCard({ access, error, loading, retry }: { access: ApiAccessStatus | null; error: Error | null; loading: boolean; retry: () => void }) {
  let body: ReactNode;
  if (loading) body = <div className="empty-state">Loading…</div>;
  else if (!access) {
    body = (
      <Notice tone="error" onRetry={retry}>
        Could not load your API access. {error ? describeFailure(error).message : ""}
      </Notice>
    );
  } else body = <StatusBody access={access} />;
  return (
    <div className="card">
      <div className="card-header">
        <h3>Access</h3>
      </div>
      <div className="card-body api-access-body">{body}</div>
    </div>
  );
}

/* ── Keys ────────────────────────────────────────────────────────────────── */

function CreateKeyDialog({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const fieldId = useId();
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The secret lives only here: closing the dialog unmounts it and it is gone.
  const [created, setCreated] = useState<CreatedApiKey | null>(null);
  const [outcome, copy] = useCopy();
  const trimmed = name.trim();

  async function create() {
    if (!trimmed || busy) return;
    setBusy(true);
    setError(null);
    try {
      setCreated(await api.createApiKey(trimmed));
      onCreated();
    } catch (e) {
      setError(refusal(e));
    }
    setBusy(false);
  }

  if (created) {
    return (
      <Dialog
        open
        title="Your new key"
        description="Copy it now and keep it somewhere safe."
        onClose={onClose}
        cancelLabel="Done"
        actions={
          <button type="button" className="btn btn-primary" onClick={() => void copy(created.key)}>
            Copy
          </button>
        }
      >
        <div className="dialog-field">
          <label htmlFor={fieldId}>Key</label>
          <input
            id={fieldId}
            className="input api-access-secret"
            readOnly
            value={created.key}
            autoComplete="off"
            spellCheck={false}
            onFocus={(e) => e.currentTarget.select()}
          />
        </div>
        <p role="status" className="api-access-note">
          {outcome}
        </p>
        <Notice>This is the only time the key is shown. If it is lost, revoke it and create another.</Notice>
      </Dialog>
    );
  }
  return (
    <Dialog
      open
      title="Create key"
      description="Name it for what will use it. The key is shown once, after you create it."
      busy={busy}
      onClose={onClose}
      onSubmit={() => void create()}
      actions={
        <button type="submit" className="btn btn-primary" disabled={!trimmed || busy}>
          {busy ? "Creating…" : "Create key"}
        </button>
      }
    >
      <div className="dialog-field">
        <label htmlFor={fieldId}>Name</label>
        <input
          id={fieldId}
          className="input"
          value={name}
          maxLength={NAME_MAX_CHARS}
          required
          readOnly={busy}
          autoComplete="off"
          spellCheck={false}
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      {error && <Notice tone="error">{error}</Notice>}
    </Dialog>
  );
}

function KeyRow({
  row,
  entitled,
  confirming,
  busy,
  onAsk,
  onKeep,
  onRevoke,
}: {
  row: ApiKeyInfo;
  entitled: boolean;
  confirming: boolean;
  busy: boolean;
  onAsk: () => void;
  onKeep: () => void;
  onRevoke: () => void;
}) {
  const questionId = useId();
  let status: ReactNode;
  if (row.revoked_at) {
    status = (
      <span className="api-access-status">
        <span className="badge offline">Revoked</span>
        <span className="api-access-when">
          <Day at={row.revoked_at} />
        </span>
      </span>
    );
  } else status = entitled ? <span className="badge online">Active</span> : <span className="badge warning">Paused</span>;
  return (
    <tr>
      <td className="cell-primary">{row.name}</td>
      <td>
        <code className="mono api-access-prefix">rtk_{row.prefix}_…</code>
      </td>
      <td>
        <Day at={row.created_at} />
      </td>
      <td>{row.last_used_at ? formatRelativeTime(row.last_used_at) : "Never"}</td>
      <td>{status}</td>
      <td>
        {!row.revoked_at &&
          (confirming ? (
            <div className="api-access-confirm" role="group" aria-labelledby={questionId}>
              <p id={questionId}>
                <strong>Revoke {row.name}?</strong> Anything using it stops working at once.
              </p>
              <div className="btn-row">
                <button type="button" className="btn btn-danger" disabled={busy} onClick={onRevoke}>
                  {busy ? "Revoking…" : "Revoke key"}
                </button>
                <button type="button" className="btn btn-secondary" disabled={busy} onClick={onKeep}>
                  Keep
                </button>
              </div>
            </div>
          ) : (
            <button type="button" className="btn btn-secondary btn-sm" aria-label={`Revoke ${row.name}`} onClick={onAsk}>
              Revoke
            </button>
          ))}
      </td>
    </tr>
  );
}

function KeysCard({ entitled }: { entitled: boolean }) {
  const { data, loading, error, refresh } = useFetch(() => api.myApiKeys());
  const [creating, setCreating] = useState(false);
  const [confirming, setConfirming] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const rows = Array.isArray(data) ? data : [];

  async function revoke(row: ApiKeyInfo) {
    if (busy) return;
    setBusy(true);
    setFailure(null);
    try {
      await api.revokeApiKey(row.id);
      setConfirming(null);
      refresh();
    } catch (e) {
      // Gone already, here or elsewhere: what was asked for.
      if (e instanceof HttpError && e.status === 404) {
        setConfirming(null);
        refresh();
      } else setFailure(`Could not revoke ${row.name}. ${refusal(e)}`);
    }
    setBusy(false);
  }

  return (
    <div className="card">
      <div className="card-header">
        <h3>Keys</h3>
        {entitled && (
          <button type="button" className="btn btn-primary btn-sm" onClick={() => setCreating(true)}>
            Create key
          </button>
        )}
      </div>
      <div className="card-body api-access-body">
        <p className="api-access-text">
          Send a key as the <code>x-apikey</code> header or as <code>Authorization: Bearer &lt;key&gt;</code>; websockets
          take <code>?apikey=&lt;key&gt;</code>. A key works only while your access is active.
        </p>
        {error && !data ? (
          <Notice tone="error" onRetry={refresh}>
            Could not load your keys. {describeFailure(error).message}
          </Notice>
        ) : (
          <DataTable
            headers={["Name", "Key", "Created", "Last used", "Status", "Actions"]}
            hints={HINTS}
            count={rows.length}
            loading={loading}
            empty="No keys yet"
          >
            {rows.map((row) => (
              <KeyRow
                key={row.id}
                row={row}
                entitled={entitled}
                confirming={confirming === row.id}
                busy={busy}
                onAsk={() => setConfirming(row.id)}
                onKeep={() => setConfirming(null)}
                onRevoke={() => void revoke(row)}
              />
            ))}
          </DataTable>
        )}
        {failure && <Notice tone="error">{failure}</Notice>}
        {!entitled && rows.length > 0 && (
          <p className="api-access-note">Keys are kept and refused until your access is active again.</p>
        )}
      </div>
      {creating && <CreateKeyDialog onClose={() => setCreating(false)} onCreated={refresh} />}
    </div>
  );
}

export default function ApiAccessPage() {
  const { data, loading, error, refresh } = useFetch(() => api.myApiAccess());
  return (
    <>
      <div className="page-header">
        <h1>API access</h1>
        <p>Keys let your own scripts and tools read network data through the data API.</p>
      </div>
      <StatusCard access={data} error={error} loading={loading} retry={refresh} />
      {data && data.kind !== "admin" && <KeysCard entitled={data.entitled} />}
    </>
  );
}
