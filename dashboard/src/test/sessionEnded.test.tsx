import { useState, type ReactNode } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi, type Mock } from "vitest";

import { api, onSessionEnded } from "../api/client";
import DashboardLayout from "../components/DashboardLayout";
import RequireAuth from "../components/RequireAuth";
import { AuthProvider, useAuth } from "../context/AuthContext";
import { ThemeProvider } from "../context/ThemeContext";
import { useOwnedNodes } from "../hooks/useOwnedNodes";
import { useNodes } from "../pages/map/hooks";
import DataExplorerPage from "../pages/user/DataExplorerPage";
import { ownedNode } from "./fixtures/ownedNode";
import { stubMatchMedia } from "./matchMedia";

/**
 * A session that ends somewhere else: signed out in another tab, expired, or
 * revoked on the server. Nothing on this page asked to sign out, so the first
 * it hears of it is an owner route answering 401.
 *
 * Off an open page the client sends the caller to the login card, which drops
 * everything with the document. On an open page it cannot, since a visitor is
 * entitled to stay; there it has to drop the identity in place instead, or the
 * page goes on showing what the session fetched (a private node's files, its
 * true position) to whoever now holds the screen. The real AuthProvider,
 * client, owned-nodes hook and console layout are used, with fetch stubbed.
 */

const DAY = "2026-09-17";
const ADA = { id: "u-ada", name: "Ada", email: "ada@example.com", role: "user" };

// Roof is private: only the owner's routes list it, at its true position.
const ROOF = ownedNode({ node_id: "retroof0001", node_ref: "nderoof00000001", name: "Roof", location_private: true });
const MAST = ownedNode({ node_id: "retmast0001", node_ref: "ndemast00000001", name: "Mast" });
const ROOF_REF = ROOF.node_ref!;
const MAST_REF = MAST.node_ref!;
// Someone else's node, on the network only.
const HILL = "ndehill00000001";

/** A node's analytics entry, as both the owner's and the public map read it. */
const coverage = (lat: number, lon: number, uncertaintyKm?: number) => ({
  detection_area: {
    rx: { lat, lon, location_uncertainty_km: uncertaintyKm },
    tx: { lat: lat + 0.1, lon: lon + 0.1 },
    beam_azimuth_deg: 90,
    beam_width_deg: 40,
    max_range_km: 50,
  },
});

/** The archive's one file a day per node, named after it: part-roof.parquet. */
const keyOf = (day: string, ref: string) =>
  `year=${day.slice(0, 4)}/month=${day.slice(5, 7)}/day=${day.slice(8, 10)}/node_ref=${ref}/part-${ref.slice(3, 7)}.parquet`;

