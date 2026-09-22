import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import App from "../App";
import Forward from "../components/Forward";
import { ThemeProvider } from "../context/ThemeContext";

const state = vi.hoisted(() => ({
  auth: {
    user: null as { name: string; email: string } | null,
    loading: false,
    logout: async () => ({ redirected: false }),
  },
}));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));
// The pages themselves are Leaflet and live data; where the visitor lands is
// what is under test.
vi.mock("../pages/map/MapPage", () => ({ default: () => <div>map page</div> }));
vi.mock("../pages/user/OverviewPage", () => ({ default: () => <div>overview page</div> }));

function visit(path: string, user: { name: string; email: string } | null) {
  state.auth.user = user;
  window.matchMedia = vi.fn().mockReturnValue({
    matches: false,
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as typeof window.matchMedia;
  render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <App />
      </MemoryRouter>
    </ThemeProvider>,
  );
}

function Where() {
  const l = useLocation();
  return <div data-testid="where">{`${l.pathname}${l.search}${l.hash}`}</div>;
}

function at(entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route index element={<Forward to="/map" />} />
        <Route path="map" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("the front door", () => {
  it("opens on the map", () => {
    expect(at("/").getByTestId("where").textContent).toBe("/map");
  });

  it("keeps an old map link's view, which lives in the hash", () => {
    const view = "#lat=41.1160&lon=-96.7549&z=4&layers=ltsdu";
    expect(at(`/${view}`).getByTestId("where").textContent).toBe(`/map${view}`);
  });

  it("keeps the query string", () => {
    expect(at("/?hex=abc123").getByTestId("where").textContent).toBe("/map?hex=abc123");
  });
});

describe("the console's index", () => {
  it("opens the map for a visitor with no session, rather than the login card", async () => {
    visit("/", null);
    expect(await screen.findByText("map page")).toBeInTheDocument();
  });

  it("opens the map for a signed-in user too", async () => {
    visit("/", { name: "Ada", email: "ada@example.com" });
    expect(await screen.findByText("map page")).toBeInTheDocument();
  });

  it("keeps the overview, one click away, for a signed-in user", async () => {
    visit("/overview", { name: "Ada", email: "ada@example.com" });
    expect(await screen.findByText("overview page")).toBeInTheDocument();
  });
});
