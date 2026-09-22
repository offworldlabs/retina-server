import { Navigate, useLocation } from "react-router-dom";

import { useAuth } from "../context/AuthContext";
import { isPublicRoute } from "../utils/publicRoutes";

/**
 * What stands between a caller and a page.
 *
 * Three answers: the open routes render to anyone, the rest want a session,
 * and the admin console wants that session to belong to an administrator.
 */
export default function RequireAuth({ isAdmin, children }) {
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
    return (
      <div className="access-denied">
        <h2>Access Denied</h2>
        <p>Admin privileges required.</p>
      </div>
    );
  }
  return children;
}
