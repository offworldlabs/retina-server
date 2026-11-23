/**
 * Every page the console routes, per surface, with the name its header gives
 * it.
 *
 * App renders its routes from this table and DashboardLayout reads its titles
 * off the same one, so a page is routed and named in a single edit. A page
 * that is not routed here, such as the simulator on a server with no fleet or
 * the radar sandbox in a production build, has no entry, and so no title
 * either.
 */
import { lazy, useMemo, type ReactElement } from "react";
import { matchRoutes } from "react-router-dom";
import { useAuth } from "./context/AuthContext";

// User pages, lazy-loaded so each chunk is only downloaded when first visited.
const OverviewPage = lazy(() => import("./pages/user/OverviewPage"));
const NodeDetailPage = lazy(() => import("./pages/user/NodeDetailPage"));
const DetectionsPage = lazy(() => import("./pages/user/DetectionsPage"));
const ContributionPage = lazy(() => import("./pages/user/ContributionPage"));
const DataExplorerPage = lazy(() => import("./pages/user/DataExplorerPage"));
const RFEnvironmentPage = lazy(() => import("./pages/user/RFEnvironmentPage"));
const LeaderboardPage = lazy(() => import("./pages/user/LeaderboardPage"));
const KnowledgeBasePage = lazy(() => import("./pages/user/KnowledgeBasePage"));
const TunnelLinkPage = lazy(() => import("./pages/user/TunnelLinkPage"));
const AnomalyPage = lazy(() => import("./pages/user/AnomalyPage"));
const RegisterRadarPage = lazy(() => import("./pages/user/RegisterRadarPage"));
const RadarPage = lazy(() => import("./pages/user/RadarPage"));
const AccountPage = lazy(() => import("./pages/user/AccountPage"));
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

// Admin pages, lazy-loaded.
const NetworkHealthPage = lazy(() => import("./pages/admin/NetworkHealthPage"));
const NodeManagementPage = lazy(() => import("./pages/admin/NodeManagementPage"));
const AnalyticsPage = lazy(() => import("./pages/admin/AnalyticsPage"));
const EventsPage = lazy(() => import("./pages/admin/EventsPage"));
const StoragePage = lazy(() => import("./pages/admin/StoragePage"));
const CustodyPage = lazy(() => import("./pages/admin/CustodyPage"));
const UserManagementPage = lazy(() => import("./pages/admin/UserManagementPage"));
const UserDetailPage = lazy(() => import("./pages/admin/UserDetailPage"));
const ConfigPage = lazy(() => import("./pages/admin/ConfigPage"));
const SystemMetricsPage = lazy(() => import("./pages/admin/SystemMetricsPage"));
const InfrastructurePage = lazy(() => import("./pages/admin/InfrastructurePage"));
const MlatVerificationPage = lazy(() => import("./pages/admin/MlatVerificationPage"));
const ApiDocsPage = lazy(() => import("./pages/admin/ApiDocsPage"));

/** A page, and what the header calls it. */
export interface PageRoute {
  /** Absolute, as the address bar reads it. "/" is the surface's index. */
  path: string;
  title: string;
  element: ReactElement;
}

/** An address that forwards to a page. It has no title of its own: for the
 *  moment it is on screen, it borrows the title of the page it lands on. */
export interface RedirectRoute {
  path: string;
  to: string;
}

export type ConsoleRoute = PageRoute | RedirectRoute;

/** What the server reports that decides which pages exist. */
export interface RouteConditions {
  /** Whether it runs a synthetic fleet, for the simulator to show. */
  syntheticFleet: boolean;
}

/** `routes` where `condition` holds, and no entries at all where it does not. */
const when = (condition: boolean, routes: ConsoleRoute[]): ConsoleRoute[] => (condition ? routes : []);

