import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import App from "../App";
import Sidebar from "../components/Sidebar";
import { ThemeProvider } from "../context/ThemeContext";
import { showsPhysics } from "../utils/physics";

type User = { name: string; email: string; synthetic_fleet?: boolean };

// The Physics item is in the signed-in nav, so the mocked caller has a session.
const state = vi.hoisted(() => ({
  auth: {
    user: { name: "Ada", email: "ada@example.com" } as User | null,
    loading: false,
    logout: async () => ({ redirected: false }),
  },
}));
const flags = vi.hoisted(() => ({ realOnly: false }));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));
// A getter, so each render reads the case's surface rather than the one the
// module saw at load. The hostname chooses the map's feed; it must not decide
// whether Physics is offered.
vi.mock("../pages/map/utils/domains", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../pages/map/utils/domains")>()),
  get usesRealOnlyFeed() {
    return flags.realOnly;
  },
}));

// The page itself is Leaflet and a canvas; the route is what is under test.
vi.mock("../pages/map/PhysicsPage", () => ({ default: () => <div>physics page</div> }));

function renderSidebar({ fleet, realOnly }: { fleet: boolean; realOnly: boolean }) {
  state.auth.user = { name: "Ada", email: "ada@example.com", synthetic_fleet: fleet };
  flags.realOnly = realOnly;
  return render(
    <MemoryRouter>
      <Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />
    </MemoryRouter>,
  );
}

describe("the Physics Layer route", () => {
  it("is offered where the server runs a fleet", () => {
    const { container } = renderSidebar({ fleet: true, realOnly: false });
    expect(container.querySelector('a[href="/physics"]')).toHaveTextContent("Physics Layer");
  });

  it("is offered on a real-radar hostname when its server runs a fleet", () => {
    const { container } = renderSidebar({ fleet: true, realOnly: true });
    expect(container.querySelector('a[href="/physics"]')).toHaveTextContent("Physics Layer");
  });

  it("is absent where the server runs no fleet, whatever the hostname", () => {
    const { container } = renderSidebar({ fleet: false, realOnly: false });
    expect(container.querySelector('a[href="/physics"]')).toBeNull();
  });
});

describe("the /physics address", () => {
  function visit(fleet: boolean) {
    state.auth.user = { name: "Ada", email: "ada@example.com", synthetic_fleet: fleet };
    window.matchMedia = vi.fn().mockReturnValue({
      matches: false,
      addEventListener: () => {},
      removeEventListener: () => {},
    }) as unknown as typeof window.matchMedia;
    render(
      <ThemeProvider>
        <MemoryRouter initialEntries={["/physics"]}>
          <App />
        </MemoryRouter>
      </ThemeProvider>,
    );
  }

  it("opens the page where the server runs a fleet", async () => {
    visit(true);
    expect(await screen.findByText("physics page")).toBeInTheDocument();
  });

  it("is no page at all where it runs none", async () => {
    visit(false);
    // Give the lazy route a chance to resolve before asserting it never did.
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText("physics page")).toBeNull();
  });
});

describe("showsPhysics", () => {
  it("shows it to a signed-in user on a server with a fleet", () => {
    expect(showsPhysics({ synthetic_fleet: true })).toBe(true);
  });

  it("hides it where the server runs no fleet", () => {
    expect(showsPhysics({ synthetic_fleet: false })).toBe(false);
  });

  it("hides it from a server that does not say", () => {
    expect(showsPhysics({})).toBe(false);
  });

  it("hides it when signed out", () => {
    expect(showsPhysics(null)).toBe(false);
  });
});
