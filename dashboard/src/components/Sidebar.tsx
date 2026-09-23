import { Link, NavLink } from "react-router-dom";
import { towerFinderUrl } from "../utils/siblings";
import { useAuth } from "../context/AuthContext";
import { isPublicRoute } from "../utils/publicRoutes";
import { externalLinkIcon } from "./RetnodeLink";

type NavItem = {
  label: string;
  icon: string;
  to?: string; // internal route (react-router)
  href?: string; // external URL, opens in a new tab
  external?: boolean;
  /** Highlight on this exact path only, not on the routes nested under it.
   *  NavLink's default is to light up for a whole subtree, which reads as two
   *  entries being current at once where one nests inside the other. */
  end?: boolean;
};

type NavSection = { title: string; items: NavItem[] };

// Built per render rather than at module scope. The links below read
// window.location, and an import-time read that threw would take down every
// module that transitively imports this one, not just the sidebar.
const userNav = (syntheticFleet: boolean): NavSection[] => [
  {
    title: "Dashboard",
    items: [
      { to: "/overview", label: "Overview", icon: "home" },
      { to: "/detections", label: "Detections", icon: "radar" },
      { to: "/rf", label: "RF Environment", icon: "activity" },
      { to: "/contribution", label: "Network", icon: "globe" },
      { to: "/anomalies", label: "Anomalies", icon: "alertTriangle" },
      { to: "/map", label: "Map", icon: "map" },
      // The simulator and the page that tunes it, together and in that order,
      // because the second is a setting of the first. Both exist only where
      // the server runs a fleet; on a fleetless deployment /sim would be an
      // empty map and the physics form would have nothing to configure.
      ...(syntheticFleet
        ? [
            { to: "/sim", label: "Simulation", icon: "target", end: true },
            { to: "/sim/physics", label: "Physics Layer", icon: "layers" },
          ]
        : []),
      { href: towerFinderUrl(location.host, location.protocol), label: "Tower Finder", icon: "radio", external: true },
    ],
  },
  {
    title: "Data",
    items: [
      { to: "/data", label: "Data Explorer", icon: "database" },
      { to: "/tunnel", label: "Tunnel Link", icon: "link" },
    ],
  },
  {
    title: "Community",
    items: [
      { to: "/leaderboard", label: "Leaderboard", icon: "trophy" },
      { to: "/knowledge", label: "Knowledge Base", icon: "book" },
    ],
  },
  {
    title: "Account",
    items: [
      { to: "/onboarding", label: "My Nodes", icon: "server" },
    ],
  },
];

const adminNav: NavSection[] = [
  {
    title: "Monitoring",
    items: [
      { to: "/", label: "Network Health", icon: "activity" },
      { to: "/nodes", label: "Nodes", icon: "server" },
      { to: "/analytics", label: "Analytics", icon: "chart" },
      { to: "/mlat", label: "MLAT Verification", icon: "target" },
      { to: "/anomalies", label: "Anomalies", icon: "alertTriangle" },
    ],
  },
  {
    title: "Operations",
    items: [
      { to: "/events", label: "Events", icon: "bell" },
      { to: "/storage", label: "Storage", icon: "harddrive" },
      { to: "/system", label: "System Metrics", icon: "cpu" },
      { to: "/infrastructure", label: "Infrastructure", icon: "server" },
      { to: "/custody", label: "Chain of Custody", icon: "shield" },
      { to: "/config", label: "Configuration", icon: "sliders" },
      { to: "/api-docs", label: "API Reference", icon: "book" },
    ],
  },
  {
    title: "Management",
    items: [
      { to: "/users", label: "Users", icon: "users" },
    ],
  },
];

