import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigationType } from "react-router-dom";

import Sidebar from "../components/Sidebar";
import Header from "../components/Header";
import LoginPage from "../pages/LoginPage";
import { ThemeProvider } from "../context/ThemeContext";
import { PUBLIC_PATHS, isPublicRoute } from "../utils/publicRoutes";
import { stubMatchMedia } from "./matchMedia";

const state = vi.hoisted(() => ({
  auth: {
    user: null as { name: string; email: string } | null,
    loading: false,
    logout: async () => ({ redirected: false }),
  },
}));

vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

const signedIn = { name: "Ada", email: "ada@example.com" };

beforeEach(() => {
  state.auth = {
    user: null,
    loading: false,
    logout: async () => ({ redirected: false }),
  };
});

describe("the sidebar shown to a caller with no session", () => {
  function renderSidebar() {
    render(
      <MemoryRouter>
        <Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />
      </MemoryRouter>
    );
  }

  /** Every entry on screen, live or not, in order. */
  const shownLabels = () =>
    screen
      .getAllByRole("link")
      .map((a) => a.textContent?.trim())
      .filter(Boolean);

  const lockedLabels = () =>
    screen
      .getAllByRole("link")
      .filter((a) => a.classList.contains("locked"))
      .map((a) => a.textContent?.trim());

  // So a visitor can see what signing in would open, rather than a nav that
  // changes shape underneath them when they do.
  it("lists what the signed-in nav lists", () => {
    state.auth = { ...state.auth, user: signedIn };
    const { unmount } = render(
      <MemoryRouter>
        <Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />
      </MemoryRouter>
    );
    const signedInLabels = shownLabels();
    unmount();

    state.auth = { ...state.auth, user: null };
    renderSidebar();
    expect(shownLabels()).toEqual(signedInLabels);
  });

  // To the sign-in card rather than the page, which would only bounce there.
  it.each(["Overview", "My Nodes"])("greys out %s, which needs a session, and leads to sign-in", (label) => {
    renderSidebar();
    const entry = screen.getByRole("link", { name: label });
    expect(entry).toHaveClass("locked");
    expect(entry).toHaveAttribute("href", "/login");
    expect(entry).toHaveAttribute("title", `${label} is only available when signed in`);
  });

  // Read off the guard's own list, so opening a route and making its entry
  // live are the same edit.
  it("leaves every open route live, pointing at its own page", () => {
    renderSidebar();
    const hrefs = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
    for (const path of PUBLIC_PATHS) expect(hrefs).toContain(path);
    for (const label of ["Map", "Data Explorer", "Leaderboard", "Knowledge Base"]) {
      expect(screen.getByRole("link", { name: label })).not.toHaveClass("locked");
    }
  });

  it("leaves Tower Finder live, as another site with its own access", () => {
    renderSidebar();
    const entry = screen.getByRole("link", { name: /Tower Finder/ });
    expect(entry).not.toHaveClass("locked");
    expect(entry).toHaveAttribute("target", "_blank");
  });

  // Every entry, read off the signed-in nav rather than listed here, so a
  // page added to or taken out of the nav needs no edit to this test.
  it("greys out exactly the entries whose page needs a session", () => {
    state.auth = { ...state.auth, user: signedIn };
    const { unmount } = render(
      <MemoryRouter>
        <Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />
      </MemoryRouter>
    );
    const needsSession = screen
      .getAllByRole("link")
      .filter((a) => {
        const href = a.getAttribute("href") ?? "";
        return href.startsWith("/") && !isPublicRoute(href, false);
      })
      .map((a) => a.textContent?.trim());
    unmount();

    state.auth = { ...state.auth, user: null };
    renderSidebar();
    expect(needsSession).toContain("Overview");
    expect(lockedLabels()).toEqual(needsSession);
  });

  it("greys out nothing once there is a session", () => {
    state.auth = { ...state.auth, user: signedIn };
    renderSidebar();
    expect(lockedLabels()).toEqual([]);
    expect(screen.getByRole("link", { name: "My Nodes" })).toHaveAttribute("href", "/onboarding");
  });
});

describe("the header shown to a caller with no session", () => {
  beforeEach(() => {
    stubMatchMedia();
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
    stubMatchMedia();
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

  it("returns there from a greyed-out nav entry too", () => {
    stubMatchMedia();
    render(
      <MemoryRouter initialEntries={["/leaderboard?page=2"]}>
        <ThemeProvider>
          <Routes>
            <Route
              path="/leaderboard"
              element={<Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />}
            />
            <Route path="/login" element={<LoginPage />} />
          </Routes>
          <Where />
        </ThemeProvider>
      </MemoryRouter>
    );

    fireEvent.click(screen.getByRole("link", { name: "My Nodes" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("PUSH /login");

    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByLabelText("location")).toHaveTextContent("POP /leaderboard?page=2");
  });
});

describe("signing in from an open page and going through with it", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
  });

  /** Open `/leaderboard` drawing `chrome`, click `link`, and ask for a sign-in
   *  link; returns what the request asked the server to mail. */
  async function requestFrom(chrome: React.ReactNode, link: string) {
    stubMatchMedia();
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ status: "accepted" }), { status: 202 }))
    );
    render(
      <MemoryRouter initialEntries={["/leaderboard"]}>
        <ThemeProvider>
          <Routes>
            <Route path="/leaderboard" element={chrome} />
            <Route path="/login" element={<LoginPage />} />
          </Routes>
        </ThemeProvider>
      </MemoryRouter>
    );
    fireEvent.click(screen.getByRole("link", { name: link }));
    fireEvent.change(screen.getByLabelText("Email address"), { target: { value: "ada@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: /sign-in link/i }));
    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    return JSON.parse((fetch as ReturnType<typeof vi.fn>).mock.calls[0][1].body);
  }

  it("asks for a link to the greyed-out page that was clicked", async () => {
    const body = await requestFrom(<Sidebar isAdmin={false} collapsed={false} onToggle={() => {}} />, "My Nodes");
    expect(body).toEqual({ email: "ada@example.com", next: "/onboarding" });
  });

  it("asks for a link back to the page the header's Sign in was on", async () => {
    const body = await requestFrom(<Header title="Leaderboard" />, "Sign in");
    expect(body).toEqual({ email: "ada@example.com", next: "/leaderboard" });
  });
});
