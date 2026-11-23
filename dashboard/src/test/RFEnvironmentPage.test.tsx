import type { ReactNode } from "react";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RFEnvironmentPage from "../pages/user/RFEnvironmentPage";
import { throughLabel } from "../pages/user/rf/series";
import { CLAIM_PATH } from "../components/ClaimPath";
import type { OwnedNode, SnrSeries } from "../types";
import { resetAuth, setAuth } from "./fakeAuth";
import { ownedNode } from "./fixtures/ownedNode";

vi.mock("../context/AuthContext", async () => ({ useAuth: (await import("./fakeAuth")).useFakeAuth }));

// Each chart hands over the data it was given, and each line its key and
// name, so a test reads what would be drawn without a layout engine.
vi.mock("recharts", () => {
  const Pass = ({ children }: { children?: ReactNode }) => <div>{children}</div>;
  const LineChart = ({ data, children }: { data?: unknown; children?: ReactNode }) => (
    <figure data-testid="line-chart">
      <output>{JSON.stringify(data)}</output>
      {children}
    </figure>
  );
  const BarChart = ({ data }: { data?: unknown }) => (
    <figure data-testid="bar-chart">
      <output>{JSON.stringify(data)}</output>
    </figure>
  );
  const Line = ({ dataKey, name }: { dataKey?: string; name?: string }) => (
    <span data-testid="line" data-key={dataKey}>
      {name}
    </span>
  );
  const Nothing = () => null;
  return {
    ResponsiveContainer: Pass,
    LineChart,
    BarChart,
    Line,
    Bar: Nothing,
    Cell: Nothing,
    XAxis: Nothing,
    YAxis: Nothing,
    CartesianGrid: Nothing,
    Tooltip: Nothing,
    Legend: Nothing,
  };
});

const ADA = { id: "7", email: "ada@example.com", name: "Ada" };

const NOW = new Date("2026-10-13T12:00:00Z").getTime();
const BUCKET_MS = 300_000;
// Ten buckets back from the one the clock is in.
const BASE = Math.floor(NOW / BUCKET_MS) * BUCKET_MS - 10 * BUCKET_MS;

// The owner's nodes. Antenna is private, so the public listing leaves it out,
// and offline; Roof is public, online, and listed under the node's own name.
// Antenna sorts first, so opening on Roof is the online preference at work.
const ANTENNA = ownedNode({
  node_id: "retantenna01",
  node_ref: "ndeantenna0001x",
  name: "Antenna",
  status: "disconnected",
  location_private: true,
});
const ROOF = ownedNode({ node_id: "retroof0001", node_ref: "nderoof00000001", name: "Roof", status: "active" });

// Other people's nodes: one with a name, one published under its ref only.
const HILL = "ndehill00000001";
const BARE = "ndebare00000001";

const LISTING = {
  nodes: {
    [ROOF.node_ref]: { name: "roof-node", frequency: 98_500_000 },
    [HILL]: { name: "Hilltop", frequency: 101_100_000 },
    [BARE]: { name: BARE },
  },
};
// The public figures: Roof's here are not the owner's, which are unfuzzed.
const PUBLIC_ANALYTICS = {
  nodes: {
    [ROOF.node_ref]: { metrics: { avg_snr: 9.5 } },
    [HILL]: { metrics: { avg_snr: 14.2, total_frames: 1000, total_detections: 50 } },
    [BARE]: { metrics: { avg_snr: 7 } },
  },
};
const ROOF_ANALYTICS = {
  node_id: ROOF.node_id,
  node_ref: ROOF.node_ref,
  trust: {},
  detection_area: {},
  metrics: { avg_snr: 12.34, total_frames: 2000, total_detections: 400 },
  reputation: {},
  coverage_map: null,
  empirical_coverage: null,
  node_bias: null,
};

