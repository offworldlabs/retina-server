import type { CurrentUser } from "@retina/shared";

import { CLAIM_PATH } from "../../components/ClaimPath";

/* ── NodeOwnerControl: renders in the aircraft list's header, for the
      node-owner view. Shown only to a signed-in owner: their name plus a
      "My nodes only" toggle that filters the map to a server-authenticated
      feed of just their own nodes. With none on the account, it says how a
      node joins one. ── */

interface NodeOwnerControlProps {
  user: Pick<CurrentUser, "name" | "email"> | null;
  ownedCount: number;
  ownerOnly: boolean;
  onToggle: (ownerOnly: boolean) => void;
  loading: boolean;
}

export default function NodeOwnerControl({ user, ownedCount, ownerOnly, onToggle, loading }: NodeOwnerControlProps) {
  if (loading) return null;
  // Nothing is offered to a signed-out visitor; the console's header carries sign-in.
  if (!user) return null;

  return (
    <div className="card owner-panel">
      <div className="owner-name">{user.name || user.email}</div>
      <label>
        <input type="checkbox" checked={ownerOnly} onChange={(e) => onToggle(e.target.checked)} />
        My nodes only
      </label>
      {ownerOnly && ownedCount === 0 && (
        <div className="owner-warning">No nodes on your account yet. {CLAIM_PATH}</div>
      )}
    </div>
  );
}