const icons = {
  home: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 9l9-7 9 7v11a2 2 0 01-2 2H5a2 2 0 01-2-2V9z" />
      <polyline points="9 22 9 12 15 12 15 22" />
    </svg>
  ),
  radar: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <path d="M12 12l7-7" />
      <circle cx="12" cy="12" r="3" />
    </svg>
  ),
  globe: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <line x1="2" y1="12" x2="22" y2="12" />
      <path d="M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10 15.3 15.3 0 014-10z" />
    </svg>
  ),
  database: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <ellipse cx="12" cy="5" rx="9" ry="3" />
      <path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" />
      <path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
    </svg>
  ),
  activity: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
    </svg>
  ),
  layers: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="12 2 2 7 12 12 22 7 12 2" />
      <polyline points="2 17 12 22 22 17" />
      <polyline points="2 12 12 17 22 12" />
    </svg>
  ),
  server: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="2" width="20" height="8" rx="2" ry="2" />
      <rect x="2" y="14" width="20" height="8" rx="2" ry="2" />
      <line x1="6" y1="6" x2="6.01" y2="6" />
      <line x1="6" y1="18" x2="6.01" y2="18" />
    </svg>
  ),
  chart: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="18" y1="20" x2="18" y2="10" />
      <line x1="12" y1="20" x2="12" y2="4" />
      <line x1="6" y1="20" x2="6" y2="14" />
    </svg>
  ),
  bell: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M18 8A6 6 0 006 8c0 7-3 9-3 9h18s-3-2-3-9" />
      <path d="M13.73 21a2 2 0 01-3.46 0" />
    </svg>
  ),
  alertTriangle: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z" />
      <line x1="12" y1="9" x2="12" y2="13" />
      <line x1="12" y1="17" x2="12.01" y2="17" />
    </svg>
  ),
  target: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10" />
      <circle cx="12" cy="12" r="6" />
      <circle cx="12" cy="12" r="2" />
    </svg>
  ),
  harddrive: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="22" y1="12" x2="2" y2="12" />
      <path d="M5.45 5.11L2 12v6a2 2 0 002 2h16a2 2 0 002-2v-6l-3.45-6.89A2 2 0 0016.76 4H7.24a2 2 0 00-1.79 1.11z" />
      <line x1="6" y1="16" x2="6.01" y2="16" />
      <line x1="10" y1="16" x2="10.01" y2="16" />
    </svg>
  ),
  shield: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
    </svg>
  ),
  sliders: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <line x1="4" y1="21" x2="4" y2="14" />
      <line x1="4" y1="10" x2="4" y2="3" />
      <line x1="12" y1="21" x2="12" y2="12" />
      <line x1="12" y1="8" x2="12" y2="3" />
      <line x1="20" y1="21" x2="20" y2="16" />
      <line x1="20" y1="12" x2="20" y2="3" />
      <line x1="1" y1="14" x2="7" y2="14" />
      <line x1="9" y1="8" x2="15" y2="8" />
      <line x1="17" y1="16" x2="23" y2="16" />
    </svg>
  ),
  users: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M17 21v-2a4 4 0 00-4-4H5a4 4 0 00-4 4v2" />
      <circle cx="9" cy="7" r="4" />
      <path d="M23 21v-2a4 4 0 00-3-3.87" />
      <path d="M16 3.13a4 4 0 010 7.75" />
    </svg>
  ),
  link: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71" />
    </svg>
  ),
  trophy: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M6 9H4.5a2.5 2.5 0 010-5H6" />
      <path d="M18 9h1.5a2.5 2.5 0 000-5H18" />
      <path d="M4 22h16" />
      <path d="M10 14.66V17c0 .55-.47.98-.97 1.21C7.85 18.75 7 20.24 7 22" />
      <path d="M14 14.66V17c0 .55.47.98.97 1.21C16.15 18.75 17 20.24 17 22" />
      <path d="M18 2H6v7a6 6 0 0012 0V2z" />
    </svg>
  ),
  book: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 19.5A2.5 2.5 0 016.5 17H20" />
      <path d="M6.5 2H20v20H6.5A2.5 2.5 0 014 19.5v-15A2.5 2.5 0 016.5 2z" />
    </svg>
  ),
  cpu: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="4" y="4" width="16" height="16" rx="2" />
      <rect x="9" y="9" width="6" height="6" />
      <line x1="9" y1="1" x2="9" y2="4" /><line x1="15" y1="1" x2="15" y2="4" />
      <line x1="9" y1="20" x2="9" y2="23" /><line x1="15" y1="20" x2="15" y2="23" />
      <line x1="20" y1="9" x2="23" y2="9" /><line x1="20" y1="14" x2="23" y2="14" />
      <line x1="1" y1="9" x2="4" y2="9" /><line x1="1" y1="14" x2="4" y2="14" />
    </svg>
  ),
  map: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="1 6 1 22 8 18 16 22 23 18 23 2 16 6 8 2 1 6" />
      <line x1="8" y1="2" x2="8" y2="18" />
      <line x1="16" y1="6" x2="16" y2="22" />
    </svg>
  ),
  radio: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="2" />
      <path d="M16.24 7.76a6 6 0 010 8.49m-8.48-.01a6 6 0 010-8.49m11.31-2.82a10 10 0 010 14.14m-14.14 0a10 10 0 010-14.14" />
    </svg>
  ),
  externalLink: externalLinkIcon,
  chevronLeft: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
         strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <polyline points="15 18 9 12 15 6" />
    </svg>
  ),
};

