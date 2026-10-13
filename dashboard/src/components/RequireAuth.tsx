import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useAuth } from "../context/AuthContext";
import { isPublicRoute } from "../utils/publicRoutes";
import { appConsoleUrl } from "../utils/siblings";
import DashboardLayout from "./DashboardLayout";

type RequireAuthProps = {
  isAdmin: boolean;
  children: ReactNode;
};

/**
 * What stands between a caller and a page.
 *
 * Three answers: the open routes render to anyone, the rest want a session,
 * and the admin console wants that session to belong to an administrator.
 */
export default function RequireAuth({ isAdmin, children }: RequireAuthProps) {
  const { user, loading } = useAuth();
  // Router space, so the mount prefix is already off.
  const { pathname } = useLocation();

  // Settled first even on an open route: the chrome around it is drawn from
  // the identity, and rendering the signed-out version of it to someone whose
  // session is one round trip away is a flash of the wrong page.
  if (loading) return <div className="loading-screen">Loading…</div>;
  if (!user) {
    if (isPublicRoute(pathname, isAdmin)) return children;
    // Carrying the page, so signing in ends on it. Not from the admin console:
    // the mailed link opens on the app host, which has none of its routes.
    return <Navigate to="/login" replace state={isAdmin ? null : { next: pathname }} />;
  }
  if (isAdmin && user.role !== "admin") {
    // In the console's own chrome, so the caller keeps the header and its Sign
    // out, and is shown the way on rather than a dead end. None of the
    // console's pages would open for them, so the sidebar lists none.
    return (
      <DashboardLayout isAdmin={isAdmin} title="Access Denied" nav={false}>
        <AccessDenied email={user.email} />
      </DashboardLayout>
    );
  }
  return children;
}

/** The admin console's answer to a caller who is signed in but is not an
 *  administrator: which account they hold, and the two ways on from here. */
function AccessDenied({ email }: { email: string }) {
  // Pending from the provider's own flag, as the header's account menu is, so
  // a sign-out that menu started shows here too.
  const { logout, signingOut } = useAuth();

  // No `leave`: once the session is gone the guard sends the caller to this
  // console's sign-in card, where an administrator's account gets in. A request
  // that fails is the provider's to report, and it clears the identity either
  // way.
  const signOut = () => {
    if (!signingOut) logout();
  };

  return (
    <>
      <div className="page-header">
        <h1>Access Denied</h1>
        <p>The admin console is for network administrators.</p>
      </div>
      <div className="card access-denied-card">
        <div className="card-body stack">
          <p>
            You are signed in as <strong>{email}</strong>.
          </p>
          <div className="btn-row">
            <a className="btn btn-primary" href={appConsoleUrl(window.location.host, window.location.protocol)}>
              Go to the Retina console
            </a>
            <button type="button" className="btn btn-secondary" onClick={signOut} disabled={signingOut}>
              {signingOut ? "Signing out…" : "Sign out"}
            </button>
          </div>
        </div>
      </div>
    </>
  );
}
