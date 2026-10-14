import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { HttpError } from "@retina/shared";
import { api } from "../api/client";
import { NodeOwnerRecords } from "../components/nodeOwner/NodeOwnerRecords";
import type { ConfigVersion, NodeReport, OwnerEvent } from "../types";

vi.mock("../api/client", () => ({
  api: { myNodeReport: vi.fn(), myNodeConfigHistory: vi.fn(), myNodeEvents: vi.fn() },
}));

const NODE = "ret0a1b2c3d";
const NOW = new Date("2026-10-12T12:00:00Z");

/** A server stamp `seconds` before NOW, in the `Z` form the owner routes send. */
const ago = (seconds: number) => new Date(NOW.getTime() - seconds * 1000).toISOString().replace(".000Z", "Z");

const REPORT: NodeReport = {
  received_at: ago(5 * 60),
  state: "stalled",
  state_since: ago(2 * 3600),
  uptime_s: 3 * 86400 + 4 * 3600 + 59,
  config_version: 7,
  health: {
    cpu_pct: 12.5,
    temp_c: 48.2,
    disk_free_mb: 2048,
    blah2: "up",
    adsb: "down",
    // Readings the console has no wording for: one of each shape.
    fan_ok: true,
    mode: "survey",
    gps: { fix: "3d", sats: 9, locked: true },
    load: [0.5, 0.25],
    spare: null,
  },
  versions: { owl_os: "2026.09.1", retina_node: "0.9.2", blah2_image: "v1.4.0", retina_tracker: null },
  // As the server keeps them: oldest first, the two of one beat sharing its stamp.
  errors: [
    { at: ago(3 * 3600), message: "SDR overflow" },
    { at: ago(60), message: "blah2 restarted" },
    { at: ago(60), message: "frame upload timed out" },
  ],
};

const V1: ConfigVersion = {
  version: 1,
  created_at: ago(30 * 86400),
  superseded_at: ago(10 * 86400),
  fc_hz: 174_928_000,
  fs_hz: 2_048_000,
  rx_lat: 52.370216,
  rx_lon: 4.895168,
  rx_alt_ft: 30,
  tx_lat: 52.1,
  tx_lon: 5.2,
  tx_alt_ft: 1200,
  tx_callsign: "5A",
  beam_width_deg: null,
  beam_azimuth_deg: 90,
  max_range_km: 60,
  cpi_s: 0.5,
  delay_tolerance_us: 2,
  doppler_tolerance_hz: 5,
};
// Retuned: carrier and sample rate move.
const V2: ConfigVersion = {
  ...V1,
  version: 2,
  created_at: ago(10 * 86400),
  superseded_at: ago(86400),
  fc_hz: 202_928_000,
  fs_hz: 2_400_000,
};
// Moved: only the receiver's latitude, below a metre of which nothing shows.
const V3: ConfigVersion = { ...V2, version: 3, created_at: ago(86400), superseded_at: null, rx_lat: 52.3703 };
const VERSIONS = [V3, V2, V1];

const EVENTS: OwnerEvent[] = [
  { kind: "renamed", at: ago(60), actor: "you", epoch: null },
  { kind: "made_private", at: ago(3600), actor: "owner", epoch: null },
  { kind: "graduated", at: ago(86400), actor: "admin", epoch: 2 },
  { kind: "registered", at: ago(2 * 86400), actor: "system", epoch: 1 },
];

function serve({
  report = REPORT as NodeReport | null,
  versions = VERSIONS,
  events = EVENTS,
}: { report?: NodeReport | null; versions?: ConfigVersion[]; events?: OwnerEvent[] } = {}) {
  vi.mocked(api.myNodeReport).mockResolvedValue(report);
  vi.mocked(api.myNodeConfigHistory).mockResolvedValue({ versions });
  vi.mocked(api.myNodeEvents).mockResolvedValue({ events });
}

const onGone = vi.fn();

const card = (name: string) => screen.getByRole("region", { name });

/** Every card has answered, one way or the other. */
async function settled() {
  await waitFor(() => {
    for (const name of ["Last report", "Configuration history", "History"]) {
      expect(card(name)).not.toHaveAttribute("aria-busy", "true");
      expect(within(card(name)).queryByText("Loading…")).toBeNull();
    }
  });
}

