import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import App from "../App";
import Sidebar from "../components/Sidebar";
import { ThemeProvider } from "../context/ThemeContext";

type User = { name: string; email: string; synthetic_fleet?: boolean };

// AuthProvider answers `syntheticFleet` from /api/auth/me for a session and
// from /api/health for a visitor; the mock hands the cases the folded answer.
const state = vi.hoisted(() => ({
  auth: {
    user: { name: "Ada", email: "ada@example.com" } as User | null,
    loading: false,
    syntheticFleet: false,
    logout: async () => ({ redirected: false }),
  },
}));
const flags = vi.hoisted(() => ({ realOnly: false }));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));
// A getter, so each render reads the case's surface rather than the one the
// module saw at load. The hostname chooses the map's feed; it must not decide
// whether Physics is offered.
vi.mock("../utils/domains", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../utils/domains")>()),
  get usesRealOnlyFeed() {
    return flags.realOnly;
  },
}));

// The page itself is Leaflet and a canvas; the route is what is under test.
vi.mock("../pages/map/PhysicsPage", () => ({ default: () => <div>physics page</div> }));
// Likewise for the map, which /sim mounts: a redirect landing on it must not
// drag Leaflet into this suite.
vi.mock("../pages/map/MapPage", () => ({ default: () => <div>map page</div> }));

/** Reports where the router ended up, so a redirect can be asserted on its
 *  destination rather than only on what rendered. */
function Where() {
  const { pathname, search, hash } = useLocation();
  return <output aria-label="location">{`${pathname}${search}${hash}`}</output>;
}

function renderSidebar({
  fleet,
  realOnly,
  signedIn = true,
}: {
  fleet: boolean;
  realOnly: boolean;
  signedIn?: boolean;
}) {
  state.auth.user = signedIn ? { name: "Ada", email: "ada@example.com", synthetic_fleet: fleet } : null;
  state.auth.syntheticFleet = fleet;
  flags.realOnly = realOnly;
  return render(
    <MemoryRouter>
      <Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />
    </MemoryRouter>,
  );
}

describe("the Physics Layer route", () => {
  it("is offered where the server runs a fleet, under the simulation", () => {
    const { container } = renderSidebar({ fleet: true, realOnly: false });
    expect(container.querySelector('a[href="/sim/physics"]')).toHaveTextContent("Physics Layer");
  });

  it("is offered on a real-radar hostname when its server runs a fleet", () => {
    const { container } = renderSidebar({ fleet: true, realOnly: true });
    expect(container.querySelector('a[href="/sim/physics"]')).toHaveTextContent("Physics Layer");
  });

  it("is absent where the server runs no fleet, whatever the hostname", () => {
    const { container } = renderSidebar({ fleet: false, realOnly: false });
    expect(container.querySelector('a[href="/sim/physics"]')).toBeNull();
  });

  // The save behind it is open, so there is no session to wait for.
  it("is offered to a visitor with no session", () => {
    const { container } = renderSidebar({ fleet: true, realOnly: false, signedIn: false });
    expect(container.querySelector('a[href="/sim/physics"]')).toHaveTextContent("Physics Layer");
  });
});

describe("the Simulation route", () => {
  it("is offered beside the map where the server runs a fleet", () => {
    const { container } = renderSidebar({ fleet: true, realOnly: false });
    expect(container.querySelector('a[href="/sim"]')).toHaveTextContent("Simulation");
  });

  it("is absent where the server runs none", () => {
    const { container } = renderSidebar({ fleet: false, realOnly: false });
    expect(container.querySelector('a[href="/sim"]')).toBeNull();
  });

  // Otherwise NavLink lights both entries up at once on /sim/physics, since a
  // link without `end` is current for its whole subtree.
  it("is not current while the physics page under it is", () => {
    for (const signedIn of [true, false]) {
      state.auth.user = signedIn ? { name: "Ada", email: "ada@example.com", synthetic_fleet: true } : null;
      state.auth.syntheticFleet = true;
      flags.realOnly = false;
      const { container, unmount } = render(
        <MemoryRouter initialEntries={["/sim/physics"]}>
          <Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />
        </MemoryRouter>,
      );
      expect(container.querySelector('a[href="/sim"]')).not.toHaveClass("active");
      expect(container.querySelector('a[href="/sim/physics"]')).toHaveClass("active");
      unmount();
    }
  });
});

describe("the /sim/physics address", () => {
  function visit(fleet: boolean, at = "/sim/physics", { signedIn = true } = {}) {
    state.auth.user = signedIn ? { name: "Ada", email: "ada@example.com", synthetic_fleet: fleet } : null;
    state.auth.syntheticFleet = fleet;
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    }) as unknown as typeof window.matchMedia;
    return render(
      <ThemeProvider>
        <MemoryRouter initialEntries={[at]}>
          <App />
          <Where />
        </MemoryRouter>
      </ThemeProvider>,
    );
  }

  it("opens the page where the server runs a fleet", async () => {
    visit(true);
    expect(await screen.findByText("physics page")).toBeInTheDocument();
  });

  // No login card on the way: the route is open like /sim above it.
  it("opens the page to a visitor with no session", async () => {
    visit(true, "/sim/physics", { signedIn: false });
    expect(await screen.findByText("physics page")).toBeInTheDocument();
    expect(screen.getByLabelText("location")).toHaveTextContent("/sim/physics");
  });

  it("is no page at all where it runs none", async () => {
    visit(false);
    // Give the lazy route a chance to resolve before asserting it never did.
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("physics page")).toBeNull();
  });
});
