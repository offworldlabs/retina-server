import { describe, it, expect, beforeEach, vi, type Mock } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Routes, Route, useLocation } from "react-router-dom";

import App from "../App";
import RequireAuth from "../components/RequireAuth";
import { ThemeProvider } from "../context/ThemeContext";
import { useConsoleRoutes } from "../routes";
import { appConsoleUrl } from "../utils/siblings";
import { stubMatchMedia } from "./matchMedia";

type User = { id?: string; name?: string; email?: string; role?: string };
type SignOut = (leave?: () => void) => Promise<{ redirected: boolean }>;

const state = vi.hoisted(() => ({
  auth: {
    user: null as User | null,
    loading: false,
    syntheticFleet: false,
    signingOut: false,
    logout: (async () => ({ redirected: false })) as SignOut,
  },
  listeners: new Set<() => void>(),
}));

// Read as a store, so that a change made while a page is up redraws whoever
// reads it, as the provider's does.
vi.mock("../context/AuthContext", async () => {
  const { useSyncExternalStore } = await import("react");
  const subscribe = (listener: () => void) => {
    state.listeners.add(listener);
    return () => {
      state.listeners.delete(listener);
    };
  };
  return { useAuth: () => useSyncExternalStore(subscribe, () => state.auth) };
});
// Passed through, and counted.
vi.mock("../routes", async (importOriginal) => {
  const routes = await importOriginal<typeof import("../routes")>();
  return { ...routes, useConsoleRoutes: vi.fn(routes.useConsoleRoutes) };
});
// App resolves its surface once, at module scope; the cases that render it
// below are all on the admin console.
vi.mock("../utils/surface", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../utils/surface")>()),
  resolveSurface: () => ({ isAdmin: true, modeParamIgnored: false }),
}));
vi.mock("../pages/admin/NodeManagementPage", () => ({ default: () => <div>node list</div> }));

function setAuth(next: Partial<typeof state.auth>) {
  state.auth = { ...state.auth, ...next };
  for (const listener of state.listeners) listener();
}

/** Signs `user` in. Its sign-out is pending until `logout` settles, as the
 *  provider's is, whoever started it. */
function signIn(user: User | null, logout: Mock<SignOut> = vi.fn(async () => ({ redirected: false }))) {
  const send: SignOut = (leave) => {
    setAuth({ signingOut: true });
    const sent = leave ? logout(leave) : logout();
    const settle = () => setAuth({ signingOut: false });
    sent.then(settle, settle);
    return sent;
  };
  state.auth = { user, loading: false, syntheticFleet: false, signingOut: false, logout: send };
  return logout;
}

/** The guard under a router, with a login route to land on so a redirect is
 *  visible as a rendered page rather than as a call on a spy. ThemeProvider
 *  because a refusal is drawn in the console's chrome, whose header reads the
 *  theme. */
function renderAt(pathname: string, isAdmin = false) {
  return render(
    <ThemeProvider>
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
    </ThemeProvider>
  );
}

describe("the route guard", () => {
  beforeEach(() => {
    stubMatchMedia();
    signIn(null);
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
    signIn({ role: "user" });
    renderAt("/onboarding");
    expect(screen.getByText("the page")).toBeInTheDocument();
  });

  // The console has no open routes, whatever they are called in the other tree.
  it("sends a caller with no session away from the admin console", () => {
    renderAt("/leaderboard", true);
    expect(screen.getByText("login card")).toBeInTheDocument();
  });

  it("opens the admin console to an administrator", () => {
    signIn({ id: "u1", name: "Ada", email: "ada@example.com", role: "admin" });
    renderAt("/nodes", true);
    expect(screen.getByText("the page")).toBeInTheDocument();
    expect(screen.queryByText("Access Denied")).toBeNull();
  });

  // Settling first costs one round trip and saves showing a signed-in caller
  // the signed-out chrome before their identity arrives.
  it("waits for an identity that has not settled, on an open route too", () => {
    state.auth = { ...state.auth, user: null, loading: true };
    renderAt("/leaderboard");
    expect(screen.queryByText("the page")).not.toBeInTheDocument();
    expect(screen.queryByText("login card")).not.toBeInTheDocument();
  });
});

