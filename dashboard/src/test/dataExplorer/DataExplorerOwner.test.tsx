import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DataExplorerPage from "../../pages/user/DataExplorerPage";
import { forgetPrivate } from "../../pages/user/dataExplorer/withheld";
import type { OwnedNode, OwnerNodeLocation } from "../../types";
import { resetAuth, setAuth } from "../fakeAuth";
import { ownedNode } from "../fixtures/ownedNode";

const { requestMock } = vi.hoisted(() => ({ requestMock: vi.fn() }));
vi.mock("@retina/shared", () => ({
  request: requestMock,
  UnauthorizedError: class UnauthorizedError extends Error {},
}));
vi.mock("../../context/AuthContext", async () => ({ useAuth: (await import("../fakeAuth")).useFakeAuth }));

const DAY = "2026-09-17";
const RANGE = `from=${DAY}&to=${DAY}`;
const ADA = { id: "7", email: "ada@example.com", name: "Ada" };

const at = (lat: number, lon: number): OwnerNodeLocation => ({
  rx_lat: lat,
  rx_lon: lon,
  rx_alt_ft: null,
  tx_lat: null,
  tx_lon: null,
  tx_alt_ft: null,
});

// The owner's two nodes. Roof is private, so the public archive and the radar
// leave it out and only the owner's routes list it; Mast is public, and the
// radar publishes it at a displaced position.
const ROOF = ownedNode({
  node_id: "retroof0001",
  node_ref: "nderoof00000001",
  name: "Roof",
  location_private: true,
  location: at(51.41234, -0.30123),
});
const MAST = ownedNode({ node_id: "retmast0001", node_ref: "ndemast00000001", name: "Mast", location: at(51.6, -0.5) });
// Someone else's node, on the network only.
const HILL = "ndehill00000001";

const FLEET = {
  [MAST.node_ref!]: {
    name: "Mast",
    status: "online",
    is_synthetic: false,
    location: { rx_lat: 51.62, rx_lon: -0.52, location_uncertainty_km: 3 },
  },
  [HILL]: {
    name: "Hilltop",
    status: "online",
    is_synthetic: false,
    location: { rx_lat: 52, rx_lon: -1, location_uncertainty_km: 3 },
  },
};

/** The one file each node has on a day, named after the node so each reads
 *  apart from the others: Roof's is part-roof.parquet. */
const keyOf = (day: string, ref: string) =>
  `year=${day.slice(0, 4)}/month=${day.slice(5, 7)}/day=${day.slice(8, 10)}/node_ref=${ref}/part-${ref.slice(3, 7)}.parquet`;
const ROOF_KEY = keyOf(DAY, ROOF.node_ref!);
const MAST_KEY = keyOf(DAY, MAST.node_ref!);

const OWNER_LISTING = "/api/auth/me/archive?";
const PUBLIC_LISTING = "/api/data/archive?";

/** A listing as the route answers it: the day asked for, narrowed to the
 *  node_ref when there is one. */
