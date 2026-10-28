import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { CLAIM_PATH } from "../components/ClaimPath";
import DetectionsPage from "../pages/user/DetectionsPage";
import type { FeedAircraft, OwnedNode } from "../types";
import { ownedNode } from "./fixtures/ownedNode";

vi.mock("../api/client", () => ({
  api: { nodes: vi.fn(), aircraft: vi.fn(), myNodes: vi.fn(), myAircraft: vi.fn() },
}));

type Auth = { user: { id: number; email: string; name: string } | null; loading: boolean };
const state = vi.hoisted(() => ({ auth: { user: null, loading: false } as Auth }));
vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

const ADA = { id: 1, email: "a@example.com", name: "Ada" };

// The owner's two nodes. Roof is private, so only the owner feed carries what
// it sees; Mast is public, and the listing has it under the node's own name,
// which the owner's label outranks.
const ROOF = ownedNode({ node_id: "retroof0001", node_ref: "nderoof00000001", name: "Roof", location_private: true });
const MAST = ownedNode({ node_id: "retmast0001", node_ref: "ndemast00000001", name: "Mast" });
// Other people's nodes: one with a name, one published under its ref only.
const HILL = "ndehill00000001";
const BARE = "ndebare00000001";
const LISTING = {
  nodes: {
    [MAST.node_ref]: { name: "mast-node" },
    [HILL]: { name: "Hilltop" },
    [BARE]: { name: BARE },
  },
};

const track = (hex: string, fields: Partial<FeedAircraft> = {}): FeedAircraft => ({
  hex,
  lat: 51.5,
  lon: -0.1,
  alt_baro: 32000,
  gs: 420,
  track: 90,
  seen: 1,
  ...fields,
});

const PRIVATE = track("aa0001", { flight: "PRV1", node_ref: ROOF.node_ref });
const MASTS = track("aa0002", { node_ref: MAST.node_ref });
// A multinode solve carries its nodes in contributing_node_refs and no node_ref.
const SOLVE = track("aa0003", { multinode: true, contributing_node_refs: [HILL, MAST.node_ref] });
const HILLS = track("aa0004", { node_ref: HILL });
const THEIR_SOLVE = track("aa0005", { multinode: true, contributing_node_refs: [HILL, BARE] });
// The old filter also matched a `source` key no producer sets.
const STRAY = { ...track("aa0006", { node_ref: HILL }), source: MAST.node_ref } as FeedAircraft;

const OWNER_FEED = [PRIVATE, MASTS, SOLVE];
const NETWORK_FEED = [MASTS, SOLVE, HILLS, THEIR_SOLVE, STRAY];

/** The page with the query string it writes beside it. */
function Where() {
  return <output data-testid="search">{useLocation().search}</output>;
}

function renderAt(entry = "/detections") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route
          path="/detections"
          element={
            <>
              <DetectionsPage />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

function answer({ mine = [ROOF, MAST], ownerFeed = OWNER_FEED, networkFeed = NETWORK_FEED } = {}) {
  vi.mocked(api.myNodes).mockResolvedValue(mine);
  vi.mocked(api.myAircraft).mockImplementation(() => Promise.resolve({ aircraft: ownerFeed }));
  vi.mocked(api.aircraft).mockImplementation(() => Promise.resolve({ aircraft: networkFeed }));
  vi.mocked(api.nodes).mockResolvedValue(LISTING);
}

/** Lets every settled request reach the page, round after round: the owner's
 *  list, then the feed it decides on. */
async function settle() {
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  }
}

async function wait(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
  await settle();
}

const filter = () => screen.getByRole("combobox", { name: "Node" }) as HTMLSelectElement;
const group = (label: string) => filter().querySelector<HTMLOptGroupElement>(`optgroup[label="${label}"]`);
const texts = (el: Element | null) => [...(el?.querySelectorAll("option") ?? [])].map((o) => o.textContent);
const row = (hex: string) => screen.getByText(hex).closest("tr")!;
// The Seen by cell, by its place under the header.
const seenBy = (hex: string) => {
  const column = screen.getAllByRole("columnheader").findIndex((th) => th.textContent === "Seen by");
  return row(hex).querySelectorAll("td")[column] as HTMLElement;
};
const hexes = () => screen.getAllByRole("row").slice(1).map((tr) => tr.querySelector("td")!.textContent);
const search = () => screen.getByTestId("search").textContent;

