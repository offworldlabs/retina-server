import { useEffect, useId, useState, type KeyboardEvent } from "react";
import { HttpError } from "@retina/shared";

import { api } from "../../../api/client";
import { Dialog } from "../../../components/Dialog";
import { Notice } from "../../../components/Notice";
import type { AdminClaim, AdminUser } from "../../../types";
import { messageOf, sentence } from "../users/userChange";
import "./claims.css";

/** How many matching accounts the search shows; more are narrowed by typing. */
const RESULTS = 6;
const SEARCH_DELAY_MS = 300;

export type OwnerDialogMode = "reassign" | "unassign";

/** What the owner route did. `gone` is its 404 for the node itself, which the
 *  dialog says in place while the caller reads the node again. */
export type OwnerChange = { kind: "reassigned"; email: string } | { kind: "unassigned" } | { kind: "gone" };

interface Props {
  mode: OwnerDialogMode;
  /** The id the owner route takes. */
  nodeId: string;
  /** What the page calls the node. */
  nodeName: string;
  owner: AdminClaim["owner"];
  onClose: () => void;
  onChanged: (change: OwnerChange) => void;
}

/** The answer to one search, carrying what it was asked for and on which try. */
type Search = { q: string; attempt: number } & ({ users: AdminUser[]; total: number } | { error: string });

function current(owner: AdminClaim["owner"]): string {
  if (!owner) return "nobody";
  return owner.email ?? "an owner with no account";
}

function plural(n: number, noun: string): string {
  return `${n} ${noun}${n === 1 ? "" : "s"}`;
}

/** Giving a node to another account, or taking it from the one that has it.
 *  Either way the node's claim address, its verification and any claim link
 *  out are cleared, as a release clears them. Open while mounted. */
