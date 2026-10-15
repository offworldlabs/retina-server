import { useEffect, useId, useRef, useState, type FormEvent, type KeyboardEvent, type ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import { HttpError, type CurrentUser } from "@retina/shared";

import { api } from "../../api/client";
import { Notice } from "../../components/Notice";
import { useAuth } from "../../context/AuthContext";
import { useFetch } from "../../hooks/usePolling";
import type { AccountSession } from "../../types";
import { DASH, formatRelativeTime } from "../../utils/format";
import { describeFailure, type RequestFailure } from "../../utils/requestFailure";
import "./AccountPage.css";

/** The longest name the server keeps, in characters once trimmed. */
const NAME_MAX_CHARS = 64;

/** How long a confirmation link lasts when the answer does not say. */
const LINK_MINUTES = 15;

/** The server's reason for a refusal, where it wrote one to be shown as it is:
 *  FastAPI's `detail`, when it is a sentence rather than a validation list. */
function serverDetail(e: unknown): string | null {
  if (!(e instanceof HttpError)) return null;
  const detail = (e.body as { detail?: unknown } | null)?.detail;
  return typeof detail === "string" && detail.trim() ? detail : null;
}

/** Whether the failure is the server turning the value down, rather than a
 *  request that may pass if tried again. */
const refusedValue = (failure: RequestFailure) => failure.kind === "invalid" || failure.kind === "refused";

/** What a 429 on the address change means: its limits are per account and per
 *  address and can last up to an hour, which "wait a minute" would understate. */
const TOO_MANY_LINKS = "Too many links have been asked for. Try again later.";

/** Remembers whether the component is still on screen, for an answer that
 *  lands after it has gone. */
function useAlive() {
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  return alive;
}

/** Runs the focus a handler asked for once the render it caused has landed,
 *  when the element it names is on screen and enabled again. */
function useFocusNext<T extends string>(targets: Record<T, () => HTMLElement | null>) {
  const next = useRef<T | null>(null);
  useEffect(() => {
    const target = next.current;
    if (!target) return;
    next.current = null;
    targets[target]()?.focus();
  });
  return next;
}

/** Reads the account again when the page opens, whenever its tab comes back
 *  into view and whenever its window regains focus, so a page left open across
 *  an address change made elsewhere, in another tab or beside it, stops naming
 *  the old one. The auth context is refreshed only by the confirm page, in its
 *  own tab. Through the console's client, so a 401, the session ended or the
 *  account deleted meanwhile, ends the session here as any owner request's
 *  does. Any other failure changes nothing, and one read is out at a time:
 *  the focus and the visibility that arrive together are answered by one. */
function useFreshAccount() {
  const { updateUser } = useAuth();
  useEffect(() => {
    let reading = false;
    let live = true;
    function read() {
      if (reading) return;
      reading = true;
      api.me().then(
        (fresh) => {
          reading = false;
          if (live) updateUser(fresh);
        },
        () => {
          reading = false;
        },
      );
    }
    function onReturn() {
      if (document.visibilityState === "visible") read();
    }
    read();
    document.addEventListener("visibilitychange", onReturn);
    window.addEventListener("focus", onReturn);
    return () => {
      live = false;
      document.removeEventListener("visibilitychange", onReturn);
      window.removeEventListener("focus", onReturn);
    };
  }, [updateUser]);
}

export default function AccountPage() {
  const { user } = useAuth();
  useFreshAccount();
  // Behind the guard, so only while a sign-out is taking the page down.
  if (!user) return null;
  return (
    <>
      <div className="page-header">
        <h1>Account</h1>
        <p>Your name and sign-in address, where you are signed in, and deleting the account.</p>
      </div>
      <ProfileCard user={user} />
      <AddressCard email={user.email} />
      <SessionsCard />
      <DeleteCard email={user.email} />
    </>
  );
}

/* ── Profile ─────────────────────────────────────────────────────────────── */

type NameFailure = { kind: "refused"; message: string } | { kind: "failed"; failure: RequestFailure };

function ProfileCard({ user }: { user: CurrentUser }) {
  const { updateUser } = useAuth();
  const id = useId();
  const nameTitleId = `${id}name`;
  const hintId = `${id}name-hint`;
  const errorId = `${id}name-error`;

  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<NameFailure | null>(null);
  const [done, setDone] = useState<string | null>(null);
  // Remounts the refusal, so the same reason given twice is announced twice.
  const [refusals, setRefusals] = useState(0);

  const edit = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const alive = useAlive();
  const focusNext = useFocusNext({
    edit: () => edit.current,
    input: () => input.current,
  });

  function openForm() {
    setDraft(user.name ?? "");
    setFailure(null);
    setDone(null);
    setOpen(true);
    focusNext.current = "input";
  }

  function close() {
    setOpen(false);
    setFailure(null);
    focusNext.current = "edit";
  }

  async function save() {
    if (saving) return;
    // Nothing to change, and nothing for the server to record.
    if (draft.trim() === (user.name ?? "").trim()) {
      close();
      return;
    }
    setSaving(true);
    setFailure(null);
    let updated: CurrentUser;
    try {
      updated = await api.updateMe({ name: draft });
    } catch (e) {
      if (!alive.current) return;
      setSaving(false);
      const described = describeFailure(e);
      if (described.kind === "invalid") {
        setFailure({
          kind: "refused",
          message:
            serverDetail(e) ?? `A name needs 1 to ${NAME_MAX_CHARS} characters and no control characters.`,
        });
        setRefusals((n) => n + 1);
      } else {
        setFailure({ kind: "failed", failure: described });
      }
      focusNext.current = "input";
      return;
    }
    if (!alive.current) return;
    setSaving(false);
    setOpen(false);
    setDone("Name saved.");
    focusNext.current = "edit";
    // The header redraws from it, with no reload.
    updateUser(updated);
  }

  function submit(e: FormEvent) {
    e.preventDefault();
    void save();
  }

  // Escape belongs to the form it closes, not to a drawer behind it. A save in
  // flight cannot be called back, so it is waited for.
  function cancelOnEscape(e: KeyboardEvent) {
    if (e.key !== "Escape") return;
    e.stopPropagation();
    if (!saving) close();
  }

  const refused = failure?.kind === "refused";

  return (
    <div className="card">
      <div className="card-header">
        <h3>Profile</h3>
      </div>
      <div className="card-body">
        <div className="account-row">
          <div className="account-row-main">
            <h4 className="account-row-title">Email address</h4>
            <p className="account-value">{user.email}</p>
            <p className="account-note">Sign-in links are sent here.</p>
          </div>
        </div>

        <div className="account-row">
          <div className="account-row-main">
            <h4 className="account-row-title" id={nameTitleId}>
              Display name
            </h4>
            {open ? (
              <form className="account-form" onSubmit={submit} onKeyDown={cancelOnEscape} aria-busy={saving} noValidate>
                {/* No maxLength: a browser counts it in UTF-16 units, an emoji
                    as two, and would stop short a name the server keeps. */}
                <input
                  ref={input}
                  className="input"
                  value={draft}
                  autoComplete="name"
                  disabled={saving}
                  aria-labelledby={nameTitleId}
                  aria-invalid={refused || undefined}
                  aria-describedby={refused ? `${errorId} ${hintId}` : hintId}
                  onChange={(e) => {
                    setDraft(e.target.value);
                    if (refused) setFailure(null);
                  }}
                />
                {failure?.kind === "refused" && (
                  <p key={refusals} id={errorId} className="account-error" role="alert">
                    {failure.message}
                  </p>
                )}
                <p id={hintId} className="account-note">
                  Up to {NAME_MAX_CHARS} characters.
                </p>
                {failure?.kind === "failed" && (
                  <Notice tone="error" onRetry={failure.failure.retryable ? () => void save() : undefined}>
                    Could not save your name. {failure.failure.message}
                  </Notice>
                )}
                <div className="btn-row">
                  <button type="submit" className="btn btn-primary" disabled={saving}>
                    {saving ? "Saving…" : "Save"}
                  </button>
                  <button type="button" className="btn btn-secondary" disabled={saving} onClick={close}>
                    Cancel
                  </button>
                </div>
              </form>
            ) : (
              <>
                <p className={user.name ? "account-value" : "account-value unset"}>{user.name || "Not set"}</p>
                <p className="account-note">
                  {user.name
                    ? "The header shows it in place of your address."
                    : "Without one, the header shows your address."}
                </p>
              </>
            )}
            <p className="account-status" role="status">
              {done && (
                <>
                  <Tick />
                  {done}
                </>
              )}
            </p>
          </div>
          {!open && (
            <button
              ref={edit}
              type="button"
              className="btn btn-secondary btn-sm"
              aria-label="Edit display name"
              onClick={openForm}
            >
              Edit
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── Sign-in address ─────────────────────────────────────────────────────── */

/** What a failed address change says. A 400 (not an address, or the current
 *  one) carries the server's own reason: the route takes any string and
 *  answers those itself, so there is no 422 to word. Another account's
 *  address is not a refusal here: the request is answered as for a free one. */
function addressFailure(e: unknown): RequestFailure {
  const detail = serverDetail(e);
  return describeFailure(e, {
    ...(detail ? { refused: detail } : {}),
    throttled: TOO_MANY_LINKS,
  });
}

function minutesOf(seconds: unknown): number {
  const minutes = typeof seconds === "number" ? Math.round(seconds / 60) : NaN;
  return Number.isFinite(minutes) && minutes > 0 ? minutes : LINK_MINUTES;
}

function AddressCard({ email }: { email: string }) {
  const id = useId();
  const inputId = `${id}new-address`;
  const errorId = `${id}new-address-error`;

  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  // `from` is the address in use when the link was asked for: once the account
  // holds another, the link is no longer on its way.
  const [sent, setSent] = useState<{ to: string; minutes: number; from: string } | null>(null);
  const [seen, setSeen] = useState(email);
  if (seen !== email) {
    setSeen(email);
    setSent(null);
  }
  const [failure, setFailure] = useState<RequestFailure | null>(null);
  // Remounts the error, so the same failure twice is announced twice.
  const [failures, setFailures] = useState(0);
  const alive = useAlive();

  async function submit(e: FormEvent) {
    e.preventDefault();
    const address = draft.trim();
    if (sending || !address) return;
    setSending(true);
    setFailure(null);
    setSent(null);
    try {
      const answer = await api.requestEmailChange(address);
      if (!alive.current) return;
      setSent({ to: answer?.email || address, minutes: minutesOf(answer?.expires_in), from: email });
      setDraft("");
    } catch (err) {
      if (!alive.current) return;
      setFailure(addressFailure(err));
      setFailures((n) => n + 1);
    } finally {
      if (alive.current) setSending(false);
    }
  }

  const invalid = failure !== null && refusedValue(failure);

  return (
    <div className="card">
      <div className="card-header">
        <h3>Sign-in address</h3>
      </div>
      <div className="card-body">
        <p className="account-text">
          Sign-in links go to <strong className="account-address">{email}</strong>. To use another address, we send a
          link to it, and the change is made when you open it.
        </p>
        {/* noValidate: the server decides what an address is, and its answer
            is shown inline, as the login card does. */}
        <form className="account-form" onSubmit={submit} aria-busy={sending} noValidate>
          <label className="account-label" htmlFor={inputId}>
            New address
          </label>
          <input
            id={inputId}
            className="input"
            type="email"
            name="email"
            autoComplete="email"
            value={draft}
            disabled={sending}
            aria-invalid={invalid || undefined}
            aria-describedby={failure ? errorId : undefined}
            onChange={(e) => {
              setDraft(e.target.value);
              if (failure) setFailure(null);
            }}
          />
          {failure && (
            <p key={failures} id={errorId} className="account-error" role="alert">
              {failure.message}
            </p>
          )}
          <div className="btn-row">
            <button type="submit" className="btn btn-primary" disabled={sending || !draft.trim()}>
              {sending ? "Sending…" : "Send link"}
            </button>
          </div>
        </form>
        <p className="account-status" role="status">
          {sent && sent.from === email && (
            <>
              <Tick />
              <span>
                A link is on its way to <strong className="account-address">{sent.to}</strong>. It lasts{" "}
                {sent.minutes} {sent.minutes === 1 ? "minute" : "minutes"}. Until it is opened,{" "}
                <strong className="account-address">{email}</strong> still signs you in.
              </span>
            </>
          )}
        </p>
      </div>
    </div>
  );
}

/* ── Sessions ────────────────────────────────────────────────────────────── */

/** What the busy state names when every other session is being signed out. */
const OTHERS = "others";

function deviceOf(session: AccountSession): string {
  return session.user_agent?.trim() || "Unknown browser";
}

/** A day, in the reader's own calendar, or null for a stamp that is not one. */
function dayOf(epochSeconds: unknown): { label: string; iso: string } | null {
  if (typeof epochSeconds !== "number" || !Number.isFinite(epochSeconds)) return null;
  const moment = new Date(epochSeconds * 1000);
  if (Number.isNaN(moment.getTime())) return null;
  return {
    label: moment.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }),
    iso: moment.toISOString(),
  };
}

function signedOutOthers(revoked: unknown): string {
  if (typeof revoked !== "number" || !(revoked > 0)) return "No other browser was signed in.";
  return revoked === 1 ? "Signed out 1 other session." : `Signed out ${revoked} other sessions.`;
}

function SessionsCard() {
  const { session } = useAuth();
  // Asked again under a new session, which is another browser's row to mark.
  const polled = useFetch(() => api.mySessions(), session);
  const { data, loading, error, refresh } = polled;

  // Ended from this card: the server has said so, so they go at once.
  const [ended, setEnded] = useState<ReadonlySet<string>>(() => new Set());
  const [busy, setBusy] = useState<string | null>(null);
  const [failure, setFailure] = useState<{ message: string; retry?: () => void } | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const status = useRef<HTMLParagraphElement>(null);
  const alive = useAlive();
  // The button pressed goes with its row, so focus comes to the outcome.
  const focusNext = useFocusNext({ status: () => status.current });

  const rows = (Array.isArray(data) ? data : [])
    .filter((row) => !ended.has(row.id))
    // This browser first; the server's order, most recent first, after it.
    .sort((a, b) => Number(Boolean(b.current)) - Number(Boolean(a.current)));
  const others = rows.filter((row) => !row.current);

  function end(ids: string[], message: string) {
    setEnded((prev) => new Set([...prev, ...ids]));
    setDone(message);
    focusNext.current = "status";
  }

  async function revoke(row: AccountSession) {
    if (busy) return;
    const device = deviceOf(row);
    setBusy(row.id);
    setFailure(null);
    setDone(null);
    try {
      await api.revokeSession(row.id);
    } catch (e) {
      if (!alive.current) return;
      setBusy(null);
      // Ended already, here or elsewhere: what was asked for.
      if (e instanceof HttpError && e.status === 404) {
        end([row.id], `${device} was already signed out.`);
        return;
      }
      const described = describeFailure(e);
      setFailure({
        message: `Could not sign out ${device}. ${described.message}`,
        retry: described.retryable ? () => void revoke(row) : undefined,
      });
      return;
    }
    if (!alive.current) return;
    setBusy(null);
    end([row.id], `Signed out ${device}.`);
  }

  async function revokeOthers() {
    if (busy) return;
    const ids = others.map((row) => row.id);
    setBusy(OTHERS);
    setFailure(null);
    setDone(null);
    let revoked: number;
    try {
      ({ revoked } = await api.revokeOtherSessions());
    } catch (e) {
      if (!alive.current) return;
      setBusy(null);
      const described = describeFailure(e);
      setFailure({
        message: `Could not sign out the other sessions. ${described.message}`,
        retry: described.retryable ? () => void revokeOthers() : undefined,
      });
      return;
    }
    if (!alive.current) return;
    setBusy(null);
    end(ids, signedOutOthers(revoked));
  }

  let body: ReactNode;
  if (loading) {
    body = <div className="empty-state">Loading…</div>;
  } else if (error && !data) {
    body = (
      <Notice tone="error" onRetry={refresh}>
        Could not load your sessions. {describeFailure(error).message}
      </Notice>
    );
  } else {
    body = (
      <>
        {error && (
          <Notice onRetry={refresh}>Could not refresh your sessions. {describeFailure(error).message}</Notice>
        )}
        {rows.length === 0 ? (
          <div className="empty-state">No signed-in browsers to show.</div>
        ) : (
          <ul className="account-sessions">
            {rows.map((row) => (
              <SessionRow
                key={row.id}
                row={row}
                busy={busy}
                onRevoke={() => void revoke(row)}
              />
            ))}
          </ul>
        )}
        {rows.length > 0 && others.length === 0 && (
          <p className="account-note">No other browser is signed in to your account.</p>
        )}
      </>
    );
  }

  return (
    <div className="card account-sessions-card">
      <div className="card-header">
        <h3>Sessions</h3>
        {others.length > 0 && (
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={busy !== null}
            onClick={() => void revokeOthers()}
          >
            {busy === OTHERS ? "Signing out…" : "Sign out everywhere else"}
          </button>
        )}
      </div>
      <div className="card-body account-sessions-body">
        {body}
        {failure && (
          <Notice tone="error" onRetry={failure.retry}>
            {failure.message}
          </Notice>
        )}
        <p ref={status} tabIndex={-1} className="account-status" role="status">
          {done && (
            <>
              <Tick />
              {done}
            </>
          )}
        </p>
      </div>
    </div>
  );
}

function SessionRow({ row, busy, onRevoke }: { row: AccountSession; busy: string | null; onRevoke: () => void }) {
  const id = useId();
  const metaId = `${id}meta`;
  const device = deviceOf(row);
  const signedIn = dayOf(row.created_at);
  return (
    <li className="account-session">
      <div className="account-session-main">
        <p className="account-session-device">
          <span className="account-session-name">{device}</span>
          {row.current && <span className="badge info">This browser</span>}
        </p>
        <dl id={metaId} className="account-session-meta">
          <div>
            <dt>Network</dt>
            <dd className="mono">{row.ip || DASH}</dd>
          </div>
          <div>
            <dt>Last active</dt>
            <dd>{formatRelativeTime(row.last_used_at)}</dd>
          </div>
          <div>
            <dt>Signed in</dt>
            <dd>{signedIn ? <time dateTime={signedIn.iso}>{signedIn.label}</time> : DASH}</dd>
          </div>
        </dl>
      </div>
      {/* None for this browser: ending it is signing out, which the account
          menu does. */}
      {!row.current && (
        <button
          type="button"
          className="btn btn-secondary btn-sm"
          aria-label={`Sign out ${device}`}
          aria-describedby={metaId}
          disabled={busy !== null}
          onClick={onRevoke}
        >
          {busy === row.id ? "Signing out…" : "Sign out"}
        </button>
      )}
    </li>
  );
}

/* ── Delete account ──────────────────────────────────────────────────────── */

/** The address as the server compares it: trimmed, in any case. */
const sameAddress = (typed: string, email: string) => typed.trim().toLowerCase() === email.trim().toLowerCase();

function DeleteCard({ email }: { email: string }) {
  const { deleteAccount } = useAuth();
  const navigate = useNavigate();
  const id = useId();
  const inputId = `${id}confirm-address`;
  const errorId = `${id}confirm-address-error`;
  const questionId = `${id}confirm-question`;

  const [typed, setTyped] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // `refused`: the server said the address is not this account's, which sends
  // the person back to the field. Anything else is said at the confirmation.
  const [failure, setFailure] = useState<{ refused: boolean; failure: RequestFailure } | null>(null);
  const [failures, setFailures] = useState(0);

  const matches = sameAddress(typed, email);

  const field = useRef<HTMLInputElement>(null);
  const start = useRef<HTMLButtonElement>(null);
  const keep = useRef<HTMLButtonElement>(null);
  const alive = useAlive();
  const focusNext = useFocusNext({
    field: () => field.current,
    start: () => start.current,
    keep: () => keep.current,
  });

  function ask(e: FormEvent) {
    e.preventDefault();
    if (!matches || confirming) return;
    setFailure(null);
    setConfirming(true);
    // The way back first, as for any step that cannot be taken back.
    focusNext.current = "keep";
  }

  function cancel() {
    setConfirming(false);
    setFailure(null);
    focusNext.current = "start";
  }

  async function remove() {
    if (deleting || !matches) return;
    setDeleting(true);
    setFailure(null);
    try {
      // On success the identity goes, and this page with it, for the login
      // card in its place in history: the account page is no longer there to
      // go back to.
      await deleteAccount(typed.trim(), () => navigate("/login", { replace: true }));
    } catch (e) {
      if (!alive.current) return;
      setDeleting(false);
      const detail = serverDetail(e);
      const described = describeFailure(e, detail ? { refused: detail } : {});
      const refused = e instanceof HttpError && e.status === 400;
      setFailure({ refused, failure: described });
      setFailures((n) => n + 1);
      if (refused) {
        setConfirming(false);
        focusNext.current = "field";
      }
    }
  }

  const fieldRefused = failure?.refused === true;

  return (
    <div className="card account-danger">
      <div className="card-header">
        <h3>Delete account</h3>
      </div>
      <div className="card-body">
        <p className="account-text">
          Deleting your account releases your nodes so that anyone can claim them, signs out every browser and removes
          the account. It cannot be undone.
        </p>
        <form className="account-form" onSubmit={ask} aria-busy={deleting} noValidate>
          <label className="account-label" htmlFor={inputId}>
            Type <strong className="account-address">{email}</strong> to confirm
          </label>
          <input
            ref={field}
            id={inputId}
            className="input"
            type="text"
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            value={typed}
            disabled={confirming || deleting}
            aria-invalid={fieldRefused || undefined}
            aria-describedby={fieldRefused ? errorId : undefined}
            onChange={(e) => {
              setTyped(e.target.value);
              if (fieldRefused) setFailure(null);
            }}
          />
          {failure?.refused && (
            <p key={failures} id={errorId} className="account-error" role="alert">
              {failure.failure.message}
            </p>
          )}
          {confirming ? (
            <div className="account-confirm" role="group" aria-labelledby={questionId}>
              <p id={questionId}>
                <strong>Delete your account?</strong> This cannot be undone.
              </p>
              {failure && !failure.refused && (
                <Notice key={failures} tone="error" onRetry={failure.failure.retryable ? () => void remove() : undefined}>
                  Could not delete your account. {failure.failure.message}
                </Notice>
              )}
              <div className="btn-row">
                <button type="button" className="btn btn-danger" disabled={deleting} onClick={() => void remove()}>
                  {deleting ? "Deleting…" : "Yes, delete my account"}
                </button>
                <button ref={keep} type="button" className="btn btn-secondary" disabled={deleting} onClick={cancel}>
                  Keep my account
                </button>
              </div>
            </div>
          ) : (
            <div className="btn-row">
              <button ref={start} type="submit" className="btn btn-danger" disabled={!matches}>
                Delete account
              </button>
            </div>
          )}
        </form>
      </div>
    </div>
  );
}

function Tick() {
  return (
    <svg
      className="account-tick"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <path d="M3.5 8.5l3 3 6-7" />
    </svg>
  );
}
