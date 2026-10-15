import { useEffect, useId, useRef, useState } from "react";
import { HttpError } from "@retina/shared";

import { api } from "../../../api/client";
import type { AdminUser } from "../../../types";
import { ChangeEmailDialog } from "./ChangeEmailDialog";
import { DeleteUserDialog } from "./DeleteUserDialog";
import { messageOf, sentence, type UserChange } from "./userChange";
import "./users.css";

export type { UserChange } from "./userChange";

/** The sign-in link refusals whose detail says why in words an administrator
 *  can act on: deactivated, too many links out, no mail can leave. */
const EXPLAINED = new Set([409, 429, 503]);

type Busy = "state" | "link" | null;
type FocusTarget = "toggle" | "keep" | "link" | "email" | "delete";

interface Props {
  user: AdminUser;
  /** After each change the server confirmed, so the caller can refetch. */
  onChanged: (change: UserChange) => void;
  /** As an action starts, before any answer, so a list can take down what it
   *  said about an earlier one. */
  onAction?: () => void;
}

/** What an administrator can do to an account: deactivate or reactivate it,
 *  mail it a sign-in link, mail a link that moves it to a new address, or
 *  delete it. Each outcome is said in a polite live region beside them. */
export function UserActions({ user, onChanged, onAction }: Props) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState<Busy>(null);
  const [dialog, setDialog] = useState<"delete" | "email" | null>(null);
  const [status, setStatus] = useState<{ text: string; error: boolean } | null>(null);
  // The server's answer to a deactivate or reactivate, until the caller hands
  // over a refetched row in place of the one it was given for.
  const [answered, setAnswered] = useState<{ row: AdminUser; active: boolean } | null>(null);
  const active = answered?.row === user ? answered.active : user.is_active;

  const root = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const keep = useRef<HTMLButtonElement>(null);
  const link = useRef<HTMLButtonElement>(null);
  const email = useRef<HTMLButtonElement>(null);
  const remove = useRef<HTMLButtonElement>(null);
  const refocus = useRef<FocusTarget | null>(null);
  const promptId = useId();

  // A step that swaps or disables the focused button, or closes a dialog,
  // hands focus to the button that carries on from it, unless the user has
  // moved on elsewhere. Safari never focused the button clicked, so a dialog
  // has nothing of its own to give focus back to there.
  useEffect(() => {
    const target = refocus.current;
    if (!target) return;
    refocus.current = null;
    const current = document.activeElement;
    if (current && current !== document.body && !root.current?.contains(current)) return;
    ({ toggle, keep, link, email, delete: remove })[target].current?.focus();
  });

  const idle = busy === null;

  function begin(next: Busy) {
    setBusy(next);
    setStatus(null);
    onAction?.();
  }

  function openDialog(next: "delete" | "email") {
    setDialog(next);
    onAction?.();
  }

  /** Back to idle, focus to `then` once the buttons are enabled again. */
  function end(then: FocusTarget) {
    refocus.current = then;
    setBusy(null);
  }

  function failed(e: unknown, what: string) {
    if (e instanceof HttpError && e.status === 404) {
      setStatus({ text: "This account no longer exists.", error: true });
      onChanged({ kind: "gone" });
      return;
    }
    setStatus({ text: sentence(`${what}: ${messageOf(e)}`), error: true });
  }

  async function deactivate() {
    begin("state");
    try {
      const account = await api.adminDeactivateUser(user.id);
      setAnswered({ row: user, active: account?.is_active ?? false });
      setConfirming(false);
      setStatus({ text: "Deactivated. All their sessions have ended.", error: false });
      end("toggle");
      onChanged({ kind: "deactivated" });
    } catch (e) {
      end("keep");
      failed(e, "Could not deactivate");
    }
  }

  async function reactivate() {
    begin("state");
    try {
      const account = await api.adminReactivateUser(user.id);
      setAnswered({ row: user, active: account?.is_active ?? true });
      setStatus({ text: "Reactivated. They sign in again with a new link.", error: false });
      end("toggle");
      onChanged({ kind: "reactivated" });
    } catch (e) {
      end("toggle");
      failed(e, "Could not reactivate");
    }
  }

  async function sendLink() {
    begin("link");
    try {
      const sent = await api.adminSendSignInLink(user.id);
      setStatus({ text: `Sign-in link queued for ${sent?.email || user.email}.`, error: false });
    } catch (e) {
      if (e instanceof HttpError && EXPLAINED.has(e.status)) {
        setStatus({ text: sentence(`Not sent: ${e.message}`), error: true });
      } else {
        failed(e, "Could not send the link");
      }
    } finally {
      end("link");
    }
  }

  function closeDialog() {
    refocus.current = dialog === "email" ? "email" : "delete";
    setDialog(null);
  }

  function dialogChanged(change: UserChange) {
    closeDialog();
    if (change.kind === "email-sent") {
      setStatus({
        text: `Confirmation link sent to ${change.email}. The address changes once it is opened.`,
        error: false,
      });
    }
    if (change.kind === "gone") setStatus({ text: "This account no longer exists.", error: true });
    onChanged(change);
  }

  return (
    <div ref={root} className="user-actions" role="group" aria-label={`Actions for ${user.email}`}>
      {confirming ? (
        <div className="user-actions-confirm" role="group" aria-labelledby={promptId}>
          <p id={promptId} className="user-actions-prompt">
            Deactivate? It ends every session they have.
          </p>
          <div className="btn-row">
            <button type="button" className="btn btn-danger btn-sm" onClick={deactivate} disabled={!idle}>
              {busy === "state" ? "Deactivating…" : "Deactivate"}
            </button>
            <button
              ref={keep}
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                refocus.current = "toggle";
                setConfirming(false);
              }}
              disabled={!idle}
            >
              Keep active
            </button>
          </div>
        </div>
      ) : (
        <div className="btn-row">
          {active ? (
            <button
              ref={toggle}
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => {
                refocus.current = "keep";
                setStatus(null);
                setConfirming(true);
                onAction?.();
              }}
              disabled={!idle}
            >
              Deactivate
            </button>
          ) : (
            <button ref={toggle} type="button" className="btn btn-secondary btn-sm" onClick={reactivate} disabled={!idle}>
              {busy === "state" ? "Reactivating…" : "Reactivate"}
            </button>
          )}
          <button ref={link} type="button" className="btn btn-secondary btn-sm" onClick={sendLink} disabled={!idle}>
            {busy === "link" ? "Sending…" : "Send sign-in link"}
          </button>
          <button
            ref={email}
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => openDialog("email")}
            disabled={!idle}
          >
            Change email
          </button>
          <button
            ref={remove}
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => openDialog("delete")}
            disabled={!idle}
          >
            Delete
          </button>
        </div>
      )}
      <p className={status?.error ? "user-actions-status error" : "user-actions-status"} role="status">
        {status?.text}
      </p>
      {dialog === "delete" && <DeleteUserDialog user={user} onClose={closeDialog} onChanged={dialogChanged} />}
      {dialog === "email" && <ChangeEmailDialog user={user} onClose={closeDialog} onChanged={dialogChanged} />}
    </div>
  );
}
