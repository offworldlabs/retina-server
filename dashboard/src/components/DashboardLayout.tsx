import { useState } from "react";
import { useLocation } from "react-router-dom";
import Sidebar from "./Sidebar";
import Header from "./Header";

const SIDEBAR_KEY = "retina.sidebarCollapsed";

/** The stored choice, or null when the user has never made one. Storage throws
 *  in private mode, and a console that will not render is worse than one whose
 *  sidebar forgets. */
function storedCollapsed(): boolean | null {
  try {
    const raw = window.localStorage.getItem(SIDEBAR_KEY);
    return raw === null ? null : raw === "true";
  } catch {
    return null;
  }
}

const pageTitles = {
  // The user surface's index only forwards to the map, so it borrows its title.
  "/": { user: "Live Map", admin: "Network Health" },
  "/overview": { user: "Overview" },
  "/map": { user: "Live Map" },
  "/sim": { user: "Simulation Map" },
  // Two segments, so this table is looked up by the whole path before it falls
  // back to the first one: /sim/physics is its own page, not a view of /sim.
  "/sim/physics": { user: "Physics Layer" },
  // The old address, which only forwards to /sim/physics. Kept so the hop does
  // not flash a header that says "Dashboard" on its way there.
  "/physics": { user: "Physics Layer" },
  "/detections": { user: "Detections" },
  "/rf": { user: "RF Environment" },
  "/contribution": { user: "Network Contribution" },
  "/data": { user: "Data Explorer" },
  "/alerts": { user: "Alerts & Notifications" },
  "/anomalies": { user: "Anomaly Monitor", admin: "Anomaly Monitor" },
  "/leaderboard": { user: "Leaderboard" },
  "/knowledge": { user: "Knowledge Base" },
  "/tunnel": { user: "Tunnel & Local Display" },
  "/onboarding": { user: "My Nodes" },
  // Forwards to /onboarding; named for the same reason as /physics above.
  "/settings": { user: "My Nodes" },
  "/nodes": { admin: "Node Management" },
  "/analytics": { admin: "Analytics" },
  "/events": { admin: "Events & Alerts" },
  "/storage": { admin: "Data & Storage" },
  "/system": { admin: "System Metrics" },
  "/custody": { admin: "Chain of Custody" },
  "/users": { admin: "User Management" },
  "/config": { admin: "Configuration" },
};

export default function DashboardLayout({ isAdmin, children }) {
  const { pathname } = useLocation();
  const mode = isAdmin ? "admin" : "user";
  const segments = pathname.split("/").filter(Boolean);
  const fullPath = segments.length ? `/${segments.join("/")}` : "/";
  const basePath = segments.length ? `/${segments[0]}` : "/";
  // Whole path first, first segment second. Almost every page is named by its
  // first segment and owns whatever nests under it (/nodes/:nodeId is "Node
  // Detail"); /sim is the one that does not, because the page nested under it
  // is a different page rather than a detail view of the same one.
  const entry = pageTitles[fullPath] ?? pageTitles[basePath];
  const title = entry?.[mode] || (pathname.includes("/nodes/") ? "Node Detail" : "Dashboard");

  const [stored, setStored] = useState(storedCollapsed);
  // The map wants the canvas; every other page wants the labels. An explicit
  // choice outranks both.
  const collapsed = stored ?? (basePath === "/map" || fullPath === "/sim");
  // The map and the physics layer draw to the edges and scroll nothing: their
  // own panels own their overflow, and a scrollbar on the pane would move the
  // canvas under them. `/sim` by first segment covers both the sim map and the
  // physics page now living under it.
  const flush = basePath === "/map" || basePath === "/sim" || basePath === "/physics";

  const toggle = () => {
    const next = !collapsed;
    setStored(next);
    try {
      window.localStorage.setItem(SIDEBAR_KEY, String(next));
    } catch {
      /* private mode: the choice still holds for this tab */
    }
  };

  return (
    <div className={`dashboard${collapsed ? " sidebar-collapsed" : ""}`}>
      <Sidebar isAdmin={isAdmin} collapsed={collapsed} onToggle={toggle} />
      <div className="main-area">
        <Header title={title} />
        <div className={`content${flush ? " flush" : ""}`}>{children}</div>
      </div>
    </div>
  );
}