function listing(path: string, refs: string[]) {
  const q = new URL(path, "http://localhost").searchParams;
  const day = (q.get("date") ?? "").replace(/\//g, "-");
  const only = q.get("node_ref");
  const files = refs
    .filter((ref) => !only || ref === only)
    .map((ref) => ({ key: keyOf(day, ref), size_bytes: 1024, modified: `${day}T06:00:00Z` }));
  return { files, count: files.length, total: files.length };
}

function json(status: number, body: unknown) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const isMe = (path: string) => path === "/api/auth/me" || path.startsWith("/api/auth/me/") || path.startsWith("/api/auth/me?");

/** The server. Ada's session holds until `live` goes false, after which every
 *  route behind it answers 401, as it does once she signs out elsewhere. */
const server = {
  live: true,
  logout: (): Promise<Response> => Promise.resolve(json(200, { ok: true })),
  /** Set to hold the owner's list, so a request can be left in flight. */
  hold: null as Promise<Response> | null,
};

function route(path: string): Response | Promise<Response> {
  if (path === "/api/auth/logout") return server.logout();
  if (isMe(path)) {
    if (path === "/api/auth/me/nodes" && server.hold) return server.hold;
    if (!server.live) return json(401, { detail: "Not authenticated" });
    if (path === "/api/auth/me") return json(200, ADA);
    if (path === "/api/auth/me/nodes") return json(200, [ROOF, MAST]);
    if (path === "/api/auth/me/analytics") {
      return json(200, { nodes: { [ROOF_REF]: coverage(51.41234, -0.30123), [MAST_REF]: coverage(51.6, -0.5) } });
    }
    if (path.startsWith("/api/auth/me/archive?")) return json(200, listing(path, [ROOF_REF, MAST_REF]));
  }
  if (path === "/api/radar/nodes") {
    const at = (lat: number, lon: number) => ({ rx_lat: lat, rx_lon: lon, location_uncertainty_km: 3 });
    return json(200, {
      nodes: {
        [MAST_REF]: { name: "Mast", status: "online", is_synthetic: false, location: at(51.62, -0.52) },
        [HILL]: { name: "Hilltop", status: "online", is_synthetic: false, location: at(52, -1) },
      },
    });
  }
  // The public map has Mast only, displaced; Roof is not published.
  if (path.startsWith("/api/radar/analytics")) return json(200, { nodes: { [MAST_REF]: coverage(51.62, -0.52, 3) } });
  if (path.startsWith("/api/data/archive?")) return json(200, listing(path, [MAST_REF, HILL]));
  return json(404, { detail: `unexpected request: ${path}` });
}

const paths = () => (fetch as Mock).mock.calls.map(([url]) => String(url));
const asked = (path: string) => paths().filter((p) => p === path).length;
/** Requests to the owner's routes, which only a session answers. */
const ownerAsks = () => paths().filter((p) => p.startsWith("/api/auth/me/")).length;

const realLocation = window.location;

/** The page the client reads to decide whether to send the caller away. */
function stubLocation(pathname: string, hostname = "app.retina.fm") {
  const loc = {
    pathname,
    hostname,
    host: hostname,
    protocol: "https:",
    origin: `https://${hostname}`,
    search: "",
    hash: "",
    href: `https://${hostname}${pathname}`,
    assign: vi.fn(),
    replace: vi.fn(),
    reload: vi.fn(),
  };
  Object.defineProperty(window, "location", { value: loc, writable: true, configurable: true });
  return loc;
}

/** The console as App mounts it, on `entry`, with `page` in the page area. */
function renderConsole(page: ReactNode, entry: string) {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <ThemeProvider>
        <AuthProvider>
          <RequireAuth isAdmin={false}>
            <DashboardLayout isAdmin={false}>{page}</DashboardLayout>
          </RequireAuth>
        </AuthProvider>
      </ThemeProvider>
    </MemoryRouter>,
  );
}

/** A page that shows the owner's nodes, and holds what it saw of them in state
 *  of its own, as a page holds a selection: only a remount clears that. */
function OwnedPage() {
  const { user } = useAuth();
  const owned = useOwnedNodes();
  const names = owned.nodes.map((n) => n.name).join(",");
  const [seen, setSeen] = useState("");
  if (names && names !== seen) setSeen(names);
  return (
    <>
      <output aria-label="identity">{user ? user.email : "nobody"}</output>
      <output aria-label="owned">{`${owned.status} ${names}`.trim()}</output>
      <output aria-label="seen">{seen}</output>
    </>
  );
}

/** The map's node listing, keyed on whoever is signed in, as /map draws it. */
function MapNodes() {
  const { user } = useAuth();
  const nodes = useNodes("real", user);
  return <output aria-label="map">{nodes.map((n) => `${n.node_ref}@${n.rx_lat}`).join(" ")}</output>;
}

const identity = () => screen.getByLabelText("identity");
const owned = () => screen.getByLabelText("owned");

/** Lets a timed poll fire and its answer land. */
const elapse = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

/** Opens the account menu and signs out, as a person would. */
async function signOut() {
  fireEvent.keyDown(await screen.findByRole("button", { name: "Account: Ada" }), { key: "Enter" });
  fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date(`${DAY}T12:00:00Z`));
  stubMatchMedia();
  server.live = true;
  server.logout = () => Promise.resolve(json(200, { ok: true }));
  server.hold = null;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => route(String(url))),
  );
  // usePolling logs every failure, the 401s included.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
});

