import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation } from "react-router-dom";
import App from "../App";
import { ThemeProvider } from "../context/ThemeContext";
import { stubMatchMedia } from "./matchMedia";

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

function Where() {
  const l = useLocation();
  return <output aria-label="location">{`${l.pathname}${l.search}${l.hash}`}</output>;
}

function visit(path: string, user: { name: string; email: string } | null) {
  state.auth.user = user;
  stubMatchMedia();
  render(
    <ThemeProvider>
      <MemoryRouter initialEntries={[path]}>
        <App />
        <Where />
      </MemoryRouter>
    </ThemeProvider>,
  );
}

describe("the console's index", () => {
  it("opens the map for a visitor with no session, rather than the login card", async () => {
    visit("/", null);
    expect(await screen.findByText("map page")).toBeInTheDocument();
  });

  it("opens it at its own address", async () => {
    visit("/", null);
    await screen.findByText("map page");
    expect(screen.getByLabelText("location").textContent).toBe("/map");
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
