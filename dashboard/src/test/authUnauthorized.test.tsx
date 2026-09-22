import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { UnauthorizedError } from "@retina/shared";
import { api } from "../api/client";
import { AuthProvider, useAuth } from "../context/AuthContext";

/**
 * A 401 is an answer, not a failure to get one.
 *
 * The client used to navigate to /login on every 401. On /login that is a
 * same-URL assignment, which reloads; the reload re-runs the auth call, which
 * 401s again. Where nothing can mint a session the page never settles, and the
 * login card only flashes between reloads.
 */

const realLocation = window.location;

function stubLocation(pathname: string, hostname = "app.retina.fm") {
  // hostname and search because the surface is resolved from them: the same
  // path is open on the user dashboard and gated on the admin console.
  const loc = { pathname, hostname, search: "", href: `https://${hostname}${pathname}` };
  Object.defineProperty(window, "location", { value: loc, writable: true, configurable: true });
  return loc;
}

describe("the API client on a 401", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 401 })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    Object.defineProperty(window, "location", {
      value: realLocation,
      writable: true,
      configurable: true,
    });
  });

  // With the page it was on, so signing in again returns there.
  it("sends a caller elsewhere in the app to the login page", async () => {
    const loc = stubLocation("/nodes/ret-0042");
    await expect(api.myNodes()).rejects.toBeInstanceOf(UnauthorizedError);
    expect(loc.href).toBe("/login?next=%2Fnodes%2Fret-0042");
  });

  // The mailed link opens on the app host, where no admin route exists.
  it("sends a caller on the admin console there without its page", async () => {
    const loc = stubLocation("/nodes", "admin.retina.fm");
    await expect(api.myNodes()).rejects.toBeInstanceOf(UnauthorizedError);
    expect(loc.href).toBe("/login");
  });

  it.each(["/login", "/login/"])(
    "does not navigate when the caller is already on the login page (%s)",
    async (pathname) => {
      // Both spellings, because the router matches them to one route and only
      // the raw string comparison would tell them apart.
      const loc = stubLocation(pathname);
      const untouched = loc.href;
      await expect(api.myNodes()).rejects.toBeInstanceOf(UnauthorizedError);
      expect(loc.href).toBe(untouched);
    }
  );
});

describe("the API client on a 401, on a page that needs no session", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 401 })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    Object.defineProperty(window, "location", {
      value: realLocation,
      writable: true,
      configurable: true,
    });
  });

  // Whatever 401s, the visitor is entitled to the rest of the page. Throwing
  // them at a login they may have no way through is the one outcome a public
  // route exists to prevent.
  it("leaves the caller on the page", async () => {
    const loc = stubLocation("/leaderboard");
    const untouched = loc.href;
    await expect(api.myNodes()).rejects.toBeInstanceOf(UnauthorizedError);
    expect(loc.href).toBe(untouched);
  });

  it("still navigates from the same path on the admin console", async () => {
    const loc = stubLocation("/leaderboard", "admin.retina.fm");
    await expect(api.myNodes()).rejects.toBeInstanceOf(UnauthorizedError);
    expect(loc.href).toBe("/login");
  });
});

function Probe() {
  const { user, loading } = useAuth();
  if (loading) return <span>deciding</span>;
  return <span>{user ? "signed in" : "signed out"}</span>;
}

describe("AuthProvider on a 401", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 401 })));
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    Object.defineProperty(window, "location", {
      value: realLocation,
      writable: true,
      configurable: true,
    });
  });

  it("settles as signed out", async () => {
    stubLocation("/nodes");
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );
    await waitFor(() => expect(screen.getByText("signed out")).toBeInTheDocument());
  });

  it("leaves the page where it is, so the guard can route", async () => {
    // The identity call goes straight to the shared client, around the wrapper
    // above. Its redirect would reload the document to reach a route the
    // router is about to render anyway, throwing away the bundle mid-boot.
    const loc = stubLocation("/nodes");
    const untouched = loc.href;
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );
    await waitFor(() => expect(screen.getByText("signed out")).toBeInTheDocument());
    expect(loc.href).toBe(untouched);
  });
});
