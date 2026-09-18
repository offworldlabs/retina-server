// @ts-nocheck — gradual TS migration

/* ── NodeOwnerControl: renders in the aircraft list's header, for the
      node-owner view. Shown only to a signed-in owner: their name plus a
      "My nodes only" toggle that filters the map to a server-authenticated
      feed of just their own nodes. ── */

export default function NodeOwnerControl({ user, ownedCount, ownerOnly, onToggle, loading }) {
  if (loading) return null;
  // Nothing is offered to a signed-out visitor; the console's header carries sign-in.
  if (!user) return null;

  return (
    <div className="owner-panel">
      <div className="owner-name">{user.name || user.email}</div>
      <label>
        <input type="checkbox" checked={ownerOnly} onChange={(e) => onToggle(e.target.checked)} />
        My nodes only
      </label>
      {ownerOnly && ownedCount === 0 && (
        <div className="owner-warning">
          No nodes linked to your account yet. Claim a node from My Nodes.
        </div>
      )}
    </div>
  );
}