const userRoutes = (): ConsoleRoute[] => [
  // The map is the front page, at its own address.
  { path: "/", to: "/map" },
  { path: "/overview", title: "My Nodes", element: <OverviewPage /> },
  { path: "/nodes/:nodeId", title: "Node Detail", element: <NodeDetailPage /> },
  { path: "/map", title: "Live Map", element: <MapPage /> },
  ...(TestRadar ? [{ path: "/test-radar", title: "Test Radar", element: <TestRadar /> }] : []),
  { path: "/detections", title: "Detections", element: <DetectionsPage /> },
  { path: "/rf", title: "RF Environment", element: <RFEnvironmentPage /> },
  { path: "/contribution", title: "Contribution", element: <ContributionPage /> },
  { path: "/data", title: "Data Explorer", element: <DataExplorerPage /> },
  { path: "/anomalies", title: "Anomaly Monitor", element: <AnomalyPage /> },
  { path: "/leaderboard", title: "Leaderboard", element: <LeaderboardPage /> },
  { path: "/knowledge", title: "Knowledge Base", element: <KnowledgeBasePage /> },
  { path: "/tunnel", title: "Tunnel Link", element: <TunnelLinkPage /> },
  // Still in bookmarks, and in sign-in links already mailed.
  { path: "/onboarding", to: "/overview" },
  { path: "/radars/new", title: "Add a Radar", element: <RegisterRadarPage /> },
  { path: "/radars/:nodeRef", title: "Your Radar", element: <RadarPage /> },
  // Reached from the header's account menu rather than the sidebar.
  { path: "/account", title: "Account", element: <AccountPage /> },
];

const adminRoutes = (syntheticFleet: boolean): ConsoleRoute[] => [
  { path: "/", title: "Network Health", element: <NetworkHealthPage /> },
  { path: "/nodes", title: "Node Management", element: <NodeManagementPage /> },
  // A pattern of its own, because the first segment alone would name the list.
  { path: "/nodes/:nodeId", title: "Node Detail", element: <NodeDetailPage /> },
  { path: "/analytics", title: "Analytics", element: <AnalyticsPage /> },
  { path: "/mlat", title: "MLAT Verification", element: <MlatVerificationPage /> },
  // The synthetic fleet and the page that tunes it, only where the server runs
  // one. No owner view: the Access identity carries no session cookie, which
  // /ws/aircraft/owner authenticates with, so its toggle would blank the map.
  ...when(syntheticFleet, [
    { path: "/sim", title: "Simulation Map", element: <MapPage feed="synthetic" ownerView={false} /> },
    // Its own page rather than a view of the map, so it is named apart from it.
    { path: "/sim/physics", title: "Physics Layer", element: <PhysicsPage /> },
  ]),
  { path: "/anomalies", title: "Anomaly Monitor", element: <AnomalyPage /> },
  { path: "/events", title: "Events & Alerts", element: <EventsPage /> },
  { path: "/storage", title: "Data & Storage", element: <StoragePage /> },
  { path: "/custody", title: "Chain of Custody", element: <CustodyPage /> },
  { path: "/users", title: "User Management", element: <UserManagementPage /> },
  { path: "/users/:userId", title: "User Detail", element: <UserDetailPage /> },
  { path: "/config", title: "Configuration", element: <ConfigPage /> },
  { path: "/system", title: "System Metrics", element: <SystemMetricsPage /> },
  { path: "/infrastructure", title: "Infrastructure", element: <InfrastructurePage /> },
  { path: "/api-docs", title: "API Reference", element: <ApiDocsPage /> },
];

/** Every page and forward on one surface, under the server's conditions. */
export function consoleRoutes(isAdmin: boolean, { syntheticFleet }: RouteConditions): ConsoleRoute[] {
  return isAdmin ? adminRoutes(syntheticFleet) : userRoutes();
}

/** This surface's table, under the conditions the signed-in session reports.
 *  App and DashboardLayout both read it through here, so the router and the
 *  header cannot disagree about which pages exist. */
export function useConsoleRoutes(isAdmin: boolean): ConsoleRoute[] {
  const syntheticFleet = Boolean(useAuth().syntheticFleet);
  return useMemo(() => consoleRoutes(isAdmin, { syntheticFleet }), [isAdmin, syntheticFleet]);
}

/**
 * What the header calls the page at `pathname`.
 *
 * The whole path first, matched as the router matches it, patterns and their
 * ranking included: /sim/physics is a page of its own rather than a view of
 * /sim, a node's page is not the admin node list, and /radars/new is not a
 * radar called "new". Then the first segment, so a page owns whatever nests
 * under it: /data/2026/09/17 is the Data Explorer's own deep link.
 */
export function pageTitle(pathname: string, routes: ConsoleRoute[]): string {
  const first = `/${pathname.split("/").filter(Boolean)[0] ?? ""}`;
  const entry = matchRoutes(routes, pathname)?.[0].route ?? routes.find((route) => route.path === first);
  const page = entry && "to" in entry ? routes.find((route) => route.path === entry.to) : entry;
  return page && "title" in page ? page.title : "Dashboard";
}
