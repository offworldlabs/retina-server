import { Fragment, type ReactNode } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { BrowserRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DataExplorerPage from "../../pages/user/DataExplorerPage";
import { forgetPrivate } from "../../pages/user/dataExplorer/withheld";
import type { OwnedNode, OwnerNodeLocation } from "../../types";
import { resetAuth, setAuth, useFakeAuth } from "../fakeAuth";
import { ownedNode } from "../fixtures/ownedNode";
import { stubViewport } from "../matchMedia";

/**
 * What the Data Explorer lets out of an owner's session: into the links it
 * writes, and to the routes that answer without one. A private node's ref
 * never reaches a public route, by the request's own address or by the page
 * address its Referer carries, and an owned node's true position never
 * reaches a link. Above all across a change of user, which the console meets
 * by remounting the page, as `Console` does here.
 */

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", () => ({
  request: requestMock,
  UnauthorizedError: class UnauthorizedError extends Error {},
}));
vi.mock("../../context/AuthContext", async () => ({ useAuth: (await import("../fakeAuth")).useFakeAuth }));

const DAY = "2026-09-17";
const RANGE = `from=${DAY}&to=${DAY}`;
const ADA = { id: "7", email: "ada@example.com", name: "Ada" };
const BOB = { id: "8", email: "bob@example.com", name: "Bob" };

const at = (lat: number, lon: number): OwnerNodeLocation => ({
  rx_lat: lat,
  rx_lon: lon,
  rx_alt_ft: null,
  tx_lat: null,
  tx_lon: null,
  tx_alt_ft: null,
});

// Ada's two nodes. Roof is private: only her own routes list it, and only
// they know where it is. Mast is public, published at a displaced position.
const ROOF = ownedNode({
  node_id: "retroof0001",
  node_ref: "nderoof00000001",
  name: "Roof",
  location_private: true,
  location: at(51.41234, -0.30123),
});
const MAST = ownedNode({ node_id: "retmast0001", node_ref: "ndemast00000001", name: "Mast", location: at(51.6, -0.5) });
const ROOF_REF = ROOF.node_ref!;
const MAST_REF = MAST.node_ref!;
// Bob's one node, public.
const SHED = ownedNode({ node_id: "retshed0001", node_ref: "ndeshed00000001", name: "Shed", location: at(52.2, 0.12) });
const SHED_REF = SHED.node_ref!;
// Someone else's node, on the network only.
const HILL = "ndehill00000001";

/** Roof's true position as a link could spell it: to the map's four decimals,
 *  or to fewer, which would still say where it is. */
const ROOF_AT = /51\.41|-0\.30/;

const published = (lat: number, lon: number) => ({
  status: "online",
  is_synthetic: false,
  location: { rx_lat: lat, rx_lon: lon, location_uncertainty_km: 1 },
});
const FLEET = {
  [MAST_REF]: { name: "Mast", ...published(51.62, -0.52) },
  [SHED_REF]: { name: "Shed", ...published(52.21, 0.13) },
  [HILL]: { name: "Hilltop", ...published(52, -1) },
};

const keyOf = (day: string, ref: string) =>
  `year=${day.slice(0, 4)}/month=${day.slice(5, 7)}/day=${day.slice(8, 10)}/node_ref=${ref}/part-${ref.slice(3, 7)}.parquet`;

const OWNER_LISTING = "/api/auth/me/archive?";
const PUBLIC_LISTING = "/api/data/archive?";

