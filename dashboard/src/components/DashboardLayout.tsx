import { useEffect, useRef, useState, type ComponentProps, type ReactNode } from "react";
import { useLocation } from "react-router-dom";
import Sidebar from "./Sidebar";
import Header from "./Header";
import ErrorBoundary from "./ErrorBoundary";
import { useAuth } from "../context/AuthContext";
import { pageTitle, useConsoleRoutes } from "../routes";
import { useMediaQuery } from "../hooks/useMediaQuery";
import { DRAWER, TABLET } from "../styles/breakpoints";
import { readStored, writeStored } from "../utils/storage";

const SIDEBAR_KEY = "retina.sidebarCollapsed";

/** The stored choice, or null when the user has never made one. */
function storedCollapsed(): boolean | null {
  const raw = readStored(SIDEBAR_KEY);
  return raw === null ? null : raw === "true";
}

type DashboardLayoutProps = {
  isAdmin: boolean;
  children: ReactNode;
  /** Names what the layout holds when it is not the page at this address, as
   *  for the admin console's refusal. That page's own shape (a flush pane, the
   *  map's collapsed rail) is not used either. */
  title?: string;
  /** False leaves the sidebar without its sections, for a caller none of them
   *  would open for, and the header without the Menu button that would only
   *  open the empty drawer. */
  nav?: boolean;
};

export default function DashboardLayout({ isAdmin, children, title, nav = true }: DashboardLayoutProps) {
  const { pathname } = useLocation();
  const { user } = useAuth();
  const routed = title === undefined;
  const segments = pathname.split("/").filter(Boolean);
  const fullPath = segments.length ? `/${segments.join("/")}` : "/";
  const basePath = segments.length ? `/${segments[0]}` : "/";

  const [stored, setStored] = useState(storedCollapsed);
  const drawer = useMediaQuery(DRAWER);
  const tablet = useMediaQuery(TABLET);
  // The map wants the canvas; every other page wants the labels, except on a
  // tablet, where the rail leaves the page its width. An explicit choice
  // outranks all three.
  const collapsed =
    stored ?? ((routed && (basePath === "/map" || fullPath === "/sim")) || (tablet && !drawer));
  // The map and the physics layer draw to the edges and scroll nothing: their
  // own panels own their overflow, and a scrollbar on the pane would move the
  // canvas under them. `/sim` by first segment covers both the sim map and the
  // physics page under it.
  const flush = routed && (basePath === "/map" || basePath === "/sim");

  const toggle = () => {
    const next = !collapsed;
    setStored(next);
    writeStored(SIDEBAR_KEY, String(next));
  };

  // On a narrow screen the sidebar is a drawer over the page. The stylesheet
  // decides how it looks; on a wide screen this state is inert.
  const [menuOpen, setMenuOpen] = useState(false);
  const menuButton = useRef<HTMLButtonElement>(null);
  const content = useRef<HTMLDivElement>(null);

  const [seenPath, setSeenPath] = useState(pathname);
  if (pathname !== seenPath) {
    setSeenPath(pathname);
    setMenuOpen(false);
  }
  // Widened past the drawer, the sidebar is back in the row but the page would
  // stay inert. Must match the stylesheet's narrow query.
  const [seenDrawer, setSeenDrawer] = useState(drawer);
  if (drawer !== seenDrawer) {
    setSeenDrawer(drawer);
    if (!drawer) setMenuOpen(false);
  }

  useEffect(() => {
    if (!menuOpen) return;
    // The drawer precedes the header in the DOM, so Tab from the button would
    // never reach it.
    document.querySelector<HTMLElement>("#console-sidebar .sidebar-nav a")?.focus();
    // Set as a property: React 18 has no boolean `inert` attribute.
    const page = content.current;
    if (page) page.inert = true;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setMenuOpen(false);
      menuButton.current?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      if (page) page.inert = false;
      document.removeEventListener("keydown", onKey);
    };
  }, [menuOpen]);

  const header = {
    isAdmin,
    menuOpen,
    onMenu: nav ? () => setMenuOpen(!menuOpen) : undefined,
    menuButtonRef: menuButton,
  };

  return (
    <div className={`dashboard${collapsed ? " sidebar-collapsed" : ""}${menuOpen ? " menu-open" : ""}`}>
      <Sidebar
        isAdmin={isAdmin}
        collapsed={collapsed}
        onToggle={toggle}
        onNavigate={() => setMenuOpen(false)}
        nav={nav}
      />
      {menuOpen && <div className="sidebar-scrim" onClick={() => setMenuOpen(false)} />}
      <div className="main-area">
        {routed ? <RoutedHeader {...header} /> : <Header {...header} title={title} />}
        <div ref={content} className={`content${flush ? " flush" : ""}`}>
          {/* Keyed on the path, which names the page: the query string and hash
              are state within it. Remounted, page and all, when the signed-in
              identity changes, since what a page fetched for one session (an
              owner's marks, say) must not outlive it. The header and sidebar
              stay, and redraw from the new identity. */}
          <ErrorBoundary
            key={user?.id ?? ""}
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

/** The header of the page at this address, under that page's own title. Its
 *  own component so that only it asks the route table for one: a layout that
 *  names what it holds, as the admin console's refusal does, builds none. */
function RoutedHeader({ isAdmin, ...props }: Omit<ComponentProps<typeof Header>, "title"> & { isAdmin: boolean }) {
  const { pathname } = useLocation();
  const routes = useConsoleRoutes(isAdmin);
  return <Header {...props} isAdmin={isAdmin} title={pageTitle(pathname, routes)} />;
}
