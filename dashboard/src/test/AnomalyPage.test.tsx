import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import { CLAIM_PATH } from "../components/ClaimPath";
import AnomalyPage from "../pages/user/AnomalyPage";
import type { FeedAircraft } from "../types";
import { ownedNode } from "./fixtures/ownedNode";

vi.mock("../api/client", () => ({
  api: { anomalies: vi.fn(), myNodes: vi.fn(), myAircraft: vi.fn() },
}));
vi.mock("recharts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("recharts")>()),
  ResponsiveContainer: () => null,
}));

type Auth = { user: { id: number; email: string; name: string } | null; loading: boolean };
const state = vi.hoisted(() => ({ auth: { user: null, loading: false } as Auth }));
vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

const ADA = { id: 1, email: "a@example.com", name: "Ada" };

// The owner's two nodes: Roof is private, so only the owner feed carries what it
// sees; Mast is public.
const ROOF = ownedNode({ node_id: "retroof0001", node_ref: "nderoof00000001", name: "Roof", location_private: true });
const MAST = ownedNode({ node_id: "retmast0001", node_ref: "ndemast00000001", name: "Mast" });
// Somebody else's node.
const HILL = "ndehill00000001";

const flagged = (hex: string, fields: Partial<FeedAircraft> = {}): FeedAircraft => ({
  hex,
  lat: 51.5,
  lon: -0.1,
  alt_baro: 32000,
  gs: 420,
  track: 90,
  seen: 1,
  is_anomalous: true,
  anomaly_types: ["spoofed_position"],
  ...fields,
});

const ON_ROOF = flagged("aa0001", { flight: "PRV1", node_ref: ROOF.node_ref ?? undefined });
// A multinode solve names its nodes in contributing_node_refs; one is another owner's.
const SOLVE = flagged("aa0003", {
  multinode: true,
  anomaly_types: ["impossible_speed"],
  contributing_node_refs: [HILL, MAST.node_ref ?? ""],
});
const CALM = flagged("aa0002", { node_ref: MAST.node_ref ?? undefined, is_anomalous: false, anomaly_types: [] });
// Flagged, but not behind either of the owner's nodes.
const THEIRS = flagged("aa0004", { node_ref: HILL });
const OWNER_FEED = [ON_ROOF, SOLVE, CALM, THEIRS];

const NETWORK = {
  summary: { active_count: 7, total_events: 42, unique_hexes: 5, most_common_type: "anomalous_behavior" },
  by_type: { anomalous_behavior: 42 },
  timeline: [],
  geographic_clusters: [{ lat: 51.5, lon: -0.1, count: 3, dominant_type: "anomalous_behavior" }],
  recent_events: [
    { hex: "ee0001", reason: "anomalous_behavior", lat: 51.5, lon: -0.1, flagged_at: "2026-10-12T10:00:00Z" },
  ],
};

/** The page with the query string it writes beside it. */
function Where() {
  return <output data-testid="search">{useLocation().search}</output>;
}

function renderAt(entry = "/anomalies") {
  return render(
    <MemoryRouter initialEntries={[entry]}>
      <Routes>
        <Route
          path="/anomalies"
          element={
            <>
              <AnomalyPage />
              <Where />
            </>
          }
        />
      </Routes>
    </MemoryRouter>,
  );
}