function listing(url: string, refs: string[]) {
  const q = new URL(url, "http://localhost").searchParams;
  const day = (q.get("date") ?? "").replace(/\//g, "-");
  const only = q.get("node_ref");
  const files = refs
    .filter((ref) => !only || ref === only)
    .map((ref) => ({ key: keyOf(day, ref), size_bytes: 1024, modified: `${day}T06:00:00Z` }));
  return Promise.resolve({ files, count: files.length, total: files.length });
}

const FRAME = { timestamp: Date.parse(`${DAY}T05:10:00Z`), delay: [12.3], doppler: [-30.5], snr: [14.2] };

/** The server, by route. Anything else is a request the page should not make. */
function serve({ mine = [ROOF, MAST] as OwnedNode[] | Error | Promise<OwnedNode[]> } = {}) {
  requestMock.mockImplementation((url: string) => {
    if (url === "/api/auth/me/nodes") return mine instanceof Error ? Promise.reject(mine) : Promise.resolve(mine);
    if (url === "/api/radar/nodes") return Promise.resolve({ nodes: FLEET });
    if (url.startsWith(OWNER_LISTING)) return listing(url, [ROOF.node_ref!, MAST.node_ref!]);
    if (url.startsWith(PUBLIC_LISTING)) return listing(url, [MAST.node_ref!, HILL]);
    if (url.startsWith("/api/auth/me/archive/") || url.startsWith("/api/data/archive/")) {
      return Promise.resolve({ node_ref: "x", detections: [FRAME] });
    }
    return Promise.reject(new Error(`unexpected request: ${url}`));
  });
}

const urls = () => requestMock.mock.calls.map((c) => c[0] as string);
const ownerListings = () => urls().filter((u) => u.startsWith(OWNER_LISTING));
const publicListings = () => urls().filter((u) => u.startsWith(PUBLIC_LISTING));
/** Every request to a route that answers without a session. */
const publicRequests = () => urls().filter((u) => !u.startsWith("/api/auth/"));

function Where() {
  return <output data-testid="search">{useLocation().search}</output>;
}

function renderAt(query = RANGE) {
  return render(
    <MemoryRouter initialEntries={[`/data?${query}`]}>
      <Routes>
        <Route
          path="/data"
          element={
            <>
              <DataExplorerPage />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

const search = () => screen.getByTestId("search").textContent;
const files = () => screen.getByTestId("de-stat-files");
const pickerRow = (name: RegExp) => screen.getByRole("checkbox", { name }).closest("label")!;
const manifest = () => screen.getByRole<HTMLTextAreaElement>("textbox", { name: /manifest/i }).value;

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  vi.setSystemTime(new Date(`${DAY}T12:00:00Z`));
  resetAuth();
  setAuth({ user: ADA, loading: false });
  forgetPrivate();
  requestMock.mockReset();
  serve();
  // usePolling logs every failure.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("DataExplorerPage, signed in on the owner's own nodes", () => {
  it("lists the owner's archive from the owner's routes, the private node included", async () => {
    renderAt();
    await waitFor(() => expect(files()).toHaveTextContent("2"));

    expect(screen.getByRole("radio", { name: /My nodes/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /My nodes/ }).closest("label")).toHaveTextContent("My nodes · 2");
    expect(screen.getByText("Browse and download your nodes' archive, private ones included")).toBeInTheDocument();
    expect(urls()).toContain("/api/auth/me/nodes");
    expect(ownerListings()).toEqual([`${OWNER_LISTING}date=2026%2F09%2F17&limit=500&offset=0`]);
    // Nothing is asked of a route that answers without a session: not the
    // public archive, and not the radar's node list either.
    expect(publicRequests()).toEqual([]);

    // The picker is the owner's list: both their nodes, the private one
    // marked as such, and nobody else's.
    fireEvent.click(screen.getByRole("button", { name: "All my nodes" }));
    expect(within(pickerRow(/Roof/)).getByText("Private")).toBeInTheDocument();
    expect(within(pickerRow(/Mast/)).queryByText("Private")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Hilltop/ })).not.toBeInTheDocument();
    expect(screen.getByText(/of your 2/)).toBeInTheDocument();
  });

  it("asks a single private node of the owner's route alone", async () => {
    renderAt(`node=${ROOF.node_ref}&${RANGE}`);
    await waitFor(() => expect(files()).toHaveTextContent("1"));
    expect(ownerListings()).toEqual([`${OWNER_LISTING}date=2026%2F09%2F17&node_ref=${ROOF.node_ref}&limit=500&offset=0`]);
    expect(publicRequests()).toEqual([]);
  });

  it("draws the owner's nodes where they really are", async () => {
    renderAt();
    await waitFor(() => expect(files()).toHaveTextContent("2"));
    fireEvent.click(screen.getByRole("button", { name: "Map" }));

    const roof = screen.getByRole("button", { name: "Centre on Roof" });
    expect(roof.querySelector("title")).toHaveTextContent("51.4123, -0.3012 (exact)");
    expect(screen.getByText(/receiver positions of your own nodes/)).toBeInTheDocument();
    expect(screen.getByText("Near (exact node position)")).toBeInTheDocument();
  });

  it("previews, links and fetches a private node's file through the owner's route, with the session", async () => {
    renderAt();
    fireEvent.click(await screen.findByRole("button", { name: "Preview part-roof.parquet" }));
    const dialog = screen.getByRole("dialog", { name: "part-roof.parquet" });
    const url = `${window.location.origin}/api/auth/me/archive/${ROOF_KEY}`;

    expect(dialog).toHaveTextContent("Exact rx51.4123, -0.3012");
    expect(within(dialog).getByRole("link", { name: "Open JSON" })).toHaveAttribute("href", url);
    expect(dialog).toHaveTextContent(`curl -sS -b "auth_token=$RETINA_SESSION" -o ${ROOF.node_ref}-part-roof.json "${url}"`);
    expect(within(dialog).getByTestId("de-drawer-session")).toHaveTextContent("answers only your signed-in session");

    fireEvent.click(within(dialog).getByRole("button", { name: /Load preview/ }));
    await waitFor(() => expect(dialog).toHaveTextContent("Frames"));
    expect(urls()).toContain(`/api/auth/me/archive/${ROOF_KEY}`);

    fireEvent.click(within(dialog).getByRole("button", { name: "Add to basket" }));
    expect(manifest()).toBe(url);
    expect(screen.getByTestId("de-basket-session")).toHaveTextContent("Its link needs your signed-in session");
    // The file's own link in the tree goes the same way.
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    const row = screen.getByRole("button", { name: "Preview part-roof.parquet" }).closest("[data-testid='de-file']")!;
    expect(within(row as HTMLElement).getByRole("link", { name: "JSON" })).toHaveAttribute(
      "href",
      `/api/auth/me/archive/${ROOF_KEY}`,
    );
    expect(publicRequests()).toEqual([]);
  });
});

