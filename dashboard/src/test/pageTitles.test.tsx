import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { api } from "../api/client";
import DashboardLayout from "../components/DashboardLayout";
import Sidebar from "../components/Sidebar";
import { ThemeProvider } from "../context/ThemeContext";
import { consoleRoutes, type ConsoleRoute } from "../routes";
import { stubMatchMedia } from "./matchMedia";

/**
 * The header's name for each page, read off the table the router is built
 * from. Each entry below comes from that table rather than from a list kept
 * here, so a page added to it is checked without an edit to this file.
 */

const state = vi.hoisted(() => ({
  auth: {
    user: { id: "u1", name: "Ada", email: "ada@example.com", role: "admin" },
    loading: false,
    syntheticFleet: true,
    logout: async () => ({ redirected: false }),
  },
}));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

beforeEach(() => {
  state.auth.syntheticFleet = true;
  stubMatchMedia();
});

/** The header's title at `path`, read off the header alone: the sidebar
 *  carries many of the same words. */
function titleAt(path: string, isAdmin: boolean) {
  const { container, unmount } = render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <DashboardLayout isAdmin={isAdmin}>
          <div>page</div>
        </DashboardLayout>
      </MemoryRouter>
    </ThemeProvider>,
  );
  const title = container.querySelector(".header-title")?.textContent;
  unmount();
  return title;
}

/** An address the entry's pattern matches, each `:param` filled in. */
const addressOf = (path: string) => path.replace(/:\w+/g, "example0001");

/** What the header should call an entry: its own title, or for a forward, the
 *  title of the page it lands on. */
function expectedTitle(route: ConsoleRoute, routes: ConsoleRoute[]): string | undefined {
  if (!("to" in route)) return route.title;
  const target = routes.find((r) => r.path === route.to);
  return target && "title" in target ? target.title : undefined;
}

const surfaces = [
  { surface: "user", isAdmin: false, routes: consoleRoutes(false, { syntheticFleet: true }) },
  { surface: "admin", isAdmin: true, routes: consoleRoutes(true, { syntheticFleet: true }) },
];

const entries = surfaces.flatMap(({ surface, isAdmin, routes }) =>
  routes.map((route) => ({ surface, isAdmin, path: route.path, title: expectedTitle(route, routes) })),
);

describe("the route table", () => {
  it.each(surfaces)("lists the $surface surface's pages, so an empty table cannot pass", ({ routes }) => {
    expect(routes.length).toBeGreaterThan(10);
  });

  // A second entry for a path would be routed or named, not both.
  it.each(surfaces)("names each path on the $surface surface once", ({ routes }) => {
    const paths = routes.map((r) => r.path);
    expect(paths).toEqual([...new Set(paths)]);
  });

  it("forwards only to a page in the same surface's table", () => {
    const forwards = surfaces.flatMap(({ routes }) =>
      routes.filter((r) => "to" in r).map((forward) => [forward.path, expectedTitle(forward, routes)]),
    );
    // The app's index and /onboarding, so a table that lost them cannot pass.
    expect(forwards.length).toBeGreaterThanOrEqual(2);
    expect(forwards.filter(([, title]) => !title)).toEqual([]);
  });
});

describe("the header's name for every routed page", () => {
  it.each(entries)("is $title at $path on the $surface surface", ({ path, isAdmin, title }) => {
    expect(title).toBeTruthy();
    expect(titleAt(addressOf(path), isAdmin)).toBe(title);
  });
});

describe("the header's name off the table's own entries", () => {
  it.each([
    // A page owns whatever nests under it: the archive's own deep link.
    ["/data/2026/09/17", false, "Data Explorer"],
    // Two segments, and its own page rather than a view of the simulator.
    ["/sim/physics", true, "Physics Layer"],
    ["/sim/anything", true, "Simulation Map"],
    // A pattern, because the first segment alone would name the admin list.
    ["/nodes/nde0example0001", false, "Node Detail"],
    ["/nodes/nde0example0001", true, "Node Detail"],
    ["/nodes", true, "Node Management"],
    // Matched as the router ranks them, so the static path beats the pattern.
    ["/radars/new", false, "Add a Radar"],
    ["/radars/ret-0042", false, "Your Radar"],
    // The index forwards to the map on the app, and is a page on the console.
    ["/", false, "Live Map"],
    ["/", true, "Network Health"],
    // Forwards to the overview, so it borrows that page's name.
    ["/onboarding", false, "My Nodes"],
    ["/overview/", false, "My Nodes"],
    ["/contribution", false, "Contribution"],
    ["/tunnel", false, "Tunnel Link"],
  ])("names %s (admin: %s) %s", (path, isAdmin, title) => {
    expect(titleAt(path, isAdmin)).toBe(title);
  });

  it.each([
    ["/nowhere", false],
    ["/nowhere", true],
    // Each surface's pages, asked for on the other.
    ["/users", false],
    ["/leaderboard", true],
    ["/sim", false],
  ])("falls back to the generic name at %s (admin: %s)", (path, isAdmin) => {
    expect(titleAt(path, isAdmin)).toBe("Dashboard");
  });
});