function listing(url: string, refs: string[]) {
  const q = new URL(url, "http://localhost").searchParams;
  const day = (q.get("date") ?? "").replace(/\//g, "-");
  const only = q.get("node_ref");
  const files = refs
    .filter((ref) => !only || ref === only)
    .map((ref) => ({ key: keyOf(day, ref), size_bytes: 1024, modified: `${day}T06:00:00Z` }));
  return Promise.resolve({ files, count: files.length, total: files.length });
}

/** The server: whose list the session gets, and what their archive holds. The
 *  network's archive is every public node's, and never Roof's. */
const server = {
  mine: [ROOF, MAST] as OwnedNode[] | Error | Promise<OwnedNode[]>,
  archive: [ROOF_REF, MAST_REF],
};

function route(url: string): Promise<unknown> {
  if (url === "/api/auth/me/nodes") {
    const { mine } = server;
    return mine instanceof Error ? Promise.reject(mine) : Promise.resolve(mine);
  }
  if (url === "/api/radar/nodes") return Promise.resolve({ nodes: FLEET });
  if (url.startsWith(OWNER_LISTING)) return listing(url, server.archive);
  if (url.startsWith(PUBLIC_LISTING)) return listing(url, [MAST_REF, SHED_REF, HILL]);
  return Promise.reject(new Error(`unexpected request: ${url}`));
}

/** A request as it left the page: its address, the page's address at that
 *  moment, and the Referer a browser would send with it. */
interface Sent {
  url: string;
  page: string;
  referrer: string;
}

let sent: Sent[] = [];

/** The Referer of a request to the page's own origin. The console's policy,
 *  strict-origin-when-cross-origin, sends the page's whole address unless the
 *  request asks for less. */
function referrerOf(opts?: { referrerPolicy?: ReferrerPolicy }): string {
  switch (opts?.referrerPolicy) {
    case "no-referrer":
      return "";
    case "origin":
    case "strict-origin":
      return `${window.location.origin}/`;
    default:
      return window.location.href;
  }
}

/** Requests to a route that answers without a session. */
const publicSent = () => sent.filter((s) => !s.url.startsWith("/api/auth/"));
/** The public requests that told the server of `ref`, by address or Referer. */
const told = (ref: string) => publicSent().filter((s) => s.url.includes(ref) || s.referrer.includes(ref));
/** The public requests sent while the address bar still named `ref`. */
const sentWhileNamed = (ref: string) => publicSent().filter((s) => s.page.includes(ref));
const ownerListings = () => sent.map((s) => s.url).filter((u) => u.startsWith(OWNER_LISTING));
const publicListings = () => sent.map((s) => s.url).filter((u) => u.startsWith(PUBLIC_LISTING));

/** The page area as DashboardLayout holds it: remounted, page and all, when
 *  the signed-in identity changes. */
function Console({ children }: { children: ReactNode }) {
  const { user } = useFakeAuth();
  return <Fragment key={user?.id ?? ""}>{children}</Fragment>;
}

function renderAt(query: string) {
  window.history.replaceState(null, "", `/data?${query}`);
  return render(
    <BrowserRouter>
      <Routes>
        <Route
          path="/data"
          element={
            <Console>
              <DataExplorerPage />
            </Console>
          }
        />
      </Routes>
    </BrowserRouter>,
  );
}

/** The address bar's query, which is what a Referer and a copied address carry. */
const address = () => window.location.search;
const share = () => screen.getByTestId("de-share").textContent ?? "";
const files = () => screen.getByTestId("de-stat-files");

/** Opens the map and centres the filter on Roof, as a click on its marker does. */
function centreOnRoof() {
  fireEvent.click(screen.getByRole("button", { name: "Map" }));
  fireEvent.click(screen.getByRole("button", { name: "Centre on Roof" }));
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date(`${DAY}T12:00:00Z`));
  resetAuth();
  setAuth({ user: ADA, loading: false });
  // Every test is a fresh document.
  forgetPrivate();
  server.mine = [ROOF, MAST];
  server.archive = [ROOF_REF, MAST_REF];
  sent = [];
  requestMock.mockReset();
  requestMock.mockImplementation((url: string, opts?: { referrerPolicy?: ReferrerPolicy }) => {
    sent.push({ url, page: `${window.location.pathname}${window.location.search}`, referrer: referrerOf(opts) });
    return route(url);
  });
  // usePolling logs every failure.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  window.history.replaceState(null, "", "/");
  Reflect.deleteProperty(navigator, "clipboard");
});