describe("DataExplorerPage, the whole network", () => {
  it("reads the public registry and archive as a visitor does, the owner's nodes marked", async () => {
    renderAt(`${RANGE}&scope=network`);
    await waitFor(() => expect(files()).toHaveTextContent("2"));

    expect(screen.getByRole("radio", { name: "Whole network" })).toBeChecked();
    expect(
      screen.getByText("Browse and download the whole network's archive, which leaves your private nodes out"),
    ).toBeInTheDocument();
    expect(urls()).toContain("/api/radar/nodes");
    expect(publicListings()).toEqual([`${PUBLIC_LISTING}date=2026%2F09%2F17&limit=500&offset=0`]);
    expect(ownerListings()).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "All nodes" }));
    expect(within(pickerRow(/Mast/)).getByText("Yours")).toBeInTheDocument();
    expect(within(pickerRow(/Hilltop/)).queryByText("Yours")).not.toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: /Roof/ })).not.toBeInTheDocument();
    // The tree marks the owner's node's files as well.
    const head = screen.getByRole("checkbox", { name: `Select every file from ${MAST.node_ref} on ${DAY}` });
    expect(head.closest(".de-node-head")).toHaveTextContent("Yours");

    fireEvent.click(head);
    fireEvent.click(screen.getByRole("button", { name: "Show manifest" }));
    expect(manifest()).toBe(`${window.location.origin}/api/data/archive/${MAST_KEY}`);
    expect(screen.queryByTestId("de-basket-session")).not.toBeInTheDocument();
  });

  it("is the page as it always was to a visitor: no switch, no marks, nothing asked of the owner's routes", async () => {
    setAuth({ user: null });
    renderAt();
    await waitFor(() => expect(files()).toHaveTextContent("2"));

    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(screen.getByText("Browse and download the detection archive")).toBeInTheDocument();
    expect(urls().filter((u) => u.startsWith("/api/auth/"))).toEqual([]);
    expect(publicListings().length).toBeGreaterThan(0);
    fireEvent.click(screen.getByRole("button", { name: "All nodes" }));
    expect(screen.queryByText("Yours")).not.toBeInTheDocument();
    expect(screen.getByText(/of 2 known/)).toBeInTheDocument();
    expect(screen.getByTestId("de-share")).toHaveTextContent(`?${RANGE}`);
  });

  it("is the network's archive for an account with no nodes, which is told how to add one", async () => {
    serve({ mine: [] });
    renderAt();
    await waitFor(() => expect(files()).toHaveTextContent("2"));

    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(screen.getByText("No nodes on your account yet, so this is the whole network's archive.")).toBeInTheDocument();
    expect(ownerListings()).toEqual([]);
    expect(publicListings().length).toBeGreaterThan(0);
  });

  it("falls back to the network's archive when the owner's list fails, and says so", async () => {
    serve({ mine: new Error("HTTP 503") });
    renderAt();
    await waitFor(() => expect(files()).toHaveTextContent("2"));

    expect(screen.getByText(/Could not load your nodes: HTTP 503\. Showing the whole network\./)).toBeInTheDocument();
    expect(ownerListings()).toEqual([]);
    expect(publicListings().length).toBeGreaterThan(0);
  });

  it("asks neither archive while the owner's list is on its way", async () => {
    let answer!: (nodes: OwnedNode[]) => void;
    serve({ mine: new Promise<OwnedNode[]>((resolve) => (answer = resolve)) });
    renderAt();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(urls()).toEqual(["/api/auth/me/nodes"]);

    await act(async () => answer([ROOF, MAST]));
    await waitFor(() => expect(files()).toHaveTextContent("2"));
    expect(publicRequests()).toEqual([]);
  });
});