export default function Sidebar({ isAdmin, collapsed, onToggle }) {
  const { user, syntheticFleet } = useAuth();
  const nav = isAdmin ? adminNav : userNav(Boolean(syntheticFleet));
  // A visitor is shown every entry, so the nav says what signing in opens;
  // those behind a session are greyed out and lead to sign-in. Read off the list
  // the route guard reads, so an entry is live exactly where its page is open.
  const locked = (item: NavItem) =>
    !user && item.to !== undefined && !isPublicRoute(item.to, isAdmin);

  return (
    <aside className="sidebar" id="console-sidebar">
      <div className="sidebar-brand">
        <div className="brand-icon">R</div>
        <div className="brand-labels">
          <div className="brand-text">Retina</div>
          <div className="brand-sub">{isAdmin ? "Admin Console" : "Node Dashboard"}</div>
        </div>
        <button
          className="sidebar-toggle"
          onClick={onToggle}
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
          aria-expanded={!collapsed}
          aria-controls="console-sidebar"
          title={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {icons.chevronLeft}
        </button>
      </div>
      <nav className="sidebar-nav">
        {nav.map((section) => (
          <div className="nav-section" key={section.title}>
            <div className="nav-section-title">{section.title}</div>
            {section.items.map((item) =>
              item.external ? (
                <a
                  key={item.href}
                  href={item.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="nav-item"
                  title={item.label}
                >
                  {icons[item.icon]}
                  <span className="nav-label">{item.label}</span>
                  <span className="nav-external">{icons.externalLink}</span>
                </a>
              ) : locked(item) ? (
                // Straight to the sign-in card rather than to a page that would
                // only bounce there, carrying the page so signing in ends on it.
                // Marked like the header's Sign in link, so the card's Back
                // returns to the open page the visitor was on.
                <Link
                  key={item.to}
                  to="/login"
                  state={{ fromOpenPage: true, next: item.to }}
                  className="nav-item locked"
                  title={`${item.label} is only available when signed in`}
                >
                  {icons[item.icon]}
                  <span className="nav-label">{item.label}</span>
                </Link>
              ) : (
                <NavLink
                  key={item.to}
                  to={item.to}
                  end={item.end ?? item.to === "/"}
                  className={({ isActive }) =>
                    `nav-item${isActive ? " active" : ""}`
                  }
                  title={item.label}
                >
                  {icons[item.icon]}
                  <span className="nav-label">{item.label}</span>
                </NavLink>
              )
            )}
          </div>
        ))}
      </nav>
    </aside>
  );
}