// Reviewer cases R1 and R1b: signing out left the private node in the link,
// and the visitor's page then asked the public archive for it by name.
describe("DataExplorerPage, signing out", () => {
  it("sends the private node's ref to no public route, and takes it out of the link before asking any", async () => {
    renderAt(`node=${ROOF_REF}&${RANGE}`);
    await waitFor(() => expect(files()).toHaveTextContent("1"));
    expect(publicSent()).toEqual([]);

    act(() => setAuth({ user: null }));
    await waitFor(() => expect(files()).toHaveTextContent("3"));

    expect(publicListings()).toEqual([`${PUBLIC_LISTING}date=2026%2F09%2F17&limit=500&offset=0`]);
    expect(told(ROOF_REF)).toEqual([]);
    expect(sentWhileNamed(ROOF_REF)).toEqual([]);
    expect(address()).toBe(`?${RANGE}`);
  });

  it("leaves the visitor no link to the owner's node, by its ref or by where it is", async () => {
    renderAt(`node=${ROOF_REF}&${RANGE}`);
    await waitFor(() => expect(files()).toHaveTextContent("1"));
    centreOnRoof();
    await waitFor(() => expect(address()).toContain("near="));
    expect(address()).not.toMatch(ROOF_AT);

    act(() => setAuth({ user: null }));
    await waitFor(() => expect(files()).toHaveTextContent("3"));

    expect(share()).toBe(`?${RANGE}`);
    expect(address()).toBe(`?${RANGE}`);
    expect(screen.getByRole("button", { name: "All nodes" })).toBeInTheDocument();
    expect(screen.queryByTestId("de-near-node")).not.toBeInTheDocument();
    expect(sentWhileNamed(ROOF_REF)).toEqual([]);
  });
});

describe("DataExplorerPage, a change of account", () => {
  it("drops the last owner's private node and centre before the next one asks the network anything", async () => {
    renderAt(`node=${ROOF_REF}&${RANGE}`);
    await waitFor(() => expect(files()).toHaveTextContent("1"));
    centreOnRoof();
    await waitFor(() => expect(address()).toContain("near="));

    server.mine = [SHED];
    server.archive = [SHED_REF];
    act(() => setAuth({ user: BOB }));
    // Bob's own archive, none of Ada's left in the link.
    await waitFor(() => expect(address()).toBe(`?${RANGE}`));
    await waitFor(() => expect(files()).toHaveTextContent("1"));
    expect(ownerListings()).toEqual([
      `${OWNER_LISTING}date=2026%2F09%2F17&node_ref=${ROOF_REF}&limit=500&offset=0`,
      `${OWNER_LISTING}date=2026%2F09%2F17&limit=500&offset=0`,
    ]);

    fireEvent.click(screen.getByRole("radio", { name: "Whole network" }));
    await waitFor(() => expect(files()).toHaveTextContent("3"));
    expect(address()).toBe(`?${RANGE}&scope=network`);
    expect(share()).toBe(`?${RANGE}&scope=network`);
    expect(told(ROOF_REF)).toEqual([]);
    expect(sentWhileNamed(ROOF_REF)).toEqual([]);
  });

  it("drops them as well when the next owner's list fails, and the network's archive stands in", async () => {
    renderAt(`node=${ROOF_REF}&${RANGE}`);
    await waitFor(() => expect(files()).toHaveTextContent("1"));
    centreOnRoof();
    await waitFor(() => expect(address()).toContain("near="));

    server.mine = new Error("HTTP 503");
    act(() => setAuth({ user: BOB }));
    expect(await screen.findByText(/Could not load your nodes: HTTP 503/)).toBeInTheDocument();
    await waitFor(() => expect(files()).toHaveTextContent("3"));

    expect(address()).toBe(`?${RANGE}`);
    expect(told(ROOF_REF)).toEqual([]);
    expect(sentWhileNamed(ROOF_REF)).toEqual([]);
  });
});

