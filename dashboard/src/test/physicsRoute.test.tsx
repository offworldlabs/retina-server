import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import Sidebar from "../components/Sidebar";

type User = { name: string; email: string; role: string; synthetic_fleet?: boolean };

// AuthProvider answers `syntheticFleet` from /api/auth/me; the mock hands the
// cases that answer directly.
const state = vi.hoisted(() => ({
  auth: {
    user: null as User | null,
    loading: false,
    syntheticFleet: false,
    logout: async () => ({ redirected: false }),
  },
  admin: false,
}));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));
// App resolves its surface once, at module scope, so each visit below loads a
// fresh copy of it under the surface the case names.
vi.mock("../utils/surface", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../utils/surface")>()),
  resolveSurface: () => ({ isAdmin: state.admin, modeParamIgnored: false }),
}));

// The pages themselves are Leaflet and a canvas; the routes are what is under
// test, and the map reports the props it was mounted with.
vi.mock("../pages/map/PhysicsPage", () => ({ default: () => <div>physics page</div> }));
vi.mock("../pages/map/MapPage", () => ({
  default: ({ feed, ownerView = true }: { feed?: string; ownerView?: boolean }) => (
    <div>{`map page, ${feed ?? "default"} feed, owner view ${ownerView ? "on" : "off"}`}</div>
  ),
}));

function signIn({ admin, fleet, signedIn = true }: { admin: boolean; fleet: boolean; signedIn?: boolean }) {
  state.admin = admin;
  state.auth.syntheticFleet = fleet;
  state.auth.user = signedIn
    ? { name: "Ada", email: "ada@example.com", role: admin ? "admin" : "user", synthetic_fleet: fleet }
    : null;
}

function renderSidebar(opts: { admin: boolean; fleet: boolean }, at = "/") {
  signIn(opts);
  return render(
    <MemoryRouter initialEntries={[at]}>
      <Sidebar isAdmin={opts.admin} collapsed={false} onToggle={() => {}} />
    </MemoryRouter>,
  );
}

describe("the admin console's Simulation section", () => {
  it("holds the fleet map and then the page that tunes it, where the server runs a fleet", () => {
    const { container } = renderSidebar({ admin: true, fleet: true });
    const section = screen.getByText("Simulation", { selector: ".nav-section-title" }).parentElement!;
    const links = [...section.querySelectorAll("a")].map((a) => [a.getAttribute("href"), a.textContent]);
    expect(links).toEqual([
      ["/sim", "Simulation Map"],
      ["/sim/physics", "Physics Layer"],
    ]);
    expect(container.querySelectorAll('a[href^="/sim"]')).toHaveLength(2);
  });

  it("is absent where the server runs none", () => {
    const { container } = renderSidebar({ admin: true, fleet: false });
    expect(screen.queryByText("Simulation", { selector: ".nav-section-title" })).toBeNull();
    expect(container.querySelector('a[href^="/sim"]')).toBeNull();
  });

  // Otherwise NavLink lights both entries up at once on /sim/physics, since a
  // link without `end` is current for its whole subtree.
  it("does not mark the map current while the physics page under it is", () => {
    const { container } = renderSidebar({ admin: true, fleet: true }, "/sim/physics");
    expect(container.querySelector('a[href="/sim"]')).not.toHaveClass("active");
    expect(container.querySelector('a[href="/sim/physics"]')).toHaveClass("active");
  });
});

describe("the app's sidebar", () => {
  it("offers neither page, even where the server runs a fleet", () => {
    const { container } = renderSidebar({ admin: false, fleet: true });
    expect(container.querySelector('a[href^="/sim"]')).toBeNull();
  });
});

describe("the /sim addresses", () => {
  async function visit(at: string, opts: { admin: boolean; fleet: boolean; signedIn?: boolean }) {
    signIn(opts);
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    }) as unknown as typeof window.matchMedia;
    vi.resetModules();
    // All three from the one fresh registry, so App's router and theme
    // contexts are the ones these providers supply.
    const [{ default: App }, { ThemeProvider }, router] = await Promise.all([
      import("../App"),
      import("../context/ThemeContext"),
      import("react-router-dom"),
    ]);
    function Where() {
      const { pathname } = router.useLocation();
      return <output aria-label="location">{pathname}</output>;
    }
    return render(
      <ThemeProvider>
        <router.MemoryRouter initialEntries={[at]}>
          <App />
          <Where />
        </router.MemoryRouter>
      </ThemeProvider>,
    );
  }

  /** Gives a lazy route the chance to resolve before asserting it never did. */
  const settle = () => new Promise((r) => setTimeout(r, 50));

  it("opens the synthetic fleet's map on the admin console, with no owner view", async () => {
    await visit("/sim", { admin: true, fleet: true });
    expect(await screen.findByText("map page, synthetic feed, owner view off")).toBeInTheDocument();
  });

  it("opens the physics page on the admin console", async () => {
    await visit("/sim/physics", { admin: true, fleet: true });
    expect(await screen.findByText("physics page")).toBeInTheDocument();
  });

  it.each(["/sim", "/sim/physics"])("makes %s no page on the admin console where there is no fleet", async (at) => {
    await visit(at, { admin: true, fleet: false });
    await settle();
    expect(screen.queryByText(/map page|physics page/)).toBeNull();
  });

  it.each(["/sim", "/sim/physics"])("makes %s no page on the app, even beside a fleet", async (at) => {
    await visit(at, { admin: false, fleet: true });
    await settle();
    expect(screen.queryByText(/map page|physics page/)).toBeNull();
    expect(screen.getByLabelText("location")).toHaveTextContent(at);
  });

  // Not open to a visitor either, so the guard sends them where it sends
  // them for any page that is not.
  it.each(["/sim", "/sim/physics"])("sends a visitor to %s on the app to sign in", async (at) => {
    await visit(at, { admin: false, fleet: true, signedIn: false });
    await waitFor(() => expect(screen.getByLabelText("location")).toHaveTextContent("/login"));
    expect(screen.queryByText(/map page|physics page/)).toBeNull();
  });
});
