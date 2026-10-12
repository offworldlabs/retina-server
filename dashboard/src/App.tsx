import { Suspense } from "react";
import { Navigate, Routes, Route } from "react-router-dom";
import LoginPage from "./pages/LoginPage";
import AuthLinkPage from "./pages/AuthLinkPage";
import ClaimPage from "./pages/ClaimPage";
import DashboardLayout from "./components/DashboardLayout";
import RequireAuth from "./components/RequireAuth";
import { useConsoleRoutes, type ConsoleRoute } from "./routes";
import { resolveSurface, warnIfModeIgnored } from "./utils/surface";

const { isAdmin: isAdminSite, modeParamIgnored } = resolveSurface(
  window.location.hostname,
  window.location.search
);

// Old links and habits still carry `?mode=admin`, which selects nothing, so say
// why rather than quietly rendering the other surface.
warnIfModeIgnored(modeParamIgnored);

/** One entry of the table as a route nested under the console's "/*". The
 *  table's paths are absolute, as the address bar and the header read them. */
function toRoute(route: ConsoleRoute) {
  const element = "to" in route ? <Navigate to={route.to} replace /> : route.element;
  return route.path === "/" ? (
    <Route key={route.path} index element={element} />
  ) : (
    <Route key={route.path} path={route.path.slice(1)} element={element} />
  );
}

export default function App() {
  const routes = useConsoleRoutes(isAdminSite);
  return (
    <Routes>
      <Route path="/login" element={<LoginPage isAdmin={isAdminSite} />} />
      {/* Both outside RequireAuth: whoever opens either link has no session
          yet, and the guard would send them to the login card instead. The
          claim link goes further and creates the account on the click. */}
      <Route path="/auth/link/:token" element={<AuthLinkPage isAdmin={isAdminSite} />} />
      <Route path="/auth/claim/:token" element={<ClaimPage />} />
      <Route
        path="/*"
        element={
          <RequireAuth isAdmin={isAdminSite}>
            <DashboardLayout isAdmin={isAdminSite}>
              <Suspense fallback={<div className="loading-screen">Loading…</div>}>
                <Routes>{routes.map(toRoute)}</Routes>
              </Suspense>
            </DashboardLayout>
          </RequireAuth>
        }
      />
    </Routes>
  );
}