describe("the simulator's entries", () => {
  it("exist on the admin console where the server runs a fleet", () => {
    const paths = consoleRoutes(true, { syntheticFleet: true }).map((r) => r.path);
    expect(paths).toEqual(expect.arrayContaining(["/sim", "/sim/physics"]));
  });

  it("are absent where it runs none", () => {
    const paths = consoleRoutes(true, { syntheticFleet: false }).map((r) => r.path);
    expect(paths.filter((p) => p.startsWith("/sim"))).toEqual([]);
  });

  it("are absent from the app, even beside a fleet", () => {
    const paths = consoleRoutes(false, { syntheticFleet: true }).map((r) => r.path);
    expect(paths.filter((p) => p.startsWith("/sim"))).toEqual([]);
  });

  // The header reads the session's own answer, not the table built above.
  it.each(["/sim", "/sim/physics"])("leave %s unnamed where there is no fleet", (path) => {
    state.auth.syntheticFleet = false;
    expect(titleAt(path, true)).toBe("Dashboard");
  });
});

describe("the radar sandbox's entry", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.resetModules();
  });

  /** A fresh copy of the table, built under the environment stubbed so far. */
  async function freshUserPaths() {
    vi.resetModules();
    const routes = await import("../routes");
    return routes.consoleRoutes(false, { syntheticFleet: false }).map((r) => r.path);
  }

  it("is in a dev build, under its own name", async () => {
    vi.stubEnv("DEV", true);
    expect(await freshUserPaths()).toContain("/test-radar");
    expect(titleAt("/test-radar", false)).toBe("Test Radar");
  });

  it("is absent from a production build", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_ENABLE_TEST_RADAR", undefined);
    expect(await freshUserPaths()).not.toContain("/test-radar");
  });

  it("is in a production build that asks for it", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_ENABLE_TEST_RADAR", "1");
    expect(await freshUserPaths()).toContain("/test-radar");
  });

  it("is never on the admin console", () => {
    expect(consoleRoutes(true, { syntheticFleet: true }).map((r) => r.path)).not.toContain("/test-radar");
  });
});

describe("the sidebar's names for the renamed pages", () => {
  it.each([
    ["Contribution", "/contribution"],
    ["Tunnel Link", "/tunnel"],
  ])("calls the page %s, as its header does", (label, path) => {
    render(
      <MemoryRouter>
        <Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />
      </MemoryRouter>,
    );
    expect(screen.getByRole("link", { name: label })).toHaveAttribute("href", path);
    expect(titleAt(path, false)).toBe(label);
  });

  it("heads the contribution page with the header's name for it", async () => {
    const title = titleAt("/contribution", false);
    vi.spyOn(api, "analytics").mockResolvedValue({ nodes: {} } as never);
    vi.spyOn(api, "overlaps").mockResolvedValue([] as never);
    vi.spyOn(api, "leaderboard").mockResolvedValue([] as never);
    try {
      const { default: ContributionPage } = await import("../pages/user/ContributionPage");
      render(
        <MemoryRouter>
          <ContributionPage />
        </MemoryRouter>,
      );
      expect(await screen.findByRole("heading", { level: 1 })).toHaveTextContent(new RegExp(`^${title}$`));
    } finally {
      vi.restoreAllMocks();
    }
  });

  it("no longer calls the contribution page Network", () => {
    render(
      <MemoryRouter>
        <Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />
      </MemoryRouter>,
    );
    expect(screen.queryByRole("link", { name: "Network" })).toBeNull();
  });
});
