import { describe, it, expect, afterEach, beforeEach, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Link, MemoryRouter, Route, Routes, useLocation, useNavigationType } from "react-router-dom";

import Header from "../components/Header";
import RequireAuth from "../components/RequireAuth";
import { AuthProvider } from "../context/AuthContext";
import { ThemeProvider } from "../context/ThemeContext";

/**
 * Where signing out leaves a caller.
 *
 * An open page stays as it was, now signed out. A page that wants a session
 * gives way to the map rather than the sign-in card: the caller asked to leave,
 * not to sign in again. The real AuthProvider and RequireAuth are used, since
 * the question is how the cleared identity and the route change meet the guard.
 */

/** As in signedOutChrome.test.tsx: jsdom has no matchMedia, and Node 20 and 26
 *  disagree about window.localStorage. */
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

/** `signOutAnswered` holds the sign-out response back until it settles. */
function stubServer(signOutAnswered: Promise<void> = Promise.resolve()) {
  const json = (body: unknown) =>
    new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      if (url === "/api/auth/me") return json({ id: "u1", name: "Ada", email: "ada@example.com", role: "admin" });
      if (url === "/api/auth/logout") return signOutAnswered.then(() => json({ ok: true }));
      return json({});
    })
  );
}

function Where() {
  const { pathname, search } = useLocation();
  return <output aria-label="location">{`${useNavigationType()} ${pathname}${search}`}</output>;
}

const signInCard = vi.fn();
function SignInCard() {
  signInCard();
  return <h1>Sign in to RETINA</h1>;
}

function renderConsole(entry: string, isAdmin = false) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <ThemeProvider>
        <AuthProvider>
          <Routes>
            <Route path="/login" element={<SignInCard />} />
            <Route
              path="/*"
              element={
                <RequireAuth isAdmin={isAdmin}>
                  <Header title="Console" isAdmin={isAdmin} />
                  <Link to="/data">Open data</Link>
                  <Routes>
                    <Route path="map" element={<h1>Map</h1>} />
                    <Route path="data" element={<h1>Data</h1>} />
                    <Route path="overview" element={<h1>Overview</h1>} />
                    <Route path="nodes" element={<h1>Nodes</h1>} />
                  </Routes>
                </RequireAuth>
              }
            />
          </Routes>
          <Where />
        </AuthProvider>
      </ThemeProvider>
    </MemoryRouter>
  );
}

async function signOut() {
  fireEvent.click(await screen.findByText("Ada"));
  fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
}

describe("signing out", () => {
  beforeEach(() => {
    stubBrowser();
    stubServer();
    signInCard.mockClear();
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("leaves an open page where it was, signed out", async () => {
    renderConsole("/data?day=2026-09-17");
    await signOut();

    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Data" })).toBeInTheDocument();
    expect(screen.getByLabelText("location")).toHaveTextContent("POP /data?day=2026-09-17");
  });

  it("takes a page that wants a session to the map, never past the sign-in card", async () => {
    renderConsole("/overview");
    await signOut();

    expect(await screen.findByRole("heading", { name: "Map" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
    // Replaced, so Back does not lead to a page that would bounce to sign-in.
    expect(screen.getByLabelText("location")).toHaveTextContent("REPLACE /map");
    expect(signInCard).not.toHaveBeenCalled();
  });

  it("judges the page the caller is on when the session ends, not the one they clicked on", async () => {
    let answer = () => {};
    stubServer(new Promise<void>((resolve) => (answer = resolve)));
    renderConsole("/overview");
    await signOut();

    fireEvent.click(screen.getByRole("link", { name: "Open data" }));
    answer();

    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
    expect(screen.getByLabelText("location")).toHaveTextContent("PUSH /data");
    expect(signInCard).not.toHaveBeenCalled();
  });

  it("leaves the admin console on its sign-in card, having no map to go to", async () => {
    renderConsole("/nodes", true);
    await signOut();

    expect(await screen.findByRole("heading", { name: "Sign in to RETINA" })).toBeInTheDocument();
    expect(screen.getByLabelText("location")).toHaveTextContent("/login");
  });
});
