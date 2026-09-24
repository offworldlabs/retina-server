import { useState } from "react";
import { matchPath, useLocation } from "react-router-dom";
import Sidebar from "./Sidebar";
import Header from "./Header";
import ErrorBoundary from "./ErrorBoundary";
import { readStored, writeStored } from "../utils/storage";

const SIDEBAR_KEY = "retina.sidebarCollapsed";

/** The stored choice, or null when the user has never made one. */
function storedCollapsed(): boolean | null {
  const raw = readStored(SIDEBAR_KEY);
  return raw === null ? null : raw === "true";
}

export const pageTitles: Record<string, { user?: string; admin?: string }> = {
  // The user surface's index only forwards to the map, so it borrows its title.
  "/": { user: "Live Map", admin: "Network Health" },
  "/overview": { user: "Overview" },
  "/map": { user: "Live Map" },
  "/sim": { admin: "Simulation Map" },
  // Two segments, so this table is looked up by the whole path before it falls
  // back to the first one: /sim/physics is its own page, not a view of /sim.
  "/sim/physics": { admin: "Physics Layer" },
  // Dev builds only.
  "/test-radar": { user: "Test Radar" },
  "/detections": { user: "Detections" },
  "/rf": { user: "RF Environment" },
  "/contribution": { user: "Network Contribution" },
  "/data": { user: "Data Explorer" },
  "/anomalies": { user: "Anomaly Monitor", admin: "Anomaly Monitor" },
  "/leaderboard": { user: "Leaderboard" },
  "/knowledge": { user: "Knowledge Base" },
  "/tunnel": { user: "Tunnel & Local Display" },
  "/onboarding": { user: "My Nodes" },
  "/radars/new": { user: "Add a Radar" },
  "/nodes": { admin: "Node Management" },
  // A pattern, because the first segment alone would name the admin list.
  "/nodes/:nodeId": { user: "Node Detail", admin: "Node Detail" },
  "/mlat": { admin: "MLAT Verification" },
  "/analytics": { admin: "Analytics" },
  "/events": { admin: "Events & Alerts" },
  "/storage": { admin: "Data & Storage" },
  "/system": { admin: "System Metrics" },
  "/custody": { admin: "Chain of Custody" },
  "/users": { admin: "User Management" },
  "/config": { admin: "Configuration" },
  "/infrastructure": { admin: "Infrastructure" },
  "/api-docs": { admin: "API Reference" },
};

export default function DashboardLayout({ isAdmin, children }) {
  const { pathname } = useLocation();
  const mode = isAdmin ? "admin" : "user";
  const segments = pathname.split("/").filter(Boolean);
  const fullPath = segments.length ? `/${segments.join("/")}` : "/";
  const basePath = segments.length ? `/${segments[0]}` : "/";
  // An exact match first, patterns included, and the first segment second, so
  // a page owns whatever nests under it unless the table names the nested
  // path itself: /sim/physics is a different page from /sim, and a node's own
  // page is not the admin node list.
  const entry =
    Object.entries(pageTitles).find(([pattern]) => matchPath(pattern, pathname))?.[1] ??
    pageTitles[basePath];
  const title = entry?.[mode] || "Dashboard";

  const [stored, setStored] = useState(storedCollapsed);
  // The map wants the canvas; every other page wants the labels. An explicit
  // choice outranks both.
  const collapsed = stored ?? (basePath === "/map" || fullPath === "/sim");
  // The map and the physics layer draw to the edges and scroll nothing: their
  // own panels own their overflow, and a scrollbar on the pane would move the
  // canvas under them. `/sim` by first segment covers both the sim map and the
  // physics page under it.
  const flush = basePath === "/map" || basePath === "/sim";

  const toggle = () => {
    const next = !collapsed;
    setStored(next);
    writeStored(SIDEBAR_KEY, String(next));
  };

  return (
    <div className={`dashboard${collapsed ? " sidebar-collapsed" : ""}`}>
      <Sidebar isAdmin={isAdmin} collapsed={collapsed} onToggle={toggle} />
      <div className="main-area">
        <Header title={title} isAdmin={isAdmin} />
        <div className={`content${flush ? " flush" : ""}`}>
          {/* Keyed on the path, which names the page: the query string and hash
              are state within it. */}
          <ErrorBoundary
            resetKey={pathname}
            title="Something went wrong on this page"
            message="The rest of the console still works. Try the page again, or choose another from the sidebar."
          >
            {children}
          </ErrorBoundary>
        </div>
      </div>
    </div>
  );
}