// Two buckets, two with no detection, then one: the server omits the empty ones.
const SERIES: SnrSeries = {
  hours: 24,
  bucket_s: 300,
  through_ms: BASE + 4 * BUCKET_MS + 120_000,
  points: [
    { t_ms: BASE, median_db: 10, p90_db: 14, count: 12 },
    { t_ms: BASE + BUCKET_MS, median_db: 11, p90_db: 15, count: 9 },
    { t_ms: BASE + 4 * BUCKET_MS, median_db: 12.5, p90_db: 16.25, count: 20 },
  ],
};
const NO_DATA: SnrSeries = { hours: 24, bucket_s: 300, through_ms: null, points: [] };

const snrPath = (node: OwnedNode) => `/api/auth/me/nodes/${node.node_id}/snr?hours=24`;
const analyticsPath = (node: OwnedNode) => `/api/auth/me/nodes/${node.node_id}/analytics`;

type Answer = { status: number; body: unknown };

/** What each path answers, by the whole path and query. One not listed is a 404. */
let routes: Map<string, () => Answer | Promise<Answer>>;

const ok = (body: unknown) => () => ({ status: 200, body });
const failing = () => ({ status: 500, body: { detail: "boom" } });

function serve({ mine = [ANTENNA, ROOF], series = SERIES }: { mine?: OwnedNode[]; series?: SnrSeries } = {}) {
  routes = new Map<string, () => Answer | Promise<Answer>>([
    ["/api/radar/nodes", ok(LISTING)],
    ["/api/radar/analytics", ok(PUBLIC_ANALYTICS)],
    ["/api/auth/me/nodes", ok(mine)],
    [analyticsPath(ROOF), ok(ROOF_ANALYTICS)],
    [analyticsPath(ANTENNA), ok(null)],
    [snrPath(ROOF), ok(series)],
    [snrPath(ANTENNA), ok(NO_DATA)],
  ]);
}

const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
  const answer = await (routes.get(String(input)) ?? (() => ({ status: 404, body: { detail: "Not found" } })))();
  return new Response(JSON.stringify(answer.body), {
    status: answer.status,
    headers: { "Content-Type": "application/json" },
  });
});

const requested = () => fetchMock.mock.calls.map(([input]) => String(input));
const requestsOf = (path: string) => requested().filter((p) => p === path).length;

/** Lets every settled request reach the page, round after round: the owner's
 *  list and the network, then what the choice they make asks for. */