describe("DetectionsPage", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.useFakeTimers();
    state.auth = { user: ADA, loading: false };
    // usePolling logs every failure.
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  it("opens on the owner's nodes and polls only the owner feed, every 3 s", async () => {
    answer();
    renderAt();
    await settle();

    expect(screen.getByRole("radio", { name: /My nodes/ })).toBeChecked();
    expect(screen.getByText("Aircraft your nodes see, private ones included · updates every 3 s")).toBeInTheDocument();
    expect(api.myAircraft).toHaveBeenCalledTimes(1);

    await wait(3000);
    expect(api.myAircraft).toHaveBeenCalledTimes(2);
    await wait(3000);
    expect(api.myAircraft).toHaveBeenCalledTimes(3);
    expect(api.aircraft).not.toHaveBeenCalled();
  });

  it("lists a private node's aircraft, and the filter defaults to all my nodes with the owner's nodes marked", async () => {
    answer();
    renderAt();
    await settle();

    expect(hexes()).toEqual(["aa0001", "aa0002", "aa0003"]);
    expect(within(seenBy("aa0001")).getByText("Roof")).toBeInTheDocument();
    expect(within(seenBy("aa0001")).getByText("Yours")).toHaveClass("badge", "yours");

    expect(filter().value).toBe("");
    expect(filter().selectedOptions[0]).toHaveTextContent("All my nodes");
    // The owner's label, not the name the node publishes, and nobody else's.
    expect(texts(group("Your nodes"))).toEqual(["Mast", "Roof"]);
    expect(group("Network")).toBeNull();
    expect(screen.queryByRole("option", { name: "Hilltop" })).toBeNull();
  });

  it("switches to the whole network: polls the public feed, writes scope=network, and still marks the owner's nodes", async () => {
    answer();
    renderAt();
    await settle();

    fireEvent.click(screen.getByRole("radio", { name: "Whole network" }));
    await settle();

    expect(search()).toBe("?scope=network");
    expect(api.aircraft).toHaveBeenCalledTimes(1);
    expect(
      screen.getByText("Every aircraft on the network, except what only your private nodes see · updates every 3 s"),
    ).toBeInTheDocument();
    expect(hexes()).toEqual(["aa0002", "aa0003", "aa0004", "aa0005", "aa0006"]);

    // Only the feed on screen is polled.
    const ownerCalls = vi.mocked(api.myAircraft).mock.calls.length;
    await wait(3000);
    expect(api.aircraft).toHaveBeenCalledTimes(2);
    expect(api.myAircraft).toHaveBeenCalledTimes(ownerCalls);

    expect(filter().selectedOptions[0]).toHaveTextContent("All nodes");
    expect(texts(group("Your nodes"))).toEqual(["Mast · yours", "Roof · yours"]);
    expect(texts(group("Network"))).toEqual(["bare00000001", "Hilltop"]);

    // The owner's node first and marked; somebody else's named and not.
    const solve = seenBy("aa0003");
    expect([...solve.querySelectorAll(".seen-by-name")].map((el) => el.textContent)).toEqual(["Mast", "Hilltop"]);
    expect(within(solve).getAllByText("Yours")).toHaveLength(1);
    expect(within(solve).getByText("Mast").closest("li")).toContainElement(within(solve).getByText("Yours"));
    expect(within(seenBy("aa0004")).queryByText("Yours")).toBeNull();
    expect(within(seenBy("aa0005")).getByText("bare00000001")).toHaveClass("mono");
  });

  it("never shows the owner feed's aircraft under the network's heading while the network loads", async () => {
    answer();
    renderAt();
    await settle();
    vi.mocked(api.aircraft).mockImplementation(() => new Promise(() => {}));

    fireEvent.click(screen.getByRole("radio", { name: "Whole network" }));
    await settle();

    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("aa0001")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
    // The switch stays put, so focus and the way back stay with it.
    expect(screen.getByRole("radio", { name: "Whole network" })).toBeChecked();
  });

  it("opens on the whole network from ?scope=network", async () => {
    answer();
    renderAt("/detections?scope=network");
    await settle();

    expect(screen.getByRole("radio", { name: "Whole network" })).toBeChecked();
    expect(api.aircraft).toHaveBeenCalledTimes(1);
    expect(api.myAircraft).not.toHaveBeenCalled();
    expect(hexes()).toEqual(["aa0002", "aa0003", "aa0004", "aa0005", "aa0006"]);
  });

  it("filters by one node: keeps a multinode solve that lists it and its own detection, drops the rest", async () => {
    answer();
    renderAt("/detections?scope=network");
    await settle();

    fireEvent.change(filter(), { target: { value: MAST.node_ref } });

    expect(hexes()).toEqual(["aa0002", "aa0003"]);
    expect(screen.queryByText("aa0006")).toBeNull();
    const tracked = screen.getByText("Aircraft Tracked").closest(".stat-card");
    expect(tracked).toHaveTextContent("2");

    fireEvent.change(filter(), { target: { value: HILL } });
    expect(hexes()).toEqual(["aa0003", "aa0004", "aa0005", "aa0006"]);
  });

  it("starts the other feed with the filter and the page back at the start", async () => {
    const many = Array.from({ length: 30 }, (_, i) => track(`bb${String(i).padStart(4, "0")}`, { node_ref: MAST.node_ref }));
    answer({ ownerFeed: [...many, PRIVATE], networkFeed: [...many, HILLS] });
    renderAt();
    await settle();

    fireEvent.change(filter(), { target: { value: MAST.node_ref } });
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(screen.getByText(/Page 2 of 2/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "Whole network" }));
    await settle();

    expect(filter().value).toBe("");
    expect(screen.getByText(/Page 1 of 2/)).toBeInTheDocument();
    expect(search()).toBe("?scope=network");
  });

  it("says why a private node of the owner's has nothing on the network feed", async () => {
    answer();
    renderAt("/detections?scope=network");
    await settle();

    fireEvent.change(filter(), { target: { value: ROOF.node_ref } });

    expect(screen.getByText("Roof is private, so it is not on the network feed. My nodes shows what it sees.")).toBeInTheDocument();
  });

  it("cuts a long Seen by list to two names and a count, the owner's first", async () => {
    const crowd = track("cc0001", { multinode: true, contributing_node_refs: [HILL, BARE, "ndefour00000001", MAST.node_ref] });
    answer({ networkFeed: [crowd] });
    renderAt("/detections?scope=network");
    await settle();

    const cell = seenBy("cc0001");
    expect([...cell.querySelectorAll(".seen-by-name")].map((el) => el.textContent)).toEqual(["Mast", "Hilltop"]);
    expect(within(cell).getByText("+2 more")).toHaveAttribute("title", "bare00000001, four00000001");
    expect(within(cell).getByText("and 2 more: bare00000001, four00000001")).toBeInTheDocument();
    // A node the listing has not caught up with can still be chosen.
    expect(texts(group("Network"))).toContain("four00000001");
  });

  it("tells apart two of the owner's nodes that share a name", async () => {
    const twin = ownedNode({ node_id: "retroof0002", node_ref: "nderoof00000002", name: "Roof" });
    answer({ mine: [ROOF, twin] });
    renderAt();
    await settle();

    expect(texts(group("Your nodes"))).toEqual(["Roof (roof00000001)", "Roof (roof00000002)"]);
  });

  it("lets go of a chosen node once it leaves the owner's account", async () => {
    answer();
    renderAt();
    await settle();
    fireEvent.change(filter(), { target: { value: MAST.node_ref } });
    expect(hexes()).toEqual(["aa0002", "aa0003"]);

    vi.mocked(api.myNodes).mockResolvedValue([ROOF]);
    vi.mocked(api.myAircraft).mockImplementation(() => Promise.resolve({ aircraft: [PRIVATE] }));
    await wait(60_000);

    expect(filter().value).toBe("");
    expect(texts(group("Your nodes"))).toEqual(["Roof"]);
    expect(hexes()).toEqual(["aa0001"]);
  });

  it("with no nodes on the account shows the whole network, no switch, and the claim path", async () => {
    answer({ mine: [] });
    renderAt();
    await settle();

    expect(api.aircraft).toHaveBeenCalledTimes(1);
    expect(api.myAircraft).not.toHaveBeenCalled();
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.getByText("No nodes on your account yet, so this is the whole network.")).toBeInTheDocument();
    expect(document.querySelector(".claim-path-text")).toHaveTextContent(CLAIM_PATH);
    expect(screen.getByText("Every aircraft on the network · updates every 3 s")).toBeInTheDocument();
    expect(group("Your nodes")).toBeNull();
    expect(screen.queryByText("Yours")).toBeNull();
  });

  it("when the owner's nodes fail to load shows the whole network under a warning, and Retry asks again", async () => {
    answer();
    vi.mocked(api.myNodes).mockRejectedValue(new Error("Service unavailable"));
    renderAt();
    await settle();

    expect(api.aircraft).toHaveBeenCalledTimes(1);
    expect(api.myAircraft).not.toHaveBeenCalled();
    expect(screen.queryByRole("radio")).toBeNull();
    const notice = screen.getByRole("alert");
    expect(notice).toHaveTextContent("Could not load your nodes: Service unavailable. Showing the whole network.");
    expect(notice).toHaveClass("notice");
    expect(notice).not.toHaveClass("error");
    expect(hexes()).toEqual(["aa0002", "aa0003", "aa0004", "aa0005", "aa0006"]);

    vi.mocked(api.myNodes).mockResolvedValue([ROOF, MAST]);
    fireEvent.click(within(notice).getByRole("button", { name: "Retry" }));
    await settle();

    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("radio", { name: /My nodes/ })).toBeChecked();
    expect(api.myAircraft).toHaveBeenCalledTimes(1);
    expect(hexes()).toEqual(["aa0001", "aa0002", "aa0003"]);
  });

  it("signed out shows the whole network and never asks for the owner's nodes", async () => {
    state.auth = { user: null, loading: false };
    answer();
    renderAt();
    await settle();

    expect(api.myNodes).not.toHaveBeenCalled();
    expect(api.myAircraft).not.toHaveBeenCalled();
    expect(api.aircraft).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("radio")).toBeNull();
    expect(document.querySelector(".claim-path")).toBeNull();
    expect(hexes()).toEqual(["aa0002", "aa0003", "aa0004", "aa0005", "aa0006"]);
  });

  it("while the owner's nodes load shows a loading line and asks for neither feed", async () => {
    answer();
    vi.mocked(api.myNodes).mockReturnValue(new Promise<OwnedNode[]>(() => {}));
    renderAt("/detections?scope=network");
    await wait(6000);

    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(api.myAircraft).not.toHaveBeenCalled();
    expect(api.aircraft).not.toHaveBeenCalled();
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
  });
});
