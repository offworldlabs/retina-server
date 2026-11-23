import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BrowserRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DashboardLayout from "../../components/DashboardLayout";
import RequireAuth from "../../components/RequireAuth";
import { AuthProvider } from "../../context/AuthContext";
import { ThemeProvider } from "../../context/ThemeContext";
import DataExplorerPage from "../../pages/user/DataExplorerPage";
import { forgetPrivate } from "../../pages/user/dataExplorer/withheld";
import { ownedNode } from "../fixtures/ownedNode";
import { stubMatchMedia } from "../matchMedia";

/**
 * The Data Explorer in the console as App mounts it: the real AuthProvider,
 * client, guard and layout, with fetch stubbed. The layout remounts the page
 * on a change of user, which is what the explorer's own memory of private
 * nodes has to outlive; and an owner route's 401 has to end the session on
 * the page at once, whichever owner route it was.
 */

const DAY = "2026-09-17";
const RANGE = `from=${DAY}&to=${DAY}`;
const ADA = { id: "u-ada", name: "Ada", email: "ada@example.com", role: "user" };

const ROOF = ownedNode({ node_id: "retroof0001", node_ref: "nderoof00000001", name: "Roof", location_private: true });
const MAST = ownedNode({ node_id: "retmast0001", node_ref: "ndemast00000001", name: "Mast" });
const ROOF_REF = ROOF.node_ref!;
const MAST_REF = MAST.node_ref!;
const HILL = "ndehill00000001";

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

/** Ada's session holds until `live` goes false: signed out here, or anywhere
 *  else, after which every route behind it answers 401. */
const server = { live: true };

function route(path: string): Response {
  if (path === "/api/auth/logout") {
    server.live = false;
    return json(200, { ok: true });
  }
  if (path === "/api/auth/me" || path.startsWith("/api/auth/me/")) {
    if (!server.live) return json(401, { detail: "Not authenticated" });
    if (path === "/api/auth/me") return json(200, ADA);
    if (path === "/api/auth/me/nodes") return json(200, [ROOF, MAST]);
    if (path.startsWith("/api/auth/me/archive?")) return json(200, listing(path, [ROOF_REF, MAST_REF]));
  }
  if (path === "/api/radar/nodes") {
    const at = (lat: number, lon: number) => ({ rx_lat: lat, rx_lon: lon, location_uncertainty_km: 1 });
    return json(200, {
      nodes: {
        [MAST_REF]: { name: "Mast", status: "online", is_synthetic: false, location: at(51.62, -0.52) },
        [HILL]: { name: "Hilltop", status: "online", is_synthetic: false, location: at(52, -1) },
      },
    });
  }
  if (path.startsWith("/api/data/archive?")) return json(200, listing(path, [MAST_REF, HILL]));
  return json(404, { detail: `unexpected request: ${path}` });
}

/** A request as it left the page, with the page's address at that moment and
 *  the Referer a browser would send with it under the console's policy. */
interface Sent {
  url: string;
  page: string;
  referrer: string;
}

let sent: Sent[] = [];

function referrerOf(init?: RequestInit): string {
  switch (init?.referrerPolicy) {
    case "no-referrer":
      return "";
    case "origin":
    case "strict-origin":
      return `${window.location.origin}/`;
    default:
      return window.location.href;
  }
}

const publicSent = () => sent.filter((s) => !s.url.startsWith("/api/auth/"));
const told = (ref: string) => publicSent().filter((s) => s.url.includes(ref) || s.referrer.includes(ref));
const sentWhileNamed = (ref: string) => publicSent().filter((s) => s.page.includes(ref));
const ownerAsks = () => sent.filter((s) => s.url.startsWith("/api/auth/me/")).length;

/** The console on /data, with the page's address set to `query`. */
function renderConsole(query: string) {
  window.history.replaceState(null, "", `/data?${query}`);
  return render(
    <BrowserRouter>
      <ThemeProvider>
        <AuthProvider>
          <RequireAuth isAdmin={false}>
            <DashboardLayout isAdmin={false}>
              <DataExplorerPage />
            </DashboardLayout>
          </RequireAuth>
        </AuthProvider>
      </ThemeProvider>
    </BrowserRouter>,
  );
}

const files = () => screen.getByTestId("de-stat-files");

/** Opens the account menu and signs out, as a person would. */
async function signOut() {
  fireEvent.keyDown(await screen.findByRole("button", { name: "Account: Ada" }), { key: "Enter" });
  fireEvent.click(screen.getByRole("menuitem", { name: "Sign out" }));
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date(`${DAY}T12:00:00Z`));
  stubMatchMedia();
  forgetPrivate();
  server.live = true;
  sent = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      sent.push({ url: String(url), page: `${window.location.pathname}${window.location.search}`, referrer: referrerOf(init) });
      return route(String(url));
    }),
  );
  // usePolling logs every failure, the 401s included.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
});

describe("the Data Explorer in the console, a session ending", () => {
  it("tells no public route of the private node selected when signed out from the account menu", async () => {
    renderConsole(`node=${ROOF_REF}&${RANGE}`);
    await waitFor(() => expect(files()).toHaveTextContent("1"));
    expect(publicSent()).toEqual([]);

    await signOut();
    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
    // Remounted by the layout as a visitor's page, on the network's archive.
    await waitFor(() => expect(files()).toHaveTextContent("2"));

    expect(told(ROOF_REF)).toEqual([]);
    expect(sentWhileNamed(ROOF_REF)).toEqual([]);
    expect(window.location.search).toBe(`?${RANGE}`);
  });

  it("ends the session at once when the owner's archive answers 401, not at the next ask for the list", async () => {
    renderConsole(RANGE);
    expect((await screen.findAllByText(/part-roof/)).length).toBeGreaterThan(0);

    // Ended elsewhere. The next filter change lists the owner's archive again,
    // most of a minute before the list of nodes would next be asked for.
    server.live = false;
    const before = sent.length;
    fireEvent.click(screen.getByRole("button", { name: "7d" }));

    expect(await screen.findByRole("link", { name: "Sign in" })).toBeInTheDocument();
    // What met the 401 was the owner's archive, not the list of nodes.
    const since = sent.slice(before);
    expect(since.some((s) => s.url.startsWith("/api/auth/me/archive?"))).toBe(true);
    expect(since.filter((s) => s.url === "/api/auth/me/nodes")).toEqual([]);
    await waitFor(() => expect(screen.queryAllByText(/part-roof/)).toHaveLength(0));
    await waitFor(() => expect(screen.queryAllByText(/part-hill/).length).toBeGreaterThan(0));
    expect(told(ROOF_REF)).toEqual([]);

    const asksAfter = ownerAsks();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 60_000);
    });
    expect(ownerAsks()).toBe(asksAfter);
  });
});
