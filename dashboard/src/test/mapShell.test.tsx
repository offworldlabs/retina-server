import { describe, it, expect, beforeEach, vi } from "vitest";
import { render } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import appSource from "../App.tsx?raw";
import DashboardLayout, { pageTitles } from "../components/DashboardLayout";
import { ThemeProvider } from "../context/ThemeContext";

const state = vi.hoisted(() => ({
  auth: {
    user: { name: "Ada", email: "ada@example.com" } as { name: string; email: string } | null,
    loading: false,
    logout: async () => ({ redirected: false }),
  },
}));

// Sidebar and Header both call useAuth, which throws outside AuthProvider; the
// house pattern (signedOutChrome.test.tsx) mocks it rather than mounting one.
vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

/** Stubbed rather than borrowed, as in theme.test.tsx: jsdom has no matchMedia,
 *  and Node 20 and 26 disagree about whose window.localStorage is reached. */
function stubBrowser(seed: Record<string, string> = {}) {
  const store = new Map(Object.entries(seed));
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    writable: true,
    value: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
      key: (i: number) => [...store.keys()][i] ?? null,
      get length() { return store.size; },
    },
  });
  window.matchMedia = vi.fn().mockReturnValue({
    matches: false,
    media: "(prefers-color-scheme: dark)",
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as typeof window.matchMedia;
  return store;
}

function renderAt(path: string, isAdmin = false) {
  return render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <DashboardLayout isAdmin={isAdmin}><div>page</div></DashboardLayout>
      </MemoryRouter>
    </ThemeProvider>,
  );
}

describe("the map's content pane", () => {
  beforeEach(() => stubBrowser());

  it("is flush on the map route", () => {
    const { container } = renderAt("/map");
    expect(container.querySelector(".content.flush")).not.toBeNull();
  });

  it("is flush on the simulation map", () => {
    const { container } = renderAt("/sim", true);
    expect(container.querySelector(".content.flush")).not.toBeNull();
  });

  // Nested under /sim, and the rule is read off the first segment, so this is
  // the case that would quietly regress if that rule were narrowed to /sim
  // exactly.
  it("is flush on the physics page under it", () => {
    const { container } = renderAt("/sim/physics", true);
    expect(container.querySelector(".content.flush")).not.toBeNull();
  });

  it("keeps its padding everywhere else", () => {
    const { container } = renderAt("/detections");
    expect(container.querySelector(".content.flush")).toBeNull();
    expect(container.querySelector(".content")).not.toBeNull();
  });
});

describe("the header's name for a page", () => {
  beforeEach(() => stubBrowser());

  // Every other page is named by its first path segment and owns whatever
  // nests under it. /sim is the exception: the page beneath it configures the
  // simulator rather than being a view of it, so the table is consulted with
  // the whole path first.
  it("names the simulation map", () => {
    const { container } = renderAt("/sim", true);
    expect(container.querySelector(".content")).not.toBeNull();
    expect(container.textContent).toContain("Simulation Map");
  });

  it("names the physics page nested under it, not its parent", () => {
    const { container } = renderAt("/sim/physics", true);
    expect(container.textContent).toContain("Physics Layer");
    expect(container.textContent).not.toContain("Simulation Map");
  });

  it("names a node's own page, not the list above it", () => {
    const { container } = renderAt("/nodes/nde0example0001");
    expect(container.textContent).toContain("Node Detail");
  });

  // The sidebar carries most of these words too, so read the header alone.
  const titleAt = (path: string, isAdmin: boolean) =>
    renderAt(path, isAdmin).container.querySelector(".header-title")?.textContent;

  it("names a node's own page on the admin surface too, where /nodes is the list", () => {
    expect(titleAt("/nodes", true)).toBe("Node Management");
    expect(titleAt("/nodes/nde0example0001", true)).toBe("Node Detail");
  });

  it.each([
    ["/mlat", "MLAT Verification"],
    ["/infrastructure", "Infrastructure"],
    ["/api-docs", "API Reference"],
  ])("names the admin page at %s", (path, title) => {
    expect(titleAt(path, true)).toBe(title);
  });

  it("names a page that owns its subtree by its first segment", () => {
    expect(renderAt("/sim/anything", true).container.textContent).toContain("Simulation Map");
  });
});

// The routes live in App.tsx and the titles here, and nothing else ties the
// two: a route added without a title reads "Dashboard".
describe("every routed console page", () => {
  const branches = appSource.match(/isAdminSite \? \(([\s\S]*?)\) : \(([\s\S]*?)\)\}\s*<\/Routes>/);
  const routes = (branch: string | undefined) =>
    [...(branch ?? "").matchAll(/<Route (?:index|path="([^"]+)")/g)].map((m) => (m[1] ? `/${m[1]}` : "/"));
  const surfaces = [
    ["admin", routes(branches?.[1])],
    ["user", routes(branches?.[2])],
  ] as const;

  it.each(surfaces)("on the %s surface is found, so an empty parse cannot pass", (_, paths) => {
    expect(paths.length).toBeGreaterThan(10);
  });

  it.each(surfaces)("on the %s surface has a title", (surface, paths) => {
    expect(paths.filter((path) => !pageTitles[path]?.[surface])).toEqual([]);
  });
});
