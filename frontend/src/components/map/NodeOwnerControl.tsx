// @ts-nocheck — gradual TS migration
import { useState } from "react";
import { API_BASE } from "./constants";

/* ── NodeOwnerControl: top-right map overlay for the node-owner view.
      - Logged out: a "Sign in" button that reveals Google/GitHub OAuth links
        (redirecting back to the current map URL).
      - Logged in: the user's name plus a "My nodes only" toggle that filters
        the map to a server-authenticated feed of just their own nodes. ── */

export default function NodeOwnerControl({ user, ownedCount, ownerOnly, onToggle, loading }) {
  const [menuOpen, setMenuOpen] = useState(false);

  if (loading) return null;

  const redirect = encodeURIComponent(window.location.href);

  if (!user) {
    return (
      <div className="owner-panel">
        <button
          className="owner-signin"
          onClick={() => setMenuOpen((v) => !v)}
          aria-expanded={menuOpen}
        >
          Sign in to see your node
        </button>
        {menuOpen && (
          <>
            <a className="owner-provider" href={`${API_BASE}/auth/login/google?redirect=${redirect}`}>
              Continue with Google
            </a>
            <a className="owner-provider" href={`${API_BASE}/auth/login/github?redirect=${redirect}`}>
              Continue with GitHub
            </a>
          </>
        )}
      </div>
    );
  }

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
