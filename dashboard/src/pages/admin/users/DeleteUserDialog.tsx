import { useId, useState } from "react";
import { HttpError } from "@retina/shared";

import { api } from "../../../api/client";
import { Dialog } from "../../../components/Dialog";
import { Notice } from "../../../components/Notice";
import type { AdminUser } from "../../../types";
import { messageOf, sentence, type UserChange } from "./userChange";

interface Props {
  user: AdminUser;
  onClose: () => void;
  /** `deleted`, or `gone` when the server no longer knows the account. */
  onChanged: (change: UserChange) => void;
}

/** What deleting does to the nodes the account owns: each is released, but
 *  a polled radar they connected is removed. */
function releasedNodes(count: number): string {
  if (count === 0) return "They own no nodes.";
  return count === 1
    ? "Their 1 node is released, or removed if it is a polled radar they connected."
    : `Their ${count} nodes are released; any polled radar they connected is removed.`;
}

/** Deleting an account, which cannot be undone, so it waits for the address
 *  to be typed out. Open while mounted. */
export function DeleteUserDialog({ user, onClose, onChanged }: Props) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const fieldId = useId();
  const matches = typed.trim() === user.email;

  async function remove() {
    if (!matches || busy) return;
    setBusy(true);
    setError(null);
    try {
      const { released } = await api.adminDeleteUser(user.id);
      onChanged({ kind: "deleted", released: typeof released === "number" ? released : 0 });
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) {
        onChanged({ kind: "gone" });
        return;
      }
      setError(sentence(`Not deleted: ${messageOf(e)}`));
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      title="Delete this account?"
      description={
        <>
          <strong>{user.email}</strong> is removed for good. {releasedNodes(user.node_count)} Every session
          they hold ends.
        </>
      }
      busy={busy}
      onClose={onClose}
      onSubmit={remove}
      actions={
        <button type="submit" className="btn btn-danger" disabled={!matches || busy}>
          {busy ? "Deleting…" : "Delete account"}
        </button>
      }
    >
      <div className="dialog-field">
        <label htmlFor={fieldId}>
          Type <strong>{user.email}</strong> to confirm
        </label>
        <input
          id={fieldId}
          type="text"
          className="input"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
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
