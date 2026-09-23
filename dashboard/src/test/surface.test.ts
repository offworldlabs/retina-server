import { describe, it, expect, vi, beforeEach, afterEach, type MockInstance } from "vitest";
import { isAdminHost as isAdmin, resolveSurface, warnIfModeIgnored, MODE_IGNORED_WARNING } from "../utils/surface";

describe("surface selection by hostname", () => {
  it("selects the admin console on every environment's admin vhost", () => {
    expect(isAdmin("admin.retina.fm")).toBe(true);
    expect(isAdmin("staging-admin.retina.fm")).toBe(true);
    expect(isAdmin("test-admin.retina.fm")).toBe(true);
    // The dev server's, which answers on every `*.localhost` name.
    expect(isAdmin("admin.localhost")).toBe(true);
  });

  // The point of matching any prefix rather than the three that exist today.
  it("selects it on an environment that does not exist yet", () => {
    expect(isAdmin("dev-admin.retina.fm")).toBe(true);
    expect(isAdmin("pr-1234-admin.retina.fm")).toBe(true);
  });

  it("leaves every other vhost on the user dashboard", () => {
    for (const host of [
      "dash.retina.fm",
      "staging-dash.retina.fm",
      "test-dash.retina.fm",
      "map.retina.fm",
      "towers.retina.fm",
      // The consolidated app vhost, where this bundle is served at the root.
      // It renders the user dashboard and never the admin console: Access
      // gates a hostname, and the admin hostname is deliberately not this one.
      "app.retina.fm",
      "staging-app.retina.fm",
      "test-app.retina.fm",
      "api.retina.fm",
      "dash.localhost",
      "app.localhost",
      "localhost",
      "127.0.0.1",
      "192.168.1.42",
      "100.101.102.103",
      "169.254.1.1",
      "mymac.local",
    ]) {
      expect(isAdmin(host)).toBe(false);
    }
  });

  it("requires the label to be exactly `admin`", () => {
    expect(isAdmin("administration.retina.fm")).toBe(false);
    expect(isAdmin("dash.admin.retina.fm")).toBe(false);
    expect(isAdmin("admin-tools.retina.fm")).toBe(false);
  });
});

describe("?mode=admin", () => {
  // The dev server's hosts among them: it answers on admin.localhost, so the
  // hostname selects the surface there as everywhere else.
  const NON_ADMIN = [
    "localhost",
    "127.0.0.1",
    "[::1]",
    "app.localhost",
    "dash.localhost",
    "192.168.1.42",
    "mymac.local",
    "app.retina.fm",
    "staging-app.retina.fm",
    "dash.retina.fm",
  ];

  it("selects nothing on any host", () => {
    for (const host of NON_ADMIN) {
      expect(resolveSurface(host, "?mode=admin").isAdmin).toBe(false);
    }
  });

  it("is reported wherever it selected nothing", () => {
    for (const host of NON_ADMIN) {
      expect(resolveSurface(host, "?mode=admin").modeParamIgnored).toBe(true);
    }
  });

  it("is not reported on an admin vhost, which renders the console regardless", () => {
    for (const host of ["admin.localhost", "admin.retina.fm", "staging-admin.retina.fm"]) {
      expect(resolveSurface(host, "?mode=admin")).toEqual({ isAdmin: true, modeParamIgnored: false });
    }
  });

  it("is reported only for the exact value `admin`", () => {
    for (const search of ["", "?mode=", "?mode=Admin", "?mode=ADMIN", "?mode=admins", "?admin", "?mode=user"]) {
      expect(resolveSurface("localhost", search).modeParamIgnored).toBe(false);
    }
  });

  it("never lets the query string override a real admin vhost", () => {
    expect(resolveSurface("admin.retina.fm", "?mode=user").isAdmin).toBe(true);
    expect(resolveSurface("staging-admin.retina.fm", "?mode=").isAdmin).toBe(true);
  });
});

describe("warnIfModeIgnored", () => {
  let warn: MockInstance<typeof console.warn>;

  beforeEach(() => {
    warn = vi.spyOn(console, "warn").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("explains why the parameter was ignored", () => {
    warnIfModeIgnored(true);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(warn).toHaveBeenCalledWith(MODE_IGNORED_WARNING);
  });

  it("says nothing when it was not ignored", () => {
    warnIfModeIgnored(false);
    expect(warn).not.toHaveBeenCalled();
  });
});
