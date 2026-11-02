import { useId, useState } from "react";
import { HttpError } from "@retina/shared";

import { api } from "../../../api/client";
import { Dialog } from "../../../components/Dialog";
import { Notice } from "../../../components/Notice";
import { messageOf, sentence } from "./userChange";

const two = (n: number) => String(n).padStart(2, "0");

/** Today in the browser's own calendar, as a date input writes it. */
function localToday(): string {
  const now = new Date();
  return `${now.getFullYear()}-${two(now.getMonth() + 1)}-${two(now.getDate())}`;
}

/** Midnight after the picked day, in the browser's own time, as an instant. */
function endOf(day: string): string {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(y, m - 1, d + 1).toISOString();
}

interface Props {
  userId: string;
  onClose: () => void;
  onGranted: () => void;
}

/** Opening customer access for an account, with or without a last day. Open
 *  while mounted. */
export function GrantAccessDialog({ userId, onClose, onGranted }: Props) {
  const fieldId = useId();
  const [day, setDay] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function grant() {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await api.adminGrantApiAccess(userId, day ? endOf(day) : null);
      onGranted();
      onClose();
    } catch (e) {
      setError(e instanceof HttpError && e.status === 422 ? "Pick a day from today on." : sentence(messageOf(e)));
      setBusy(false);
    }
  }

  return (
    <Dialog
      open
      title="Grant API access"
      description="The account gets customer access to the data API and can create keys."
      busy={busy}
      onClose={onClose}
      onSubmit={() => void grant()}
      actions={
        <button type="submit" className="btn btn-primary" disabled={busy}>
          {busy ? "Granting…" : "Grant access"}
        </button>
      }
    >
      <div className="dialog-field">
        <label htmlFor={fieldId}>Last day of access (optional)</label>
        <input id={fieldId} type="date" className="input" value={day} min={localToday()} readOnly={busy} onChange={(e) => setDay(e.target.value)} />
        <p className="api-access-note">Leave empty for access with no end date. Access ends at midnight after this day, your time.</p>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
    </Dialog>
  );
}
