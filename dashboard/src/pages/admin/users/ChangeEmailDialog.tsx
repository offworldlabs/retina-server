import { useId, useState } from "react";
import { HttpError } from "@retina/shared";

import { api } from "../../../api/client";
import { Dialog } from "../../../components/Dialog";
import { Notice } from "../../../components/Notice";
import type { AdminUser } from "../../../types";
import { messageOf, sentence, type UserChange } from "./userChange";

/** Enough of an address to be worth sending; the server has the last word. */
const ADDRESS = /^[^\s@]+@[^\s@]+$/;

interface Props {
  user: AdminUser;
  onClose: () => void;
  /** `email-sent` with the new address as the server normalised it, or
   *  `gone` when the server no longer knows the account. */
  onChanged: (change: UserChange) => void;
}

/** Moving an account to another address, for an owner who has lost theirs.
 *  A link goes to the new address and the address changes when it is opened,
 *  the same change the owner can ask for. Open while mounted. */
export function ChangeEmailDialog({ user, onClose, onChanged }: Props) {
  const [email, setEmail] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fieldId = useId();
  const next = email.trim();
  const ready = ADDRESS.test(next) && next.toLowerCase() !== user.email.toLowerCase();

  async function send() {
    if (!ready || busy) return;
    setBusy(true);
    setError(null);
    try {
      const sent = await api.adminChangeUserEmail(user.id, next);
      onChanged({ kind: "email-sent", email: sent?.email || next });
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) {
        onChanged({ kind: "gone" });
        return;
      }
      // The server's refusals say why in words: taken, not an address, too
      // many asked for, or no mail can leave.
      setError(sentence(`Not sent: ${messageOf(e)}`));
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      title="Change email address"
      description={
        <>
          A confirmation link goes to the new address. Until it is opened, the account keeps signing in at{" "}
          <strong>{user.email}</strong>.
        </>
      }
      busy={busy}
      onClose={onClose}
      onSubmit={send}
      actions={
        <button type="submit" className="btn btn-primary" disabled={!ready || busy}>
          {busy ? "Sending…" : "Send link"}
        </button>
      }
    >
      <div className="dialog-field">
        <label htmlFor={fieldId}>New address</label>
        <input
          id={fieldId}
          type="email"
          className="input"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          readOnly={busy}
          autoComplete="off"
          autoCapitalize="none"
          spellCheck={false}
        />
      </div>
      {error && <Notice tone="error">{error}</Notice>}
    </Dialog>
  );
}