/** A signed-in caller who is not an administrator, on the admin console. */
describe("the admin console's refusal", () => {
  const owner = { id: "u2", name: "Grace", email: "grace@example.com", role: "user" };

  beforeEach(() => {
    stubMatchMedia();
    signIn(owner);
    vi.mocked(useConsoleRoutes).mockClear();
  });

  it("is drawn in the console's chrome, under its own title", () => {
    const { container } = renderAt("/nodes", true);
    expect(container.querySelector(".header-title")).toHaveTextContent("Access Denied");
    expect(screen.getByRole("heading", { name: "Access Denied" })).toBeInTheDocument();
    expect(screen.queryByText("the page")).toBeNull();
  });

  it("says why, and which account the caller holds", () => {
    renderAt("/nodes", true);
    expect(screen.getByText("The admin console is for network administrators.")).toBeInTheDocument();
    expect(
      screen.getByText((_, el) => el?.tagName === "P" && el.textContent === "You are signed in as grace@example.com."),
    ).toBeInTheDocument();
  });

  it("leads to the app console in the same environment", () => {
    renderAt("/nodes", true);
    const link = screen.getByRole("link", { name: "Go to the Retina console" });
    expect(link).toHaveAttribute("href", appConsoleUrl(window.location.host, window.location.protocol));
    // An absolute address: the admin console has no page to route it to.
    expect(link.getAttribute("href")).toMatch(/^https?:\/\//);
  });

  // The guard sends the caller to the sign-in card once the session is gone,
  // so nothing is asked of `leave`.
  it("signs the caller out", async () => {
    const logout = signIn(owner);
    renderAt("/nodes", true);
    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Sign out" })));
    expect(logout).toHaveBeenCalledTimes(1);
    expect(logout).toHaveBeenCalledWith();
  });

  it("takes one sign-out at a time", async () => {
    let answer = (_: { redirected: boolean }) => {};
    const logout = signIn(owner, vi.fn(() => new Promise<{ redirected: boolean }>((resolve) => (answer = resolve))));
    renderAt("/nodes", true);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    const pending = screen.getByRole("button", { name: "Signing out…" });
    expect(pending).toBeDisabled();
    fireEvent.click(pending);
    expect(logout).toHaveBeenCalledTimes(1);
    await act(async () => answer({ redirected: false }));
    expect(screen.getByRole("button", { name: "Sign out" })).toBeEnabled();
  });

  // The header's menu signs out too, and the provider sends one sign-out at a
  // time, so the refusal's button reads the provider's pending state rather
  // than only its own clicks.
  it("shows a sign-out the header's menu started as pending", async () => {
    let answer = (_: { redirected: boolean }) => {};
    const logout = signIn(owner, vi.fn(() => new Promise<{ redirected: boolean }>((resolve) => (answer = resolve))));
    renderAt("/nodes", true);

    fireEvent.keyDown(screen.getByRole("button", { name: "Account: Grace" }), { key: "Enter" });
    fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));

    expect(screen.getByRole("button", { name: "Signing out…" })).toBeDisabled();
    expect(logout).toHaveBeenCalledTimes(1);
    await act(async () => answer({ redirected: false }));
    expect(screen.getByRole("button", { name: "Sign out" })).toBeEnabled();
  });

  // It names itself, and draws none of the console's pages, so there is
  // nothing to read the route table for.
  it("builds no route table for its title", () => {
    renderAt("/nodes", true);
    expect(screen.getByRole("heading", { name: "Access Denied" })).toBeInTheDocument();
    expect(useConsoleRoutes).not.toHaveBeenCalled();
  });

  // Every entry would lead to the same refusal, and the narrow screen's Menu
  // would open an empty drawer.
  it("offers none of the console's pages", () => {
    const { container } = renderAt("/nodes", true);
    expect(screen.queryByRole("navigation")).toBeNull();
    expect(container.querySelector(".nav-item")).toBeNull();
    for (const label of ["Network Health", "Nodes", "Users", "Configuration"]) {
      expect(screen.queryByRole("link", { name: label })).toBeNull();
    }
    expect(screen.queryByRole("button", { name: "Menu" })).toBeNull();
  });

  it("keeps the sidebar's brand and its collapse toggle", () => {
    renderAt("/nodes", true);
    expect(screen.getByText("Admin Console")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /collapse sidebar/i })).toBeInTheDocument();
  });

  // The pane is flush only for the map pages it would otherwise hold.
  it("keeps the pane's padding at an address that is a full-bleed page", () => {
    const { container } = renderAt("/sim", true);
    expect(container.querySelector(".content")).not.toBeNull();
    expect(container.querySelector(".content.flush")).toBeNull();
    expect(container.querySelector(".dashboard.sidebar-collapsed")).toBeNull();
  });
});

describe("the admin console as a whole", () => {
  function visit(path: string) {
    return render(
      <ThemeProvider>
        <MemoryRouter initialEntries={[path]}>
          <App />
        </MemoryRouter>
      </ThemeProvider>
    );
  }

  beforeEach(() => stubMatchMedia());

  // The guard draws the refusal in place of the console's own layout, not
  // inside it.
  it("draws its refusal in one layout, and loads none of its pages", () => {
    signIn({ id: "u2", name: "Grace", email: "grace@example.com", role: "user" });
    const { container } = visit("/nodes");
    expect(container.querySelectorAll(".dashboard")).toHaveLength(1);
    expect(container.querySelectorAll(".header")).toHaveLength(1);
    expect(container.querySelector(".header-title")).toHaveTextContent("Access Denied");
    expect(screen.queryByText("node list")).toBeNull();
  });

  it("draws the page to an administrator, under the page's own title", async () => {
    signIn({ id: "u1", name: "Ada", email: "ada@example.com", role: "admin" });
    const { container } = visit("/nodes");
    expect(await screen.findByText("node list")).toBeInTheDocument();
    expect(container.querySelector(".header-title")).toHaveTextContent("Node Management");
    expect(screen.getByRole("link", { name: "Network Health" })).toBeInTheDocument();
  });
});
