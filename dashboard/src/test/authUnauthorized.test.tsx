import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { api, UnauthorizedError } from "../api/client";
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

function stubLocation(pathname: string) {
  const loc = { pathname, href: `https://dash.retina.fm${pathname}` };
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

  it("sends a caller elsewhere in the app to the login page", async () => {
    const loc = stubLocation("/nodes");
    await expect(api.me()).rejects.toBeInstanceOf(UnauthorizedError);
    expect(loc.href).toBe("/login");
  });

  it.each(["/login", "/login/"])(
    "does not navigate when the caller is already on the login page (%s)",
    async (pathname) => {
      // Both spellings, because the router matches them to one route and only
      // the raw string comparison would tell them apart.
      const loc = stubLocation(pathname);
      const untouched = loc.href;
      await expect(api.me()).rejects.toBeInstanceOf(UnauthorizedError);
      expect(loc.href).toBe(untouched);
    }
  );
});

describe("the API client on a 401, mounted under /dash/", () => {
  // The login path is derived from Vite's `base`, fixed at build time, so the
  // mounted build can only be exercised by re-importing under a stubbed one.
  async function loadMounted() {
    vi.resetModules();
    vi.stubEnv("BASE_URL", "/dash/");
    return import("../api/client");
  }

  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(null, { status: 401 })));
  });

  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
    vi.resetModules();
    Object.defineProperty(window, "location", {
      value: realLocation,
      writable: true,
      configurable: true,
    });
  });

  it("sends a caller to the login page inside the mount", async () => {
    // A bare /login here is the app vhost's root, which serves the MAP bundle:
    // a 200, a working page, and the wrong application.
    const mounted = await loadMounted();
    const loc = stubLocation("/dash/nodes");
    await expect(mounted.api.me()).rejects.toBeInstanceOf(mounted.UnauthorizedError);
    expect(loc.href).toBe("/dash/login");
  });

  it.each(["/dash/login", "/dash/login/"])(
    "still recognises the login page it is already on (%s)",
    async (pathname) => {
      // window.location.pathname carries the mount, so a guard comparing it
      // against an unmounted /login never matches and the reload loop returns.
      const mounted = await loadMounted();
      const loc = stubLocation(pathname);
      const untouched = loc.href;
      await expect(mounted.api.me()).rejects.toBeInstanceOf(mounted.UnauthorizedError);
      expect(loc.href).toBe(untouched);
    }
  );
});

function Probe() {
  const { user, loading } = useAuth();
  if (loading) return <span>deciding</span>;
  return <span>{user ? "signed in" : "signed out"}</span>;
}

describe("AuthProvider on a 401", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("settles as signed out rather than retrying", async () => {
    // The retries exist for a busy server. Spending them on a 401 leaves the
    // login card behind a loading state for ~9s, which reads as a hung page.
    vi.spyOn(api, "me").mockRejectedValue(new UnauthorizedError());
    render(
      <AuthProvider>
        <Probe />
      </AuthProvider>
    );
    await waitFor(() => expect(screen.getByText("signed out")).toBeInTheDocument());
    expect(api.me).toHaveBeenCalledTimes(1);
  });
});
