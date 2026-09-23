import { lazy, Suspense } from "react";
import { Navigate, Routes, Route } from "react-router-dom";
import LoginPage from "./pages/LoginPage";
import AuthLinkPage from "./pages/AuthLinkPage";
import ClaimPage from "./pages/ClaimPage";
import DashboardLayout from "./components/DashboardLayout";
import RequireAuth from "./components/RequireAuth";
import { resolveSurface, warnIfModeIgnored } from "./utils/surface";
import { useAuth } from "./context/AuthContext";

// User pages — lazy-loaded so each chunk is only downloaded when first visited
const OverviewPage = lazy(() => import("./pages/user/OverviewPage"));
const NodeDetailPage = lazy(() => import("./pages/user/NodeDetailPage"));
const DetectionsPage = lazy(() => import("./pages/user/DetectionsPage"));
const ContributionPage = lazy(() => import("./pages/user/ContributionPage"));
const DataExplorerPage = lazy(() => import("./pages/user/DataExplorerPage"));
const RFEnvironmentPage = lazy(() => import("./pages/user/RFEnvironmentPage"));
const AlertsPage = lazy(() => import("./pages/user/AlertsPage"));
const LeaderboardPage = lazy(() => import("./pages/user/LeaderboardPage"));
const KnowledgeBasePage = lazy(() => import("./pages/user/KnowledgeBasePage"));
const TunnelLinkPage = lazy(() => import("./pages/user/TunnelLinkPage"));
const AnomalyPage = lazy(() => import("./pages/user/AnomalyPage"));
const OnboardingPage = lazy(() => import("./pages/user/OnboardingPage"));
// Leaflet and the map tree are ~400 KB, so they load when the map is first
// opened rather than on every console page.
const MapPage = lazy(() => import("./pages/map/MapPage"));
const PhysicsPage = lazy(() => import("./pages/map/PhysicsPage"));

// Toy radar sim, dev- and flag-gated so it does not ship to production. The
// lazy import itself sits behind the flag rather than just the route: an
// unconditional `lazy(() => import(...))` emits the sandbox's chunk into
// every build regardless of which branch ever renders it.
const TEST_RADAR_ENABLED =
  import.meta.env.DEV || import.meta.env.VITE_ENABLE_TEST_RADAR === "1";
const TestRadar = TEST_RADAR_ENABLED ? lazy(() => import("./pages/map/TestRadar")) : null;

// Admin pages — lazy-loaded
const NetworkHealthPage = lazy(() => import("./pages/admin/NetworkHealthPage"));
const NodeManagementPage = lazy(() => import("./pages/admin/NodeManagementPage"));
const AnalyticsPage = lazy(() => import("./pages/admin/AnalyticsPage"));
const EventsPage = lazy(() => import("./pages/admin/EventsPage"));
const StoragePage = lazy(() => import("./pages/admin/StoragePage"));
const CustodyPage = lazy(() => import("./pages/admin/CustodyPage"));
const UserManagementPage = lazy(() => import("./pages/admin/UserManagementPage"));
const ConfigPage = lazy(() => import("./pages/admin/ConfigPage"));
const SystemMetricsPage = lazy(() => import("./pages/admin/SystemMetricsPage"));
const InfrastructurePage = lazy(() => import("./pages/admin/InfrastructurePage"));
const MlatVerificationPage = lazy(() => import("./pages/admin/MlatVerificationPage"));
const ApiDocsPage = lazy(() => import("./pages/admin/ApiDocsPage"));

const { isAdmin: isAdminSite, modeParamIgnored } = resolveSurface(
  window.location.hostname,
  window.location.search
);

// `?mode=admin` used to work on any host, so say why it stopped rather than
// quietly rendering the wrong surface to someone following an old link.
warnIfModeIgnored(modeParamIgnored);

export default function App() {
  const { syntheticFleet } = useAuth();
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
                <Routes>
                  {isAdminSite ? (
                    <>
                      <Route index element={<NetworkHealthPage />} />
                      <Route path="nodes" element={<NodeManagementPage />} />
                      <Route path="nodes/:nodeId" element={<NodeDetailPage />} />
                      <Route path="analytics" element={<AnalyticsPage />} />
                      <Route path="mlat" element={<MlatVerificationPage />} />
                      <Route path="anomalies" element={<AnomalyPage />} />
                      <Route path="events" element={<EventsPage />} />
                      <Route path="storage" element={<StoragePage />} />
                      <Route path="custody" element={<CustodyPage />} />
                      <Route path="users" element={<UserManagementPage />} />
                      <Route path="config" element={<ConfigPage />} />
                      <Route path="system" element={<SystemMetricsPage />} />
                      <Route path="infrastructure" element={<InfrastructurePage />} />
                      <Route path="api-docs" element={<ApiDocsPage />} />
                    </>
                  ) : (
                    <>
                      {/* The map is the front page, at its own address. */}
                      <Route index element={<Navigate to="/map" replace />} />
                      <Route path="overview" element={<OverviewPage />} />
                      <Route path="nodes/:nodeId" element={<NodeDetailPage />} />
                      <Route path="map" element={<MapPage />} />
                      {/* The simulator is a path, not a hostname. /map keeps
                          whatever feed its hostname implies — real nodes on
                          app and test-app — while /sim asks for the synthetic
                          fleet by name, so one console serves both and neither
                          depends on what the browser's address bar says. */}
                      <Route path="sim" element={<MapPage feed="synthetic" />} />
                      {/* Open like /sim, session or not: the page draws the
                          fleet's solver internals and saves its configuration,
                          and both ends of that are public on a server that
                          has a fleet. Absent where there is none. */}
                      {syntheticFleet && (
                        <Route path="sim/physics" element={<PhysicsPage />} />
                      )}
                      {TestRadar && <Route path="test-radar" element={<TestRadar />} />}
                      <Route path="detections" element={<DetectionsPage />} />
                      <Route path="rf" element={<RFEnvironmentPage />} />
                      <Route path="contribution" element={<ContributionPage />} />
                      <Route path="data" element={<DataExplorerPage />} />
                      <Route path="alerts" element={<AlertsPage />} />
                      <Route path="anomalies" element={<AnomalyPage />} />
                      <Route path="leaderboard" element={<LeaderboardPage />} />
                      <Route path="knowledge" element={<KnowledgeBasePage />} />
                      <Route path="tunnel" element={<TunnelLinkPage />} />
                      <Route path="onboarding" element={<OnboardingPage />} />
                    </>
                  )}
                </Routes>
              </Suspense>
            </DashboardLayout>
          </RequireAuth>
        }
      />
    </Routes>
  );
}