describe("a session that ends elsewhere, seen from an open page", () => {
  it("drops the identity and the owner's nodes at the next ask, without a page load", async () => {
    const loc = stubLocation("/data");
    const untouched = loc.href;
    renderConsole(<OwnedPage />, "/data");
    await waitFor(() => expect(owned()).toHaveTextContent("ready Roof,Mast"));
    expect(screen.getByLabelText("seen")).toHaveTextContent("Roof,Mast");
    const asksBefore = ownerAsks();

    server.live = false;
    await elapse(60_000);

    expect(identity()).toHaveTextContent("nobody");
    expect(owned()).toHaveTextContent(/^signed-out$/);
    // Started over: what the page held from the session went with it.
    expect(screen.getByLabelText("seen")).toBeEmptyDOMElement();
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
    // Nobody asked to sign out, so nothing is said about a sign-out.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(loc.href).toBe(untouched);
    expect(loc.assign).not.toHaveBeenCalled();
    expect(loc.reload).not.toHaveBeenCalled();
    // The one ask that met the 401, and none after it.
    expect(ownerAsks()).toBe(asksBefore + 1);
    await elapse(10 * 60_000);
    expect(ownerAsks()).toBe(asksBefore + 1);
    expect(asked("/api/auth/me")).toBe(1);
  });

  it("takes the private node's true position off the map", async () => {
    stubLocation("/map");
    renderConsole(<MapNodes />, "/map");
    const map = () => screen.getByLabelText("map");
    await waitFor(() => expect(map()).toHaveTextContent(`${ROOF_REF}@51.41234`));
    expect(map()).toHaveTextContent(`${MAST_REF}@51.6`);

    server.live = false;
    await elapse(30_000);

    // Mast is back where the public listing puts it, and Roof is gone.
    await waitFor(() => expect(map()).toHaveTextContent(new RegExp(`^${MAST_REF}@51\\.62$`)));
    const asksAfter = ownerAsks();
    await elapse(10 * 60_000);
    expect(ownerAsks()).toBe(asksAfter);
    expect(map()).not.toHaveTextContent(ROOF_REF);
  });

  // Reviewer case R8: the Data Explorer kept listing the private node's files.
  it("takes the private node's files off the Data Explorer", async () => {
    stubLocation("/data");
    renderConsole(<DataExplorerPage />, `/data?from=${DAY}&to=${DAY}`);
    expect((await screen.findAllByText(/part-roof/)).length).toBeGreaterThan(0);

    server.live = false;
    await elapse(61_000);

    await waitFor(() => expect(screen.queryAllByText(/part-roof/)).toHaveLength(0));
    expect(screen.getByRole("link", { name: "Sign in" })).toBeInTheDocument();
    // Redrawn as a visitor's: the network's archive, which never lists Roof.
    await waitFor(() => expect(screen.queryAllByText(/part-hill/).length).toBeGreaterThan(0));
    expect(paths().filter((p) => p.startsWith("/api/data/archive?") && p.includes(ROOF_REF))).toEqual([]);
    const asksAfter = ownerAsks();
    await elapse(10 * 60_000);
    expect(ownerAsks()).toBe(asksAfter);
  });
});

describe("a session that ends elsewhere, seen from a page that needs one", () => {
  // As before: off to the login card with the page to return to. The identity
  // stays put meanwhile, so the guard does not start a navigation of its own
  // that would race the load.
  it("still sends the caller to sign in again", async () => {
    const loc = stubLocation("/overview");
    const told = vi.fn();
    const stop = onSessionEnded(told);
    renderConsole(<OwnedPage />, "/overview");
    await waitFor(() => expect(owned()).toHaveTextContent("ready Roof,Mast"));

    server.live = false;
    await elapse(60_000);

    expect(loc.href).toBe("/login?next=%2Foverview");
    expect(told).not.toHaveBeenCalled();
    expect(identity()).toHaveTextContent("ada@example.com");
    stop();
  });
});