describe("DataExplorerPage, switching scope", () => {
  it("drops a private node from the selection before the network's archive is asked", async () => {
    renderAt(`node=${ROOF.node_ref}&${RANGE}`);
    await waitFor(() => expect(files()).toHaveTextContent("1"));

    fireEvent.click(screen.getByRole("radio", { name: "Whole network" }));
    await waitFor(() => expect(files()).toHaveTextContent("2"));

    expect(publicListings()).toEqual([`${PUBLIC_LISTING}date=2026%2F09%2F17&limit=500&offset=0`]);
    expect(publicRequests().filter((u) => u.includes(ROOF.node_ref!))).toEqual([]);
    // Nothing of the selection was left, so it is every node rather than none.
    expect(search()).toBe(`?${RANGE}&scope=network`);
    expect(screen.getByRole("button", { name: "All nodes" })).toBeInTheDocument();
  });

  it("keeps the owner's public node selected across the switch, and drops only the private one", async () => {
    renderAt(`node=${MAST.node_ref}&node=${ROOF.node_ref}&${RANGE}`);
    await waitFor(() => expect(files()).toHaveTextContent("2"));

    fireEvent.click(screen.getByRole("radio", { name: "Whole network" }));
    await waitFor(() => expect(files()).toHaveTextContent("1"));

    // One node left, so the network's listing is scoped to it, and to it alone.
    expect(publicListings()).toEqual([
      `${PUBLIC_LISTING}date=2026%2F09%2F17&limit=500&offset=0&node_ref=${MAST.node_ref}`,
    ]);
    expect(publicRequests().filter((u) => u.includes(ROOF.node_ref!))).toEqual([]);
    expect(search()).toBe(`?node=${MAST.node_ref}&${RANGE}&scope=network`);
  });

  it("drops the network's nodes from the selection on the way back to the owner's own", async () => {
    renderAt(`node=${HILL}&${RANGE}&scope=network`);
    await waitFor(() => expect(publicListings()).toHaveLength(1));
    expect(publicListings()[0]).toContain(`node_ref=${HILL}`);

    fireEvent.click(screen.getByRole("radio", { name: /My nodes/ }));
    await waitFor(() => expect(files()).toHaveTextContent("2"));

    expect(ownerListings()).toEqual([`${OWNER_LISTING}date=2026%2F09%2F17&limit=500&offset=0`]);
    expect(search()).toBe(`?${RANGE}`);
    expect(screen.getByRole("button", { name: "All my nodes" })).toBeInTheDocument();
  });

  it("does not carry a private node named in a link to the network's archive", async () => {
    renderAt(`node=${ROOF.node_ref}&${RANGE}&scope=network`);
    await waitFor(() => expect(files()).toHaveTextContent("2"));

    expect(publicListings()).toEqual([`${PUBLIC_LISTING}date=2026%2F09%2F17&limit=500&offset=0`]);
    expect(publicRequests().filter((u) => u.includes(ROOF.node_ref!))).toEqual([]);
    expect(screen.getByTestId("de-share")).toHaveTextContent(`?${RANGE}&scope=network`);
  });

  it("keeps a private node's file in the basket across the switch, still fetched from the owner's route", async () => {
    renderAt();
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select part-roof.parquet" }));

    fireEvent.click(screen.getByRole("radio", { name: "Whole network" }));
    await waitFor(() => expect(publicListings()).toHaveLength(1));
    await waitFor(() => expect(files()).toHaveTextContent("2"));

    fireEvent.click(screen.getByRole("button", { name: "Show manifest" }));
    expect(manifest()).toBe(`${window.location.origin}/api/auth/me/archive/${ROOF_KEY}`);
    expect(screen.getByTestId("de-basket-session")).toBeInTheDocument();
    expect(publicRequests().filter((u) => u.includes(ROOF.node_ref!))).toEqual([]);
  });

  it("leaves the filters' Reset to the filters, and the scope where it was", async () => {
    renderAt(`${RANGE}&minsize=4096&scope=network`);
    await waitFor(() => expect(publicListings().length).toBeGreaterThan(0));
    fireEvent.click(screen.getByRole("button", { name: "Reset" }));
    await waitFor(() => expect(search()).toBe("?from=2026-09-15&to=2026-09-17&scope=network"));
    expect(screen.getByRole("radio", { name: "Whole network" })).toBeChecked();
  });
});

describe("DataExplorerPage, a change of session", () => {
  it("starts afresh, leaving nothing of the owner's archive on screen", async () => {
    renderAt();
    fireEvent.click(await screen.findByRole("checkbox", { name: "Select part-roof.parquet" }));
    expect(screen.getByTestId("de-basket-count")).toHaveTextContent("1 file selected");

    act(() => setAuth({ user: null }));
    await waitFor(() => expect(screen.getByTestId("de-basket-count")).toHaveTextContent("0 files selected"));
    await waitFor(() => expect(files()).toHaveTextContent("2"));
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    expect(screen.queryByText(/part-roof/)).not.toBeInTheDocument();
    expect(publicListings().length).toBeGreaterThan(0);
  });
});