// Reviewer case R2: with the owner's list failing, the page fell back to the
// network's archive and asked it about the private node the link named.
describe("DataExplorerPage, with the owner's list on its way or failing", () => {
  it("asks no public route about a node the link names, says why, and offers Retry", async () => {
    let fail!: (e: Error) => void;
    server.mine = new Promise<OwnedNode[]>((_, reject) => (fail = reject));
    renderAt(`node=${ROOF_REF}&${RANGE}`);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    // On its way: nothing is asked but the list itself.
    expect(sent.map((s) => s.url)).toEqual(["/api/auth/me/nodes"]);

    await act(async () => fail(new Error("HTTP 503")));
    expect(await screen.findByText(/Could not load your nodes: HTTP 503\. Showing the whole network\./)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/1 of 1 days listed/)).toBeInTheDocument());

    expect(publicListings()).toEqual([`${PUBLIC_LISTING}date=2026%2F09%2F17&limit=500&offset=0`]);
    expect(told(ROOF_REF)).toEqual([]);
    // The link is left as it came, for Retry to show it.
    expect(address()).toBe(`?node=${ROOF_REF}&${RANGE}`);

    server.mine = [ROOF, MAST];
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(files()).toHaveTextContent("1"));
    expect(ownerListings()).toEqual([`${OWNER_LISTING}date=2026%2F09%2F17&node_ref=${ROOF_REF}&limit=500&offset=0`]);
    expect(told(ROOF_REF)).toEqual([]);
  });

  it("keeps the private nodes an earlier list named off the network's archive, and in the link", async () => {
    const first = renderAt(RANGE);
    await waitFor(() => expect(files()).toHaveTextContent("2"));
    first.unmount();

    server.mine = new Error("HTTP 503");
    renderAt(`node=${ROOF_REF}&${RANGE}`);
    expect(await screen.findByText(/Could not load your nodes: HTTP 503/)).toBeInTheDocument();
    await waitFor(() => expect(files()).toHaveTextContent("3"));

    expect(told(ROOF_REF)).toEqual([]);
    expect(address()).toBe(`?node=${ROOF_REF}&${RANGE}`);

    server.mine = [ROOF, MAST];
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(files()).toHaveTextContent("1"));
    expect(ownerListings()).toEqual([
      `${OWNER_LISTING}date=2026%2F09%2F17&limit=500&offset=0`,
      `${OWNER_LISTING}date=2026%2F09%2F17&node_ref=${ROOF_REF}&limit=500&offset=0`,
    ]);
  });
});

describe("DataExplorerPage, a link kept for Retry", () => {
  /** Ada has been here before in this document, so her private node is known,
   *  and her list now fails on a link centred on it. */
  async function failingOnRoof() {
    const first = renderAt(RANGE);
    await waitFor(() => expect(files()).toHaveTextContent("2"));
    first.unmount();
    server.mine = new Error("HTTP 503");
    renderAt(`node=${ROOF_REF}&${RANGE}&near=${ROOF_REF}%2C10`);
    expect(await screen.findByText(/Could not load your nodes: HTTP 503/)).toBeInTheDocument();
    await waitFor(() => expect(files()).toHaveTextContent("3"));
  }

  it("keeps the link's own node and centre through a filter change made while the owner's list fails", async () => {
    await failingOnRoof();
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await waitFor(() => expect(address()).toBe(`?node=${ROOF_REF}&from=2026-09-11&to=${DAY}&near=${ROOF_REF}%2C10`));
    expect(told(ROOF_REF)).toEqual([]);

    server.mine = [ROOF, MAST];
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    // A week of Roof's own files, one a day, measured from Roof.
    await waitFor(() => expect(files()).toHaveTextContent("7"));
    expect(screen.getByTestId("de-near-node")).toHaveTextContent("Roof");
    expect(told(ROOF_REF)).toEqual([]);
  });

  it("still clears them on Reset", async () => {
    await failingOnRoof();
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(address()).toBe("?from=2026-09-15&to=2026-09-17"));
  });
});