async function renderRecords(props: { nodeId?: string; refreshKey?: number } = {}) {
  const view = render(<NodeOwnerRecords nodeId={props.nodeId ?? NODE} refreshKey={props.refreshKey} onGone={onGone} />);
  await settled();
  return view;
}

/** The reading beside a label in one of the report's tables. */
function reading(region: HTMLElement, label: string): HTMLElement {
  const cell = within(region).getByText(label, { selector: "td" });
  return cell.nextElementSibling as HTMLElement;
}

/** One version's entry, newest first. */
const versionItems = () => within(card("Configuration history")).getAllByRole("listitem");

/** One value of a version, as the whole field that holds its label and value. */
function field(item: HTMLElement, label: string): HTMLElement {
  return within(item).getByText(label, { selector: "dt" }).parentElement as HTMLElement;
}

const exact = (iso: string) => new Date(iso).toLocaleString();

// Anything React or the page logs that a failing route does not explain is a
// failure of its own: a key warning, an update outside act, a crash.
let errors: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  vi.resetAllMocks();
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  errors = vi.spyOn(console, "error").mockImplementation(() => {});
  serve();
});

afterEach(() => {
  // usePolling logs each failed request as the Error it is; nothing else may log.
  const unexpected = errors.mock.calls.filter(([first]) => !(first instanceof Error));
  expect(unexpected).toEqual([]);
  errors.mockRestore();
  vi.useRealTimers();
});

describe("NodeOwnerRecords: last report", () => {
  it("shows when the report arrived, relative and exact, with the node's state, uptime and configuration version", async () => {
    await renderRecords();
    const report = card("Last report");

    const received = reading(report, "Received");
    expect(received).toHaveTextContent("5m ago");
    const time = received.querySelector("time");
    expect(time).toHaveAttribute("datetime", new Date(REPORT.received_at).toISOString());
    expect(time).toHaveTextContent(exact(REPORT.received_at));

    const state = within(reading(report, "State")).getByText("Stalled");
    expect(state).toHaveClass("badge", "warning");
    expect(reading(report, "In this state since")).toHaveTextContent("2h ago");
    expect(reading(report, "Uptime")).toHaveTextContent("3d 4h");
    expect(reading(report, "Configuration version")).toHaveTextContent("7");
  });

  it("names the software versions the node reported, a missing one as a dash", async () => {
    await renderRecords();
    const software = within(card("Last report")).getByRole("region", { name: "Software" });
    expect(reading(software, "Operating system (owl-os)")).toHaveTextContent("2026.09.1");
    expect(reading(software, "Node software (retina-node)")).toHaveTextContent("0.9.2");
    expect(reading(software, "Radar software (blah2)")).toHaveTextContent("v1.4.0");
    expect(reading(software, "Tracker release")).toHaveTextContent("—");
    expect(within(reading(software, "Node software (retina-node)")).getByText("0.9.2")).toHaveClass("mono");
  });

  it("formats the contract's health readings in their units and services as badges", async () => {
    await renderRecords();
    const health = within(card("Last report")).getByRole("region", { name: "Health" });
    expect(reading(health, "CPU load")).toHaveTextContent("12.5%");
    expect(reading(health, "Temperature")).toHaveTextContent("48.2 °C");
    expect(reading(health, "Free disk space")).toHaveTextContent("2.00 GB");
    expect(within(reading(health, "Radar service (blah2)")).getByText("Up")).toHaveClass("badge", "online");
    expect(within(reading(health, "ADS-B receiver")).getByText("Down")).toHaveClass("badge", "offline");
  });

  it("shows other health values plainly and nested ones compactly, never as [object Object]", async () => {
    await renderRecords();
    const health = within(card("Last report")).getByRole("region", { name: "Health" });
    expect(reading(health, "Fan ok")).toHaveTextContent(/^Yes$/);
    expect(reading(health, "Mode")).toHaveTextContent(/^survey$/);
    expect(reading(health, "Spare")).toHaveTextContent(/^—$/);
    const gps = within(reading(health, "Gps")).getByText('{fix: "3d", sats: 9, locked: true}');
    expect(gps.tagName).toBe("CODE");
    expect(gps).toHaveClass("mono");
    expect(reading(health, "Load")).toHaveTextContent("[0.5, 0.25]");
    expect(card("Last report").textContent).not.toContain("[object Object]");
  });

  it("reads ADS-B left out of the beat as disabled, as the contract does", async () => {
    const health = { ...REPORT.health };
    delete health.adsb;
    serve({ report: { ...REPORT, health } });
    await renderRecords();
    const readings = within(card("Last report")).getByRole("region", { name: "Health" });
    expect(reading(readings, "ADS-B receiver")).toHaveTextContent(/^Disabled$/);
  });

  it("says so when the report carries no health block, ADS-B included", async () => {
    serve({ report: { ...REPORT, health: {} } });
    await renderRecords();
    const readings = within(card("Last report")).getByRole("region", { name: "Health" });
    expect(readings).toHaveTextContent("This report carries no health readings.");
    expect(within(readings).queryByText("ADS-B receiver")).toBeNull();
  });

  it("lists the errors it reported newest first, the later of one beat's first", async () => {
    await renderRecords();
    const errorsPart = within(card("Last report")).getByRole("region", { name: /Errors reported/ });
    const items = within(errorsPart).getAllByRole("listitem");
    expect(items.map((li) => li.querySelector("p")?.textContent)).toEqual([
      "frame upload timed out",
      "blah2 restarted",
      "SDR overflow",
    ]);
    expect(items[0]).toHaveTextContent("1m ago");
    expect(items[2]).toHaveTextContent("3h ago");
    expect(within(errorsPart).getByText("3")).toHaveClass("badge", "plain");
  });

  it("says no errors were reported when there are none", async () => {
    serve({ report: { ...REPORT, errors: [] } });
    await renderRecords();
    expect(within(card("Last report")).getByText("No errors reported since this node joined your account.")).toBeInTheDocument();
  });

  it("explains a node that has never sent a heartbeat report", async () => {
    serve({ report: null });
    await renderRecords();
    const report = card("Last report");
    expect(report).toHaveTextContent(
      "This node has not sent a heartbeat report yet. Nodes on the v1 node software send one with every heartbeat",
    );
    expect(within(report).queryByRole("table")).toBeNull();
    expect(within(report).queryByRole("alert")).toBeNull();
  });
});

