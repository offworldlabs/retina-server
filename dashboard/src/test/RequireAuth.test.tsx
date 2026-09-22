import { describe, it, expect, beforeEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";

import RequireAuth from "../components/RequireAuth";

const state = vi.hoisted(() => ({
  auth: { user: null as { role?: string } | null, loading: false },
}));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

/** The guard under a router, with a login route to land on so a redirect is
 *  visible as a rendered page rather than as a call on a spy. */
function renderAt(pathname: string, isAdmin = false) {
  return render(
    <MemoryRouter initialEntries={[pathname]}>
      <Routes>
        <Route path="/login" element={<span>login card</span>} />
        <Route
          path="*"
          element={
            <RequireAuth isAdmin={isAdmin}>
              <span>the page</span>
            </RequireAuth>
          }
        />
      </Routes>
    </MemoryRouter>
  );
}

describe("the route guard", () => {
  beforeEach(() => {
    state.auth = { user: null, loading: false };
  });

  it("renders a page that needs no session to a caller with none", () => {
    renderAt("/leaderboard");
    expect(screen.getByText("the page")).toBeInTheDocument();
  });

  it("sends a caller with no session away from a page that needs one", () => {
    renderAt("/onboarding");
    expect(screen.getByText("login card")).toBeInTheDocument();
  });

  function renderWithCard(pathname: string, isAdmin: boolean) {
    function Card() {
      return <span>login card for {useLocation().state?.next ?? "nowhere"}</span>;
    }
    render(
      <MemoryRouter initialEntries={[pathname]}>
        <Routes>
          <Route path="/login" element={<Card />} />
          <Route
            path="*"
            element={
              <RequireAuth isAdmin={isAdmin}>
                <span>the page</span>
              </RequireAuth>
            }
          />
        </Routes>
      </MemoryRouter>
    );
  }

  // So that signing in ends on it, rather than on the map.
  it("tells the login card which page it sent the caller away from", () => {
    renderWithCard("/nodes/ret-0042", false);
    expect(screen.getByText("login card for /nodes/ret-0042")).toBeInTheDocument();
  });

  // The mailed link opens on the app host, where no admin route exists.
  it("tells it nothing from the admin console", () => {
    renderWithCard("/nodes", true);
    expect(screen.getByText("login card for nowhere")).toBeInTheDocument();
  });

  it("renders a page that needs a session to a caller who has one", () => {
    state.auth = { user: { role: "user" }, loading: false };
    renderAt("/onboarding");
    expect(screen.getByText("the page")).toBeInTheDocument();
  });

  // The console has no open routes, whatever they are called in the other tree.
  it("sends a caller with no session away from the admin console", () => {
    renderAt("/leaderboard", true);
    expect(screen.getByText("login card")).toBeInTheDocument();
  });

  it("refuses the admin console to a caller who is not an administrator", () => {
    state.auth = { user: { role: "user" }, loading: false };
    renderAt("/nodes", true);
    expect(screen.getByText("Access Denied")).toBeInTheDocument();
  });

  // Settling first costs one round trip and saves showing a signed-in caller
  // the signed-out chrome before their identity arrives.
  it("waits for an identity that has not settled, on an open route too", () => {
    state.auth = { user: null, loading: true };
    renderAt("/leaderboard");
    expect(screen.queryByText("the page")).not.toBeInTheDocument();
    expect(screen.queryByText("login card")).not.toBeInTheDocument();
  });
});
