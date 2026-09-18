// @ts-nocheck — gradual TS migration

/* ── NodeOwnerControl: top-right map overlay for the node-owner view.
      Shown only to a signed-in owner: their name plus a "My nodes only" toggle
      that filters the map to a server-authenticated feed of just their own
      nodes. ── */

export default function NodeOwnerControl({ user, ownedCount, ownerOnly, onToggle, loading }) {
  if (loading) return null;
  // Nothing is offered to a signed-out visitor. The map hostnames carry no Access
  // application and magic links are unbuilt, so every provider this could name
  // fails at the provider. Restore a sign-in here alongside a login method that
  // works, from the signal ClickUp 123zgec2ryj adds.
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
          No nodes linked to your account yet. Claim a node from your dashboard.
        </div>
      )}
    </div>
  );
}
