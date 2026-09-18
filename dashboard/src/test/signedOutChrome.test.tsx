import { describe, it, expect, beforeEach, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigationType } from "react-router-dom";

import Sidebar from "../components/Sidebar";
import Header from "../components/Header";
import LoginPage from "../pages/LoginPage";
import { ThemeProvider } from "../context/ThemeContext";
import { PUBLIC_ROUTES } from "../utils/publicRoutes";

const state = vi.hoisted(() => ({
  auth: { user: null as { name: string; email: string } | null, loading: false, logout: async () => ({ redirected: false }) },
}));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

/** As in theme.test.tsx: stubbed rather than borrowed, because jsdom has no
 *  matchMedia and Node 20 and 26 disagree about window.localStorage. */
function stubBrowser() {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    value: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
      key: () => null,
      length: 0,
    },
  });
  window.matchMedia = vi.fn().mockReturnValue({
    matches: false,
    media: "(prefers-color-scheme: dark)",
    addEventListener: () => {},
    removeEventListener: () => {},
  }) as unknown as typeof window.matchMedia;
}

const signedIn = { name: "Ada", email: "ada@example.com" };

beforeEach(() => {
  state.auth = { user: null, loading: false, logout: async () => ({ redirected: false }) };
});

describe("the sidebar shown to a caller with no session", () => {
  function renderSidebar() {
    render(
      <MemoryRouter>
        <Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />
      </MemoryRouter>
    );
  }

  // Drawn from the allowlist rather than repeating it, so opening a route and
  // advertising it are the same edit: a fifth entry in PUBLIC_ROUTES that the
  // nav did not pick up fails here rather than going unmentioned.
  it("offers every open route, and only those", () => {
    renderSidebar();
    const shown = screen
      .getAllByRole("link")
      .map((a) => a.textContent?.trim())
      .filter(Boolean);
    expect(shown).toEqual(PUBLIC_ROUTES.map((r) => r.label));
  });

  it("points each internal entry at its own route", () => {
    renderSidebar();
    for (const { path, label } of PUBLIC_ROUTES) {
      expect(screen.getByRole("link", { name: label })).toHaveAttribute("href", path);
    }
  });

  // Nothing on screen leads to a wall: an entry that only answers to a session
  // is a link whose whole behaviour is to bounce the caller to a login card.
  it.each(["Overview", "Detections", "My Nodes", "Settings", "Alerts"])(
    "withholds %s",
    (label) => {
      renderSidebar();
      expect(screen.queryByText(label)).not.toBeInTheDocument();
    }
  );

  it("offers the whole dashboard once there is a session", () => {
    state.auth = { ...state.auth, user: signedIn };
    renderSidebar();
    expect(screen.getByText("Settings")).toBeInTheDocument();
    expect(screen.getByText("Leaderboard")).toBeInTheDocument();
  });
});

describe("the header shown to a caller with no session", () => {
  beforeEach(() => {
    stubBrowser();
    document.documentElement.removeAttribute("data-theme");
  });

  function renderHeader() {
    return render(
      <MemoryRouter>
        <ThemeProvider>
          <Header title="Leaderboard" />
        </ThemeProvider>
      </MemoryRouter>
    );
  }

  it("offers a way in", () => {
    renderHeader();
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
  });

  it("does not offer a way out of a session that does not exist", () => {
    renderHeader();
    expect(screen.queryByText("Sign out")).not.toBeInTheDocument();
  });

  // The switch itself is appearanceSwitch.test.tsx's, run in both states.
  it("keeps a visitor's choice for their next visit", () => {
    const { unmount } = renderHeader();
    fireEvent.click(screen.getByRole("radio", { name: "Dark" }));
    unmount();
    document.documentElement.removeAttribute("data-theme");

    renderHeader();
    expect(document.documentElement.getAttribute("data-theme")).toBe("dark");
    expect(screen.getByRole("radio", { name: "Dark" })).toHaveAttribute("aria-checked", "true");
  });
});

describe("signing in from an open page and changing one's mind", () => {
  function Where() {
    const { pathname, search } = useLocation();
    return <output aria-label="location">{`${useNavigationType()} ${pathname}${search}`}</output>;
  }

  it("returns to the page the sign-in link was on, as it was left", () => {
    stubBrowser();
    render(
      <MemoryRouter initialEntries={["/leaderboard?page=2"]}>
        <ThemeProvider>
          <Routes>
            <Route path="/leaderboard" element={<Header title="Leaderboard" />} />
            <Route path="/login" element={<LoginPage />} />
          </Routes>
          <Where />
        </ThemeProvider>
      </MemoryRouter>
    );

    fireEvent.click(screen.getByRole("link", { name: "Sign in" }));
    fireEvent.click(screen.getByRole("button", { name: "Back" }));

    // A pop rather than a fresh visit, so the browser's own Back does not
    // then lead to the login card again.
    expect(screen.getByLabelText("location")).toHaveTextContent("POP /leaderboard?page=2");
  });
});