describe("NodeOwnerRecords: configuration history", () => {
  it("lists versions newest first, whatever order they arrive in, and marks only the one in force", async () => {
    serve({ versions: [V1, V3, V2] });
    await renderRecords();
    const items = versionItems();
    expect(items.map((li) => within(li).getByRole("heading").textContent)).toEqual([
      "Version 3",
      "Version 2",
      "Version 1",
    ]);
    expect(within(items[0]).getByText("In force")).toHaveClass("badge", "online");
    expect(within(items[1]).queryByText("In force")).toBeNull();
    expect(within(items[2]).queryByText("In force")).toBeNull();
    expect(field(items[0], "Until")).toHaveTextContent(/^UntilNow$/);
    expect(field(items[1], "Until")).toHaveTextContent("1d ago");
    expect(field(items[1], "In force from")).toHaveTextContent("10d ago");
    expect(field(items[1], "In force from").querySelector("time")).toHaveAttribute(
      "datetime",
      new Date(V2.created_at).toISOString(),
    );
    expect(within(card("Configuration history")).getByText("3 versions")).toBeInTheDocument();
  });

  it("marks each value that changed from the version before it, in style and in words", async () => {
    await renderRecords();
    const [v3, v2] = versionItems();

    // A change below what five places show is still a change.
    expect(field(v3, "RX position")).toHaveClass("is-changed");
    expect(field(v3, "RX position")).toHaveTextContent("52.37030, 4.89517 (changed)");
    expect(within(field(v3, "RX position")).getByText("(changed)")).toHaveClass("node-records-sr");
    expect(field(v3, "Carrier frequency")).not.toHaveClass("is-changed");
    expect(v3).toHaveTextContent("Changed from version 2: RX position.");

    expect(field(v2, "Carrier frequency")).toHaveClass("is-changed");
    expect(field(v2, "Sample rate")).toHaveClass("is-changed");
    expect(field(v2, "RX position")).not.toHaveClass("is-changed");
    expect(v2).toHaveTextContent("Changed from version 1: Carrier frequency, Sample rate.");
    expect(within(v2).getAllByText("(changed)")).toHaveLength(2);
  });

  it("marks nothing on the oldest version, which has nothing before it", async () => {
    await renderRecords();
    const oldest = versionItems()[2];
    expect(oldest.querySelectorAll(".is-changed")).toHaveLength(0);
    expect(within(oldest).queryByText("(changed)")).toBeNull();
    expect(oldest).toHaveTextContent("The earliest configuration since this node joined your account.");
  });

  it("shows every value with its unit", async () => {
    await renderRecords();
    const v1 = versionItems()[2];
    const expected: [string, string][] = [
      ["Carrier frequency", "174.928 MHz"],
      ["Sample rate", "2.048 MHz"],
      ["RX position", "52.37022, 4.89517"],
      ["RX altitude", "30 ft"],
      ["TX position", "52.10000, 5.20000"],
      ["TX altitude", "1,200 ft"],
      ["TX callsign", "5A"],
      ["Beam width", "—"],
      ["Beam azimuth", "90°"],
      ["Max range", "60 km"],
      ["CPI", "0.5 s"],
      ["Delay tolerance", "2 µs"],
      ["Doppler tolerance", "5 Hz"],
    ];
    for (const [label, value] of expected) {
      expect(field(v1, label).querySelector("dd")).toHaveTextContent(new RegExp(`^${value}$`));
    }
  });

  it("says the oldest loaded is not the first when the history fills the limit", async () => {
    const many = Array.from({ length: 50 }, (_, i) => ({
      ...V1,
      version: 51 - i,
      superseded_at: i === 0 ? null : V1.superseded_at,
      max_range_km: 60 + (i % 2),
    }));
    serve({ versions: many });
    await renderRecords();
    const history = card("Configuration history");
    expect(within(history).getByText("Newest 50 versions")).toBeInTheDocument();
    fireEvent.click(within(history).getByRole("button", { name: "Show 45 older versions" }));
    const items = versionItems();
    expect(items).toHaveLength(50);
    expect(items[49]).toHaveTextContent("The oldest version loaded. Earlier versions are not shown.");
  });

  it("keeps a long history behind a button, which opens and closes it", async () => {
    const seven = Array.from({ length: 7 }, (_, i) => ({ ...V1, version: 7 - i, cpi_s: 0.5 + i / 10 }));
    serve({ versions: seven });
    await renderRecords();
    expect(versionItems()).toHaveLength(5);
    const more = within(card("Configuration history")).getByRole("button", { name: "Show 2 older versions" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(more);
    expect(versionItems()).toHaveLength(7);
    // The fifth, drawn before and after, is compared with the sixth either way.
    expect(field(versionItems()[4], "CPI")).toHaveClass("is-changed");
    const fewer = within(card("Configuration history")).getByRole("button", {
      name: "Show the newest versions only",
    });
    expect(fewer).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(fewer);
    expect(versionItems()).toHaveLength(5);
  });

  it("starts where the caller's account does, without calling the versions before it missing", async () => {
    // The server sends what the node has run under since it joined the
    // caller's account, which need not start at version 1.
    serve({ versions: [V3, { ...V2, superseded_at: V3.created_at }] });
    await renderRecords();
    const items = versionItems();
    expect(items).toHaveLength(2);
    expect(items[1]).toHaveTextContent("The earliest configuration since this node joined your account.");
    expect(within(card("Configuration history")).queryByText(/Earlier versions are not shown/)).toBeNull();
  });

  it("says when no configuration is recorded", async () => {
    serve({ versions: [] });
    await renderRecords();
    expect(
      within(card("Configuration history")).getByText("No configuration recorded since this node joined your account."),
    ).toBeInTheDocument();
  });
});

describe("NodeOwnerRecords: history", () => {
  const items = () => within(card("History")).getAllByRole("listitem");

  it("lists events newest first in plain words, each with when it happened", async () => {
    await renderRecords();
    expect(items().map((li) => li.querySelector("p")?.textContent)).toEqual([
      "Label changed by you",
      "Location made private by an earlier owner",
      "Graduated from probation by an administrator",
      "Registered automatically",
    ]);
    const first = items()[0];
    expect(first).toHaveTextContent("1m ago");
    expect(first.querySelector("time")).toHaveAttribute("datetime", new Date(EVENTS[0].at).toISOString());
    expect(first.querySelector("time")).toHaveTextContent(exact(EVENTS[0].at));
    expect(within(card("History")).getByText("4 entries")).toBeInTheDocument();
  });

  it.each([
    ["registered", "Registered"],
    ["removed", "Removed"],
    ["graduated", "Graduated from probation"],
    ["returned_to_probation", "Returned to probation"],
    ["renamed", "Label changed"],
    ["made_private", "Location made private"],
    ["made_public", "Location made public"],
    // A kind the console has no words for yet, under its own name.
    ["config_pushed_by_ops", "Config pushed by ops"],
    // And one that an object lookup would answer with Object's own.
    ["constructor", "Constructor"],
  ])("words the kind %s as %s", async (kind, words) => {
    serve({ events: [{ kind, at: ago(60), actor: "system", epoch: null }] });
    await renderRecords();
    expect(items()[0].querySelector("strong")).toHaveTextContent(new RegExp(`^${words}$`));
  });

  it.each([
    ["you", "Removed by you"],
    ["owner", "Removed by an earlier owner"],
    ["admin", "Removed by an administrator"],
    ["system", "Removed automatically"],
  ] as const)("words the actor %s", async (actor, sentence) => {
    serve({ events: [{ kind: "removed", at: ago(60), actor, epoch: null }] });
    await renderRecords();
    expect(items()[0].querySelector("p")).toHaveTextContent(new RegExp(`^${sentence}$`));
  });

  it("says when nothing is recorded", async () => {
    serve({ events: [] });
    await renderRecords();
    expect(within(card("History")).getByText("Nothing recorded since this node joined your account.")).toBeInTheDocument();
  });
});

describe("NodeOwnerRecords: fetching", () => {
  it("shows a loading line in each card until its own answer lands", async () => {
    let answer!: (report: NodeReport | null) => void;
    vi.mocked(api.myNodeReport).mockReturnValue(new Promise((resolve) => (answer = resolve)));
    render(<NodeOwnerRecords nodeId={NODE} onGone={onGone} />);
    expect(within(card("Last report")).getByText("Loading…")).toBeInTheDocument();
    expect(card("Last report")).toHaveAttribute("aria-busy", "true");
    expect(await within(card("History")).findByText(/Label changed/)).toBeInTheDocument();
    expect(within(card("Last report")).getByText("Loading…")).toBeInTheDocument();
    answer(REPORT);
    await settled();
    expect(card("Last report")).toHaveAttribute("aria-busy", "false");
  });

  it.each([
    ["myNodeReport", "Last report", "the last report"],
    ["myNodeConfigHistory", "Configuration history", "the configuration history"],
    ["myNodeEvents", "History", "this node's history"],
  ] as const)("a failing %s shows its own error while the other two cards render", async (method, name, what) => {
    vi.mocked(api[method]).mockRejectedValue(new Error("Service unavailable"));
    await renderRecords();
    expect(within(card(name)).getByRole("alert")).toHaveTextContent(`Could not load ${what}: Service unavailable`);
    expect(screen.getAllByRole("alert")).toHaveLength(1);
    if (name !== "Last report") expect(reading(card("Last report"), "Uptime")).toHaveTextContent("3d 4h");
    if (name !== "Configuration history") expect(versionItems()).toHaveLength(3);
    if (name !== "History") expect(within(card("History")).getAllByRole("listitem")).toHaveLength(4);
  });

  it("retries only the card that failed", async () => {
    vi.mocked(api.myNodeEvents).mockRejectedValueOnce(new Error("Service unavailable"));
    await renderRecords();
    fireEvent.click(within(card("History")).getByRole("button", { name: "Retry" }));
    expect(await within(card("History")).findByText(/Label changed/)).toBeInTheDocument();
    expect(within(card("History")).queryByRole("alert")).toBeNull();
    expect(api.myNodeEvents).toHaveBeenCalledTimes(2);
    expect(api.myNodeReport).toHaveBeenCalledTimes(1);
    expect(api.myNodeConfigHistory).toHaveBeenCalledTimes(1);
  });

  it("fetches all three again when refreshKey changes, and only then", async () => {
    const view = await renderRecords({ refreshKey: 0 });
    for (const method of [api.myNodeReport, api.myNodeConfigHistory, api.myNodeEvents]) {
      expect(method).toHaveBeenCalledTimes(1);
    }

    // The page bumps the key after the owner relabels the node.
    serve({ events: [{ kind: "renamed", at: ago(0), actor: "you", epoch: null }, ...EVENTS] });
    view.rerender(<NodeOwnerRecords nodeId={NODE} refreshKey={1} onGone={onGone} />);
    expect(await within(card("History")).findByText("5 entries")).toBeInTheDocument();
    await settled();
    for (const method of [api.myNodeReport, api.myNodeConfigHistory, api.myNodeEvents]) {
      expect(method).toHaveBeenCalledTimes(2);
    }

    view.rerender(<NodeOwnerRecords nodeId={NODE} refreshKey={1} onGone={onGone} />);
    await settled();
    for (const method of [api.myNodeReport, api.myNodeConfigHistory, api.myNodeEvents]) {
      expect(method).toHaveBeenCalledTimes(2);
    }
  });

  it("keeps what it had under a warning when a refresh fails", async () => {
    const view = await renderRecords({ refreshKey: 0 });
    vi.mocked(api.myNodeEvents).mockRejectedValue(new Error("Service unavailable"));
    view.rerender(<NodeOwnerRecords nodeId={NODE} refreshKey={1} onGone={onGone} />);
    const notice = await within(card("History")).findByRole("alert");
    expect(notice).toHaveTextContent("Could not refresh this node's history: Service unavailable");
    expect(within(card("History")).getAllByRole("listitem")).toHaveLength(4);
  });

  it.each([
    ["Last report", () => api.myNodeReport],
    ["Configuration history", () => api.myNodeConfigHistory],
    ["History", () => api.myNodeEvents],
  ])("hands the node to the page when the %s route answers 404, with no error of its own", async (name, route) => {
    vi.mocked(route()).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    await renderRecords();
    expect(onGone).toHaveBeenCalledTimes(1);
    expect(within(card(name)).queryByRole("alert")).toBeNull();
    expect(within(card(name)).queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("hands the node to the page when a refresh answers 404, whatever the card had", async () => {
    const view = await renderRecords({ refreshKey: 0 });
    expect(onGone).not.toHaveBeenCalled();
    vi.mocked(api.myNodeEvents).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    view.rerender(<NodeOwnerRecords nodeId={NODE} refreshKey={1} onGone={onGone} />);
    await waitFor(() => expect(onGone).toHaveBeenCalledTimes(1));
    expect(within(card("History")).queryByRole("alert")).toBeNull();
  });

  it.each([
    ["a 403", new HttpError(403, "Forbidden", null)],
    ["a 500", new HttpError(500, "Internal Server Error", null)],
    ["a network failure", new Error("Failed to fetch")],
  ])("keeps %s to the card, with Retry, and does not call the page", async (_, failure) => {
    vi.mocked(api.myNodeConfigHistory).mockRejectedValueOnce(failure);
    await renderRecords();
    const notice = within(card("Configuration history")).getByRole("alert");
    expect(notice).toHaveTextContent("Could not load the configuration history");
    expect(within(notice).getByRole("button", { name: "Retry" })).toBeInTheDocument();
    expect(onGone).not.toHaveBeenCalled();
  });

  it("asks every route for the node it was given", async () => {
    await renderRecords();
    expect(vi.mocked(api.myNodeReport).mock.calls).toEqual([[NODE]]);
    expect(vi.mocked(api.myNodeConfigHistory).mock.calls).toEqual([[NODE, 50]]);
    expect(vi.mocked(api.myNodeEvents).mock.calls).toEqual([[NODE, 100]]);
  });

  it("drops the last node's records at once when the node changes", async () => {
    const view = await renderRecords();
    const never = new Promise<never>(() => {});
    vi.mocked(api.myNodeReport).mockReturnValue(never);
    vi.mocked(api.myNodeConfigHistory).mockReturnValue(never);
    vi.mocked(api.myNodeEvents).mockReturnValue(never);
    view.rerender(<NodeOwnerRecords nodeId="ret9f8e7d6c" onGone={onGone} />);
    for (const name of ["Last report", "Configuration history", "History"]) {
      expect(within(card(name)).getByText("Loading…")).toBeInTheDocument();
    }
    expect(screen.queryByText(/Label changed/)).toBeNull();
    expect(vi.mocked(api.myNodeReport).mock.lastCall).toEqual(["ret9f8e7d6c"]);
    expect(vi.mocked(api.myNodeConfigHistory).mock.lastCall).toEqual(["ret9f8e7d6c", 50]);
    expect(vi.mocked(api.myNodeEvents).mock.lastCall).toEqual(["ret9f8e7d6c", 100]);
  });
});