function answer({ mine = [ROOF, MAST] } = {}) {
  vi.mocked(api.myNodes).mockResolvedValue(mine);
  vi.mocked(api.myAircraft).mockImplementation(() => Promise.resolve({ aircraft: OWNER_FEED }));
  vi.mocked(api.anomalies).mockResolvedValue(NETWORK);
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

const realLocation = window.location;

function onHost(hostname: string) {
  Object.defineProperty(window, "location", {
    value: { hostname, pathname: "/", search: "", hash: "", href: `https://${hostname}/` },
    writable: true,
    configurable: true,
  });
}

const card = (title: string) => screen.getByRole("heading", { name: title }).closest(".card") as HTMLElement;
const liveCard = () => card("Anomalous Aircraft on Your Nodes");
const row = (hex: string) => within(liveCard()).getByText(hex).closest("tr")!;
const search = () => screen.getByTestId("search").textContent;

describe("AnomalyPage", () => {
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
    Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
  });

  it("opens on My nodes, polls the owner feed and lists only anomalous aircraft behind the owner's nodes, private ones included", async () => {
    answer();
    renderAt();
    await settle();

    expect(screen.getByRole("radio", { name: /My nodes/ })).toBeChecked();
    expect(search()).toBe("");
    expect(api.myAircraft).toHaveBeenCalledTimes(1);

    const live = within(liveCard());
    // Not the unflagged aircraft on the owner's node, not the flagged one on somebody else's.
    expect(live.getAllByRole("row").slice(1).map((tr) => tr.querySelector("td")!.textContent)).toEqual([
      expect.stringContaining("aa0001"),
      expect.stringContaining("aa0003"),
    ]);
    expect(live.queryByText("aa0002")).toBeNull();
    expect(live.queryByText("aa0004")).toBeNull();

    // The private node's aircraft names the node, flagged private.
    const privateRow = within(row("aa0001"));
    expect(privateRow.getByRole("link", { name: "Roof" })).toHaveAttribute("href", `/nodes/${ROOF.node_ref}`);
    expect(privateRow.getByText("Private")).toBeInTheDocument();
    expect(privateRow.getByText("spoofed position")).toBeInTheDocument();
    expect(privateRow.queryByText(/other node/)).toBeNull();

    // The solve names the owner's node and counts the other owner's.
    const solveRow = within(row("aa0003"));
    expect(solveRow.getByRole("link", { name: "Mast" })).toBeInTheDocument();
    expect(solveRow.queryByRole("link", { name: "Roof" })).toBeNull();
    expect(solveRow.getByText("with 1 other node")).toBeInTheDocument();

    // The tiles are the owner's: two aircraft, both nodes seeing one.
    expect(screen.getByText("Anomalous Aircraft").closest(".stat-card")).toHaveTextContent("2");
    expect(screen.getByText("Your Nodes Seeing One").closest(".stat-card")).toHaveTextContent("of 2");

    await wait(10_000);
    expect(api.myAircraft).toHaveBeenCalledTimes(2);
  });

  it("labels the sections that follow the live one as the whole network's, the log naming no node", async () => {
    answer();
    renderAt();
    await settle();

    expect(screen.getByRole("heading", { name: "Whole network" })).toBeInTheDocument();
    expect(screen.getByText("The anomaly log does not record which node saw an event.")).toBeInTheDocument();
    // Below it: the network's timeline, breakdown, hotspots and events, in place of its stat tiles.
    expect(screen.getByRole("heading", { name: "Event Timeline (24h)" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Geographic Hotspots" })).toBeInTheDocument();
    expect(within(card("Recent Anomaly Events")).getByText("ee0001")).toBeInTheDocument();
    expect(screen.queryByText("Active Anomalies")).toBeNull();
  });

  it("writes ?scope=network on switching to Whole network, stops polling the owner feed and shows the network's live summary", async () => {
    answer();
    renderAt();
    await settle();
    expect(screen.queryByText("Active Anomalies")).toBeNull();
    expect(api.myAircraft).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("radio", { name: "Whole network" }));
    await settle();

    expect(search()).toBe("?scope=network");
    expect(screen.getByRole("radio", { name: "Whole network" })).toBeChecked();
    expect(screen.queryByRole("heading", { name: "Anomalous Aircraft on Your Nodes" })).toBeNull();
    expect(screen.getByText("Active Anomalies").closest(".stat-card")).toHaveTextContent("7");
    expect(screen.getByText("Total Events").closest(".stat-card")).toHaveTextContent("42");

    await wait(30_000);
    expect(api.myAircraft).toHaveBeenCalledTimes(1);
    // The network's own data keeps its period.
    expect(vi.mocked(api.anomalies).mock.calls.length).toBeGreaterThan(1);

    fireEvent.click(screen.getByRole("radio", { name: /My nodes/ }));
    await settle();
    expect(search()).toBe("");
    expect(api.myAircraft).toHaveBeenCalledTimes(2);
  });

  it("opens on Whole network when the link says ?scope=network, without asking for the owner feed", async () => {
    answer();
    renderAt("/anomalies?scope=network");
    await settle();

    expect(screen.getByRole("radio", { name: "Whole network" })).toBeChecked();
    expect(screen.getByText("Active Anomalies").closest(".stat-card")).toHaveTextContent("7");
    expect(screen.queryByRole("heading", { name: "Anomalous Aircraft on Your Nodes" })).toBeNull();
    await wait(30_000);
    expect(api.myAircraft).not.toHaveBeenCalled();
  });

  it("offers an owner with no nodes the claim path and the network page, with no switch and no owner feed", async () => {
    answer({ mine: [] });
    renderAt();
    await settle();

    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.getByText(/No nodes on your account yet/)).toBeInTheDocument();
    expect(screen.getByText(CLAIM_PATH, { exact: false })).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
    // The network page, tiles and all, and nothing to call the rest of it "whole" against.
    expect(screen.getByText("Active Anomalies").closest(".stat-card")).toHaveTextContent("7");
    expect(screen.queryByRole("heading", { name: "Whole network" })).toBeNull();

    await wait(30_000);
    expect(api.myAircraft).not.toHaveBeenCalled();
  });

  it.each([
    ["Signed out", "localhost", { user: null, loading: false }],
    ["On the admin host, signed in", "admin.retina.fm", { user: ADA, loading: false }],
  ])("%s: shows the network page alone, with no switch and no call for the owner's nodes or feed", async (_who, host, auth) => {
    onHost(host);
    state.auth = auth;
    answer();
    renderAt();
    await settle();

    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.queryByText(/No nodes on your account yet/)).toBeNull();
    expect(screen.getByText("Active Anomalies").closest(".stat-card")).toHaveTextContent("7");
    expect(within(card("Recent Anomaly Events")).getByText("ee0001")).toBeInTheDocument();

    await wait(60_000);
    expect(api.myNodes).not.toHaveBeenCalled();
    expect(api.myAircraft).not.toHaveBeenCalled();
  });

  it("warns, with Retry, over the network page when the owner's list fails, and offers the switch once Retry works", async () => {
    answer();
    vi.mocked(api.myNodes).mockRejectedValue(new Error("HTTP 500"));
    renderAt();
    await settle();

    const notice = screen.getByRole("alert");
    expect(notice).toHaveClass("notice");
    expect(notice).not.toHaveClass("error");
    expect(notice).toHaveTextContent("Could not load your nodes: HTTP 500");
    expect(within(notice).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    // Over the network page, which needs no list.
    expect(screen.getByText("Active Anomalies").closest(".stat-card")).toHaveTextContent("7");
    expect(screen.queryByRole("radio")).toBeNull();
    expect(api.myAircraft).not.toHaveBeenCalled();

    vi.mocked(api.myNodes).mockResolvedValue([ROOF, MAST]);
    fireEvent.click(within(notice).getByRole("button", { name: "Retry" }));
    await settle();

    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.getByRole("radio", { name: /My nodes/ })).toBeChecked();
    expect(api.myAircraft).toHaveBeenCalledTimes(1);
  });
});
