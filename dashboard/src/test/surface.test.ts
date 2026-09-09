import { describe, it, expect, vi, beforeEach, afterEach, type MockInstance } from "vitest";
import { resolveSurface, warnIfModeIgnored, MODE_IGNORED_WARNING } from "../utils/surface";

const isAdmin = (hostname: string, search = "") =>
  resolveSurface(hostname, search).isAdmin;

describe("surface selection by hostname", () => {
  it("selects the admin console on every environment's admin vhost", () => {
    expect(isAdmin("admin.retina.fm")).toBe(true);
    expect(isAdmin("staging-admin.retina.fm")).toBe(true);
    expect(isAdmin("test-admin.retina.fm")).toBe(true);
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
      "api.retina.fm",
      "dash.localhost",
      // A dev host with no query stays on the user dashboard too: that branch
      // returns wantsAdmin, and flipping it to true would put every LAN and
      // Tailscale visitor on the admin console by default.
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

describe("?mode=admin override", () => {
  it("is honoured wherever the dev server can be reached", () => {
    for (const host of ["localhost", "127.0.0.1", "[::1]", "dash.localhost"]) {
      expect(isAdmin(host, "?mode=admin")).toBe(true);
    }
  });

  // `vite --host` binds 0.0.0.0, so a phone on the LAN arrives on a private
  // address and still needs a way in.
  it("is honoured on a private address", () => {
    for (const host of [
      "192.168.1.42",
      "10.0.0.7",
      "172.16.0.1",
      "172.31.255.254",
      "100.101.102.103", // CGNAT, so Tailscale
      "169.254.1.1", // link-local
    ]) {
      expect(isAdmin(host, "?mode=admin")).toBe(true);
    }
  });

  // mDNS is how a Mac on the LAN is reached, and must not be confused with
  // the `.localhost` case above.
  it("is honoured on an mDNS name", () => {
    expect(isAdmin("mymac.local", "?mode=admin")).toBe(true);
  });

  it("is not honoured outside those ranges", () => {
    for (const host of ["172.15.0.1", "172.32.0.1", "100.63.0.1", "100.128.0.1", "notlocalhost.retina.fm"]) {
      expect(isAdmin(host, "?mode=admin")).toBe(false);
    }
  });

  // location.hostname brackets IPv6 and may append a %25-escaped zone id.
  it("is honoured on an IPv6 dev address", () => {
    for (const host of ["[fe80::1]", "[fe80::1%25en0]", "[fd12:3456::1]"]) {
      expect(isAdmin(host, "?mode=admin")).toBe(true);
    }
  });

  it("is not honoured on a public IPv6 address", () => {
    expect(isAdmin("[2001:db8::1]", "?mode=admin")).toBe(false);
  });

  it("is not honoured on an out-of-range octet", () => {
    for (const host of ["10.999.999.999", "192.168.300.1", "169.254.999.0"]) {
      expect(isAdmin(host, "?mode=admin")).toBe(false);
    }
  });

  // The ranges match addresses, not names that merely start the same way.
  it("is not honoured on a registrable domain shaped like a private address", () => {
    for (const host of ["192.168.evil.com", "10.attacker.net", "172.16.example.org"]) {
      expect(isAdmin(host, "?mode=admin")).toBe(false);
    }
  });

  // The production hole this closes: dash and admin are one build served from
  // one nginx root, so without this the console renders on the dash vhost.
  it("is ignored on a deployed non-admin vhost, and says so", () => {
    for (const host of ["dash.retina.fm", "staging-dash.retina.fm"]) {
      const surface = resolveSurface(host, "?mode=admin");
      expect(surface.isAdmin).toBe(false);
      expect(surface.modeParamIgnored).toBe(true);
    }
  });

  it("only warns when the parameter was actually asked for", () => {
    expect(resolveSurface("dash.retina.fm", "").modeParamIgnored).toBe(false);
    expect(resolveSurface("admin.retina.fm", "?mode=admin").modeParamIgnored).toBe(false);
    expect(resolveSurface("localhost", "?mode=admin").modeParamIgnored).toBe(false);
  });

  // Nothing but the exact value selects the admin surface, so loosening the
  // comparison to a truthiness or case-insensitive test would fail here.
  it("takes only the exact value `admin`", () => {
    for (const search of ["?mode=", "?mode=Admin", "?mode=ADMIN", "?mode=admins", "?admin", "?mode=user"]) {
      expect(isAdmin("localhost", search)).toBe(false);
    }
  });

  it("never lets the query string override a real admin vhost", () => {
    expect(isAdmin("admin.retina.fm", "?mode=user")).toBe(true);
    expect(isAdmin("staging-admin.retina.fm", "?mode=")).toBe(true);
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