async function settle() {
  for (let i = 0; i < 6; i++) {
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

async function open() {
  const view = render(<RFEnvironmentPage />);
  await settle();
  return view;
}

const picker = () => screen.getByRole("combobox", { name: "Node" }) as HTMLSelectElement;
const group = (label: string) => picker().querySelector<HTMLOptGroupElement>(`optgroup[label="${label}"]`);
const texts = (el: Element | null) => [...(el?.querySelectorAll("option") ?? [])].map((o) => o.textContent);
const choose = async (value: string) => {
  fireEvent.change(picker(), { target: { value } });
  await settle();
};
/** What a chart was handed to draw. */
const drawn = (testId: string) => JSON.parse(screen.getByTestId(testId).querySelector("output")?.textContent ?? "null");
const lineData = () => drawn("line-chart");
const lines = () => screen.getAllByTestId("line").map((l) => [l.getAttribute("data-key"), l.textContent]);
const trendCard = () => screen.getByTestId("line-chart").closest(".card") as HTMLElement;
// "Average SNR" names a tile and a row of the details table alike.
const stat = (label: string) =>
  within(document.querySelector(".stats-grid") as HTMLElement).getByText(label).nextElementSibling;
const detail = (label: string) =>
  within(screen.getByRole("table")).getByText(label).nextElementSibling;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "setInterval", "clearInterval", "Date"] });
  vi.setSystemTime(NOW);
  resetAuth();
  setAuth({ user: ADA, loading: false });
  fetchMock.mockClear();
  vi.stubGlobal("fetch", fetchMock);
  // usePolling logs every failure.
  vi.spyOn(console, "error").mockImplementation(() => {});
  serve();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("RF Environment for an owner", () => {
  it("opens on the owner's node that is online, lists their nodes first and marks the choice", async () => {
    await open();

    expect(picker().value).toBe(`mine:${ROOF.node_id}`);
    // The owner's group comes first; a private node says so in its option.
    expect([...picker().querySelectorAll("optgroup")].map((g) => g.label)).toEqual(["Your nodes", "Network"]);
    expect(texts(group("Your nodes"))).toEqual(["Antenna · private", "Roof"]);
    // Roof is on the listing too, and offered once, as the owner's.
    expect(texts(group("Network"))).toEqual(["bare00000001", "Hilltop"]);
    expect(screen.getByText("Yours")).toHaveClass("badge", "yours");
    expect(screen.getByText("2 yours · 2 on the network")).toBeInTheDocument();
  });

  it("opens on the owner's first node when none of them is online", async () => {
    serve({ mine: [{ ...ROOF, status: "disconnected" }, ANTENNA] });
    await open();

    expect(picker().value).toBe(`mine:${ANTENNA.node_id}`);
  });

  it("draws the server's series for an owned node, the owner's figures beside it", async () => {
    await open();

    expect(requestsOf(snrPath(ROOF))).toBe(1);
    expect(screen.getByRole("heading", { name: "SNR, last 24 hours · 5-minute buckets" })).toBeInTheDocument();
    // The two missing buckets are one gap, which breaks both lines there.
    expect(lineData()).toEqual([
      { t_ms: BASE, median_db: 10, p90_db: 14, count: 12 },
      { t_ms: BASE + BUCKET_MS, median_db: 11, p90_db: 15, count: 9 },
      { t_ms: BASE + 2 * BUCKET_MS, median_db: null, p90_db: null, count: 0, gap_until: BASE + 4 * BUCKET_MS },
      { t_ms: BASE + 4 * BUCKET_MS, median_db: 12.5, p90_db: 16.25, count: 20 },
    ]);
    expect(lines()).toEqual([
      ["median_db", "Median (dB)"],
      ["p90_db", "90th percentile (dB)"],
    ]);
    expect(within(trendCard()).getByText(throughLabel(SERIES.through_ms!, NOW))).toBeInTheDocument();
    expect(within(trendCard()).getByText(/41 detections in the last 24 hours/)).toBeInTheDocument();
    expect(screen.queryByText(/since you opened this page/)).toBeNull();

    // The owner's unfuzzed analytics, not the public listing's 9.5.
    expect(requestsOf(analyticsPath(ROOF))).toBe(1);
    expect(stat("Average SNR")).toHaveTextContent("12.3 dB");
    expect(detail("Average SNR")).toHaveTextContent("12.34 dB");
    expect(detail("Detection Rate")).toHaveTextContent("20.0%");
    expect(screen.getByText("From your node's own data")).toBeInTheDocument();
  });

  it("says plainly when an owned node has no detections in the window yet", async () => {
    serve({ series: NO_DATA });
    await open();

    expect(screen.getByText("No detections from this node in the last 24 hours.")).toBeInTheDocument();
    expect(screen.queryByTestId("line-chart")).toBeNull();
    expect(screen.queryByText(/^through /)).toBeNull();
  });

  it("offers a private node the public listing leaves out, and reads its series by node_id", async () => {
    await open();
    await choose(`mine:${ANTENNA.node_id}`);

    expect(requestsOf(snrPath(ANTENNA))).toBe(1);
    expect(screen.getByText("Private")).toHaveClass("badge", "private");
    expect(screen.getByText("No detections from this node in the last 24 hours.")).toBeInTheDocument();
    // Its analytics answered that there are none yet.
    expect(screen.getByText("No analytics for this node yet")).toBeInTheDocument();
  });

  it("shows the live trend for a node that is not the owner's, sampled since the page opened, and back", async () => {
    await open();
    await wait(5000);
    await wait(5000);
    await choose(HILL);

    expect(screen.getByRole("heading", { name: "Average SNR since you opened this page · sampled every 5 s" })).toBeInTheDocument();
    // Three answers since the page opened, each one a sample, the first of
    // them taken while the owner's node was on screen.
    expect(lineData()).toEqual([
      { t: NOW, snr: 14.2 },
      { t: NOW + 5000, snr: 14.2 },
      { t: NOW + 10_000, snr: 14.2 },
    ]);
    expect(lines()).toEqual([["snr", "Average SNR (dB)"]]);
    expect(screen.getByText("As the network publishes it")).toBeInTheDocument();
    expect(screen.queryByText("Yours")).toBeNull();
    // Nothing of anyone else's is asked of the owner routes.
    expect(requested().filter((p) => p.includes(HILL))).toEqual([]);

    await choose(`mine:${ROOF.node_id}`);
    expect(screen.getByRole("heading", { name: "SNR, last 24 hours · 5-minute buckets" })).toBeInTheDocument();
    expect(requestsOf(snrPath(ROOF))).toBe(2);
  });

  it("asks for the series once a minute, as the server refreshes it, and stops while it is not shown", async () => {
    await open();
    await wait(59_000);
    expect(requestsOf(snrPath(ROOF))).toBe(1);
    await wait(1000);
    expect(requestsOf(snrPath(ROOF))).toBe(2);

    await choose(HILL);
    await wait(120_000);
    expect(requestsOf(snrPath(ROOF))).toBe(2);
  });

  it("keeps the page when the series fails, and says so in the trend card", async () => {
    routes.set(snrPath(ROOF), failing);
    await open();

    const card = screen.getByRole("heading", { name: "SNR, last 24 hours · 5-minute buckets" }).closest(".card") as HTMLElement;
    expect(within(card).getByRole("alert")).toHaveTextContent("Could not load this node's SNR history");
    expect(stat("Average SNR")).toHaveTextContent("12.3 dB");
  });

  it("does not show one owned node's series as another's while the next is on its way", async () => {
    let release: () => void = () => {};
    routes.set(snrPath(ANTENNA), () => new Promise<Answer>((resolve) => (release = () => resolve(ok(NO_DATA)()))));
    await open();
    await choose(`mine:${ANTENNA.node_id}`);

    expect(screen.queryByTestId("line-chart")).toBeNull();
    expect(screen.getAllByText("Loading…").length).toBeGreaterThan(0);
    release();
    await settle();
    expect(screen.getByText("No detections from this node in the last 24 hours.")).toBeInTheDocument();
  });

  it("says which of the owner's figures failed, and dashes them rather than wait forever", async () => {
    routes.set(analyticsPath(ROOF), failing);
    await open();

    expect(screen.getByText(/Could not load this node's analytics/)).toBeInTheDocument();
    expect(stat("Average SNR")).toHaveTextContent("—");
    // The series has its own request, which answered.
    expect(lineData()).toHaveLength(4);
  });
});

describe("RF Environment's choice of node", () => {
  it("waits for the owner's list, so it never opens on a network node and jumps", async () => {
    let release: () => void = () => {};
    routes.set("/api/auth/me/nodes", () => new Promise<Answer>((resolve) => (release = () => resolve(ok([ANTENNA, ROOF])()))));
    await open();

    expect(screen.queryByRole("combobox")).toBeNull();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    release();
    await settle();
    expect(picker().value).toBe(`mine:${ROOF.node_id}`);
  });

  it("shows the owner's node before the network answers, and adds the network when it does", async () => {
    let release: () => void = () => {};
    routes.set("/api/radar/nodes", () => new Promise<Answer>((resolve) => (release = () => resolve(ok(LISTING)()))));
    await open();

    expect(picker().value).toBe(`mine:${ROOF.node_id}`);
    expect(group("Network")).toBeNull();
    expect(screen.getByText("2 yours")).toBeInTheDocument();
    expect(lineData()).toHaveLength(4);
    release();
    await settle();
    expect(texts(group("Network"))).toEqual(["bare00000001", "Hilltop"]);
    expect(picker().value).toBe(`mine:${ROOF.node_id}`);
  });

  it("keeps its first choice when another of the owner's nodes comes online", async () => {
    serve({ mine: [ANTENNA, { ...ROOF, status: "disconnected" }] });
    await open();
    expect(picker().value).toBe(`mine:${ANTENNA.node_id}`);

    routes.set("/api/auth/me/nodes", ok([ANTENNA, ROOF]));
    await wait(60_000);
    expect(requestsOf("/api/auth/me/nodes")).toBe(2);
    expect(picker().value).toBe(`mine:${ANTENNA.node_id}`);
  });

  it("keeps the viewer's choice across refreshes", async () => {
    await open();
    await choose(BARE);
    await wait(5000);
    await wait(60_000);
    expect(picker().value).toBe(BARE);
  });

  it("moves to the owner's next node when the one shown leaves the account", async () => {
    await open();
    await choose(`mine:${ANTENNA.node_id}`);

    routes.set("/api/auth/me/nodes", ok([ROOF]));
    await wait(60_000);
    expect(picker().value).toBe(`mine:${ROOF.node_id}`);
    expect(texts(group("Your nodes"))).toEqual(["Roof"]);
  });
});

describe("RF Environment without nodes of one's own", () => {
  it("shows a signed-in owner with no nodes the network as before, and how to add one", async () => {
    serve({ mine: [] });
    await open();

    // The first by name: the bare ref goes by its short form, which sorts first.
    expect(picker().value).toBe(BARE);
    expect(picker().querySelector("optgroup")).toBeNull();
    expect(texts(picker())).toEqual(["bare00000001", "Hilltop", "roof-node"]);
    expect(screen.getByText(CLAIM_PATH, { exact: false })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Average SNR since you opened this page · sampled every 5 s" })).toBeInTheDocument();
    expect(screen.getByText("3 nodes")).toBeInTheDocument();
    expect(requested().filter((p) => p.startsWith("/api/auth/me/nodes/"))).toEqual([]);
  });

  it("asks a signed-out visitor's page for no owner route at all", async () => {
    setAuth({ user: null, loading: false });
    await open();

    expect(picker().value).toBe(BARE);
    expect(requested().filter((p) => p.startsWith("/api/auth/"))).toEqual([]);
    expect(screen.queryByText(CLAIM_PATH, { exact: false })).toBeNull();
  });

  it("falls back to the network when the owner's list fails, and says so", async () => {
    routes.set("/api/auth/me/nodes", failing);
    await open();

    expect(screen.getByRole("alert")).toHaveTextContent("Could not load your nodes");
    expect(picker().value).toBe(BARE);
  });

  it("marks the owner's own bars in the network chart", async () => {
    await open();

    expect(drawn("bar-chart")).toEqual([
      { name: "Hilltop", snr: 14.2, mine: false },
      { name: "roof-node", snr: 9.5, mine: true },
      { name: "bare00000001", snr: 7, mine: false },
    ]);
  });
});

describe("RF Environment when the network fails", () => {
  it("still shows the owner's nodes, and says the network's figures are missing", async () => {
    routes.set("/api/radar/nodes", failing);
    routes.set("/api/radar/analytics", failing);
    await open();

    expect(picker().value).toBe(`mine:${ROOF.node_id}`);
    // How many are on the network is unknown, not none.
    expect(screen.getByText("2 yours")).toBeInTheDocument();
    expect(screen.getByText(/Could not load the network.s figures/)).toBeInTheDocument();
    expect(screen.getByText("The network’s figures could not be loaded.")).toBeInTheDocument();
    expect(lineData()).toHaveLength(4);
  });

  it("shows only the error to a visitor with nothing else to show", async () => {
    setAuth({ user: null, loading: false });
    routes.set("/api/radar/nodes", failing);
    await open();

    expect(screen.getByRole("alert")).toHaveTextContent("Could not load RF environment data");
    expect(screen.queryByRole("combobox")).toBeNull();
    expect(document.querySelector(".stat-card")).toBeNull();
  });
});