export function OwnerReassignDialog({ mode, nodeId, nodeName, owner, onClose, onChanged }: Props) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [nodeGone, setNodeGone] = useState(false);

  const [typed, setTyped] = useState("");
  const q = typed.trim();
  const [search, setSearch] = useState<Search | null>(null);
  // Bumped to ask the same question again: a retry, or after the account
  // picked turned out to be gone.
  const [attempt, setAttempt] = useState(0);
  const [pickedId, setPickedId] = useState<string | null>(null);

  const fieldId = useId();
  const legendId = useId();
  const confirmId = useId();
  const group = useId();

  useEffect(() => {
    if (mode !== "reassign" || !q) return;
    let live = true;
    const timer = setTimeout(() => {
      api.adminUsers({ q, limit: RESULTS }).then(
        (page) => {
          if (!live) return;
          const users = Array.isArray(page?.users) ? page.users : [];
          setSearch({ q, attempt, users, total: typeof page?.total === "number" ? page.total : users.length });
        },
        (e: unknown) => {
          if (live) setSearch({ q, attempt, error: messageOf(e) });
        },
      );
    }, SEARCH_DELAY_MS);
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [mode, q, attempt]);

  // The last list answered stays while the next search is in flight, so the
  // choices do not jump about under the pointer as the address is typed.
  const answered = q && search && search.q === q && search.attempt === attempt ? search : null;
  const users = q && search && "users" in search ? search.users : [];
  const picked = users.find((user) => user.id === pickedId) ?? null;

  let note: string;
  if (!q) note = "Type part of their address.";
  else if (!answered) note = "Searching…";
  else if ("error" in answered) note = sentence(`Could not search: ${answered.error}`);
  else if (answered.total === 0) note = `No account matches “${q}”.`;
  else if (answered.total > answered.users.length)
    note = `${answered.users.length} of ${answered.total} shown. Type more of the address to narrow them.`;
  else note = `${plural(answered.total, "account")} found.`;

  async function submit() {
    if (busy || nodeGone) return;
    const userId = mode === "unassign" ? null : picked?.id;
    if (userId === undefined) return;
    setBusy(true);
    setError(null);
    try {
      await api.adminSetNodeOwner(nodeId, userId);
    } catch (e) {
      setBusy(false);
      refused(e);
      return;
    }
    onChanged(picked && mode === "reassign" ? { kind: "reassigned", email: picked.email } : { kind: "unassigned" });
  }

  function refused(e: unknown) {
    const not = mode === "unassign" ? "Not unassigned" : owner ? "Not reassigned" : "Not assigned";
    if (e instanceof HttpError && e.status === 409 && mode === "unassign") {
      setError(
        `${not}: a polled radar keeps an owner while it is polled. Reassign it to another account, or have its owner remove it.`,
      );
      return;
    }
    if (e instanceof HttpError && e.status === 404 && mode === "reassign" && e.message === "User not found") {
      setError("That account no longer exists. Pick another.");
      setPickedId(null);
      setAttempt((n) => n + 1);
      return;
    }
    if (e instanceof HttpError && e.status === 404) {
      setError("This node is no longer registered, so it cannot have an owner.");
      setNodeGone(true);
      onChanged({ kind: "gone" });
      return;
    }
    setError(sentence(`${not}: ${messageOf(e)}`));
  }

  // Enter in the search box searches, as typing does, rather than confirming
  // a choice made before the last few letters changed the list.
  function onSearchKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Enter") event.preventDefault();
  }

  if (mode === "unassign") {
    return (
      <Dialog
        open
        title="Unassign the owner?"
        description={
          <>
            <strong>{nodeName}</strong> is taken from <strong>{current(owner)}</strong> and left with no owner. Its
            claim address and verification are reset, and it can be claimed again.
          </>
        }
        busy={busy}
        onClose={onClose}
        onSubmit={submit}
        actions={
          <button type="submit" className="btn btn-danger" disabled={busy || nodeGone}>
            {busy ? "Unassigning…" : "Unassign"}
          </button>
        }
      >
        {error && <Notice tone="error">{error}</Notice>}
      </Dialog>
    );
  }

  return (
    <Dialog
      open
      title={owner ? "Reassign owner" : "Assign owner"}
      description={
        <>
          Pick the account that should own <strong>{nodeName}</strong>.
        </>
      }
      busy={busy}
      onClose={onClose}
      onSubmit={submit}
      actions={
        <button
          type="submit"
          className="btn btn-primary"
          disabled={!picked || busy || nodeGone}
          aria-describedby={picked ? confirmId : undefined}
        >
          {busy ? (owner ? "Reassigning…" : "Assigning…") : owner ? "Reassign" : "Assign"}
        </button>
      }
    >
      <div className="dialog-field">
        <label htmlFor={fieldId}>Search accounts by email</label>
        <input
          id={fieldId}
          type="search"
          className="input"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={onSearchKey}
          readOnly={busy}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
        />
      </div>
      <div className="owner-search-note">
        <p role="status">{note}</p>
        {answered && "error" in answered && (
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => setAttempt((n) => n + 1)}>
            Retry
          </button>
        )}
      </div>
      {users.length > 0 && (
        <fieldset className="owner-results" role="radiogroup" aria-labelledby={legendId}>
          <legend id={legendId} className="owner-results-legend">
            Matching accounts
          </legend>
          {users.map((user) => {
            const owns = owner?.user_id === user.id;
            return (
              <label key={user.id} className="owner-option">
                <input
                  type="radio"
                  name={group}
                  value={user.id}
                  checked={pickedId === user.id}
                  onChange={() => setPickedId(user.id)}
                  disabled={owns || busy}
                />
                <span className="owner-option-who">
                  <span className="owner-option-email">{user.email}</span>
                  {user.name && <span className="owner-option-sub">{user.name}</span>}
                  {owns && <span className="owner-option-sub">Current owner</span>}
                </span>
                <span className={user.is_active ? "badge online" : "badge offline"}>
                  {user.is_active ? "Active" : "Deactivated"}
                </span>
              </label>
            );
          })}
        </fieldset>
      )}
      {picked && (
        <p id={confirmId} className="owner-confirm">
          {owner ? (
            <>
              <strong>{nodeName}</strong> moves from <strong>{current(owner)}</strong> to{" "}
              <strong>{picked.email}</strong>.
            </>
          ) : (
            <>
              <strong>{nodeName}</strong> has no owner now and goes to <strong>{picked.email}</strong>.
            </>
          )}{" "}
          Its claim address and verification are reset, and a claim link still out stops working.
          {!picked.is_active && " This account is deactivated, so they cannot sign in to see it."}
        </p>
      )}
      {error && <Notice tone="error">{error}</Notice>}
    </Dialog>
  );
}