describe("the 401s that already meant signed out", () => {
  it("takes the who-am-I probe's 401 as signed out, once, with nothing more asked", async () => {
    server.live = false;
    const loc = stubLocation("/data");
    const untouched = loc.href;
    const told = vi.fn();
    const stop = onSessionEnded(told);
    renderConsole(<OwnedPage />, "/data");
    await waitFor(() => expect(owned()).toHaveTextContent(/^signed-out$/));

    await elapse(10 * 60_000);

    expect(asked("/api/auth/me")).toBe(1);
    expect(ownerAsks()).toBe(0);
    expect(told).not.toHaveBeenCalled();
    expect(identity()).toHaveTextContent("nobody");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(loc.href).toBe(untouched);
    stop();
  });

  it("takes the sign-out's 401 as signed out, with no notice and nothing more asked", async () => {
    server.logout = () => Promise.resolve(json(401, { detail: "Not authenticated" }));
    const loc = stubLocation("/data");
    const untouched = loc.href;
    const told = vi.fn();
    const stop = onSessionEnded(told);
    renderConsole(<OwnedPage />, "/data");
    await waitFor(() => expect(owned()).toHaveTextContent("ready Roof,Mast"));
    const asksBefore = ownerAsks();

    await signOut();

    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
    expect(identity()).toHaveTextContent("nobody");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(asked("/api/auth/logout")).toBe(1);
    expect(told).not.toHaveBeenCalled();
    expect(loc.href).toBe(untouched);
    await elapse(10 * 60_000);
    expect(ownerAsks()).toBe(asksBefore);
    expect(asked("/api/auth/me")).toBe(1);
    stop();
  });

  // An owner request sent before the sign-out and answered after it: the
  // session it met is the one just ended, which says nothing new.
  it("keeps one notice for an unconfirmed sign-out when a late owner request then answers 401", async () => {
    server.logout = () => Promise.resolve(json(500, { detail: "the server's own words" }));
    const loc = stubLocation("/data");
    const untouched = loc.href;
    renderConsole(<OwnedPage />, "/data");
    await waitFor(() => expect(owned()).toHaveTextContent("ready Roof,Mast"));
    let answer: (r: Response) => void = () => {};
    server.hold = new Promise((resolve) => (answer = resolve));
    await elapse(60_000);
    const asksBefore = ownerAsks();

    await signOut();
    expect(await screen.findByRole("alert")).toHaveTextContent("Sign-out not confirmed");

    await act(async () => answer(json(401, { detail: "Not authenticated" })));
    await elapse(10 * 60_000);

    expect(screen.getAllByRole("alert")).toHaveLength(1);
    expect(screen.getByRole("alert")).toHaveTextContent("Sign-out not confirmed");
    expect(screen.getByRole("alert")).not.toHaveTextContent("still has not confirmed it");
    expect(identity()).toHaveTextContent("nobody");
    expect(asked("/api/auth/logout")).toBe(1);
    expect(ownerAsks()).toBe(asksBefore);
    expect(loc.href).toBe(untouched);
  });
});

describe("which 401s the client reads as the session ending", () => {
  beforeEach(() => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(401, { detail: "Not authenticated" })),
    );
  });

  it.each([
    ["an open page", "/leaderboard"],
    ["a nested open page", "/data/2026/09/17"],
    ["the login page", "/login"],
  ])("an owner route's, on %s, without moving the caller", async (_where, pathname) => {
    const loc = stubLocation(pathname);
    const untouched = loc.href;
    const told = vi.fn();
    const stop = onSessionEnded(told);

    await expect(api.myNodes()).rejects.toMatchObject({ status: 401 });
    await expect(api.myArchive({ date: "2026/09/17" })).rejects.toMatchObject({ status: 401 });

    expect(told).toHaveBeenCalledTimes(2);
    expect(loc.href).toBe(untouched);
    stop();
  });

  it.each([
    ["a page that needs a session", "/nodes/ret-0042", "app.retina.fm", "/login?next=%2Fnodes%2Fret-0042"],
    ["the admin console", "/leaderboard", "admin.retina.fm", "/login"],
  ])("none on %s, which still sends the caller to sign in", async (_where, pathname, hostname, landing) => {
    const loc = stubLocation(pathname, hostname);
    const told = vi.fn();
    const stop = onSessionEnded(told);

    await expect(api.myNodes()).rejects.toMatchObject({ status: 401 });

    expect(told).not.toHaveBeenCalled();
    expect(loc.href).toBe(landing);
    stop();
  });

  it.each([
    ["the sign-out", () => api.logout()],
    ["a public read", () => api.leaderboard()],
    ["a claim link", () => api.claimPreview("t0k3n")],
  ])("not %s's", async (_what, call) => {
    stubLocation("/leaderboard");
    const told = vi.fn();
    const stop = onSessionEnded(told);

    await expect(call()).rejects.toMatchObject({ status: 401 });

    expect(told).not.toHaveBeenCalled();
    stop();
  });

  it("not an owner route's other refusals", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => json(403, { detail: "Forbidden" })),
    );
    stubLocation("/leaderboard");
    const told = vi.fn();
    const stop = onSessionEnded(told);

    await expect(api.myNodes()).rejects.toMatchObject({ status: 403 });

    expect(told).not.toHaveBeenCalled();
    stop();
  });

  it("stops telling a listener once it unsubscribes", async () => {
    stubLocation("/leaderboard");
    const told = vi.fn();
    onSessionEnded(told)();

    await expect(api.myNodes()).rejects.toMatchObject({ status: 401 });

    expect(told).not.toHaveBeenCalled();
  });
});