// Reviewer cases R3 and R7: a centre on the owner's node wrote its true
// position into the link, which outlived the switch to the network; and a link
// naming the private node kept it in the address bar while public listings ran.
describe("DataExplorerPage, links to the owner's nodes", () => {
  it("centres on the owner's node by naming it, never by where it is", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    renderAt(`node=${ROOF_REF}&${RANGE}`);
    await waitFor(() => expect(files()).toHaveTextContent("1"));
    centreOnRoof();

    const link = `?node=${ROOF_REF}&${RANGE}&near=${ROOF_REF}%2C50`;
    await waitFor(() => expect(address()).toBe(link));
    expect(share()).toBe(link);
    expect(screen.getByTestId("de-near-node")).toHaveTextContent("Roof");
    expect(screen.queryByLabelText("centre latitude")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Copy link" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${window.location.origin}/data${link}`));
    expect(writeText.mock.calls[0][0]).not.toMatch(ROOF_AT);
  });

  it("measures a centre on the owner's node from where their own list puts it", async () => {
    renderAt(`${RANGE}&near=${ROOF_REF}%2C10`);
    // Mast is 25 km from Roof, so only Roof's file is within 10 km of it.
    await waitFor(() => expect(files()).toHaveTextContent("1"));
    expect(screen.getByTestId("de-near-node")).toHaveTextContent("Roof");
    expect(screen.getByLabelText("Near (exact node position)")).toHaveValue(10);
    expect(address()).toBe(`?${RANGE}&near=${ROOF_REF}%2C10`);
  });

  it("leaves neither the node nor its centre in the link once switched to the network", async () => {
    renderAt(`node=${ROOF_REF}&${RANGE}`);
    await waitFor(() => expect(files()).toHaveTextContent("1"));
    centreOnRoof();
    await waitFor(() => expect(address()).toContain("near="));

    fireEvent.click(screen.getByRole("radio", { name: "Whole network" }));
    await waitFor(() => expect(files()).toHaveTextContent("3"));

    expect(share()).toBe(`?${RANGE}&scope=network`);
    expect(address()).toBe(`?${RANGE}&scope=network`);
    expect(told(ROOF_REF)).toEqual([]);
    expect(sentWhileNamed(ROOF_REF)).toEqual([]);
  });

  it("takes a private node a link names in the network's archive out of the address bar before asking it anything", async () => {
    renderAt(`node=${ROOF_REF}&${RANGE}&scope=network`);
    await waitFor(() => expect(files()).toHaveTextContent("3"));

    expect(address()).toBe(`?${RANGE}&scope=network`);
    expect(told(ROOF_REF)).toEqual([]);
    expect(sentWhileNamed(ROOF_REF)).toEqual([]);
  });

  it("drops a centre on a node for a visitor who opens the link, before asking anything", async () => {
    setAuth({ user: null });
    renderAt(`${RANGE}&near=${ROOF_REF}%2C25`);
    await waitFor(() => expect(files()).toHaveTextContent("3"));

    expect(address()).toBe(`?${RANGE}`);
    expect(share()).toBe(`?${RANGE}`);
    expect(sentWhileNamed(ROOF_REF)).toEqual([]);
  });

  it("sends no public request the page's address rides along with", async () => {
    // A link whose private node no list here has named: the page cannot tell
    // it from any other node, and asks the network about it by no name.
    setAuth({ user: null });
    renderAt(`node=${ROOF_REF}&${RANGE}`);
    await waitFor(() => expect(screen.getByText(/1 of 1 days listed/)).toBeInTheDocument());

    expect(publicSent().length).toBeGreaterThan(0);
    expect(publicSent().every((s) => s.referrer === `${window.location.origin}/`)).toBe(true);
    expect(told(ROOF_REF)).toEqual([]);
  });
});

// A phone folds the filters behind a line that sums them up, which the page
// writes for itself rather than through the link.
describe("DataExplorerPage, the filters summed up on a phone", () => {
  const summary = () => document.querySelector(".de-fsummary");

  beforeEach(() => stubViewport(390));
  afterEach(() => vi.unstubAllGlobals());

  it("says a centre on the owner's node by its name and radius, never by where it is", async () => {
    renderAt(`${RANGE}&near=${ROOF_REF}%2C10`);
    await waitFor(() =>
      expect(summary()).toHaveAccessibleName("All my nodes · 2026-09-17 · 00:00–23:59 UTC · within 10 km of Roof"),
    );
    expect(summary()).toHaveAttribute("aria-expanded", "false");
    expect(summary()?.textContent).not.toMatch(ROOF_AT);
  });

  it("counts a lone node a visitor's link names, and names it nowhere", async () => {
    setAuth({ user: null });
    renderAt(`node=${ROOF_REF}&${RANGE}`);
    await waitFor(() => expect(screen.getByText(/1 of 1 days listed/)).toBeInTheDocument());
    expect(summary()).toHaveAccessibleName("1 of 3 nodes · 2026-09-17 · 00:00–23:59 UTC");
    expect(summary()?.textContent).not.toContain(ROOF_REF);
  });
});
