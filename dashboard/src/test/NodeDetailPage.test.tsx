import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { HttpError } from "@retina/shared";
import NodeDetailPage from "../pages/user/NodeDetailPage";
import { api } from "../api/client";
import { POSITION_STATUS_EXPLANATION } from "../components/PositionStatusBadge";
import stylesheet from "../pages/user/nodeDetail/nodeDetail.css?raw";
import type { OwnedNode, OwnerEvent, OwnerSummary } from "../types";
import { ownedNode } from "./fixtures/ownedNode";
import { declarations } from "./paletteTokens";

vi.mock("../api/client", () => ({
  api: {
    nodeAnalytics: vi.fn(),
    nodes: vi.fn(),
    myNodes: vi.fn(),
    myNode: vi.fn(),
    myNodeAnalytics: vi.fn(),
    updateMyNode: vi.fn(),
    releaseNode: vi.fn(),
    myNodeReport: vi.fn(),
    myNodeConfigHistory: vi.fn(),
    myNodeEvents: vi.fn(),
    adminNodeRefs: vi.fn(),
    adminNodeClaim: vi.fn(),
  },
}));
vi.mock("recharts", async (importOriginal) => ({
  ...await importOriginal<typeof import("recharts")>(),
  ResponsiveContainer: () => null,
}));

/** Every read the page has started settles, and whatever it starts after them. */
const settle = () => act(() => new Promise<void>((done) => setTimeout(done, 0)));

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: Error) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const REF = "nde0a1b2c3d4e5f";
const ID = "ret0a1b2c3d";

/** The owner's own summary: nothing moved, so these are the true figures. */
const SUMMARY: OwnerSummary = {
  node_id: ID,
  node_ref: REF,
  trust: { trust_score: 0.9, adsb_matches: 12 },
  detection_area: { estimated_range_km: 41.5, beam_width_deg: 30, validated_positions: 7 },
  metrics: { total_frames: 1234, total_detections: 56, total_tracks: 7, avg_snr: 12.5, availability_7d: 0.97 },
  reputation: { reputation: 0.8 },
  coverage_map: null,
  empirical_coverage: null,
  node_bias: null,
};

/** What anyone is sent for the same node: other figures, so a test can tell
 *  which route the page drew from. */
const PUBLIC_SUMMARY = { node_ref: REF, metrics: { total_frames: 9, total_detections: 3, total_tracks: 1, avg_snr: 4 } };

/** An owned node with its whole configuration, as GET /api/auth/me/nodes/{id}
 *  sends it. */
function mine(overrides: Partial<OwnedNode> = {}): OwnedNode {
  return ownedNode({
    node_id: ID,
    node_ref: REF,
    name: REF,
    frequency: 174_928_000,
    sample_rate: 2_048_000,
    location: { rx_lat: 52.370216, rx_lon: 4.895168, rx_alt_ft: 30, tx_lat: 52.1, tx_lon: 5.2, tx_alt_ft: 1200 },
    rf: {
      tx_callsign: "5A",
      beam_width_deg: 45,
      beam_azimuth_deg: 90,
      max_range_km: 60,
      cpi_s: 0.5,
      delay_tolerance_us: 2,
      doppler_tolerance_hz: 5,
    },
    claimed_with: "ada@example.com",
    ...overrides,
  });
}

/** The caller owns `node`, and the owner routes answer for it. */
function own(node: OwnedNode, summary: OwnerSummary | null = SUMMARY) {
  vi.mocked(api.myNodes).mockResolvedValue([node]);
  vi.mocked(api.myNode).mockResolvedValue(node);
  vi.mocked(api.myNodeAnalytics).mockResolvedValue(summary);
}

function Navigation() {
  const navigate = useNavigate();
  return <button onClick={() => navigate("/nodes/b")}>Go to B</button>;
}

function renderPage(path = `/nodes/${REF}`) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Navigation />
      <Routes>
        <Route path="/nodes/:nodeId" element={<NodeDetailPage />} />
        <Route path="/overview" element={<div>My nodes page</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

const heading = () => screen.findByRole("heading", { level: 1 });
const card = (title: string) => screen.getByRole("heading", { name: title }).closest(".card") as HTMLElement;
const reading = (label: string) => within(card("RF Configuration")).getByText(label).closest("tr")!.children[1];

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(api.nodes).mockResolvedValue({ nodes: {} });
  vi.mocked(api.myNodes).mockResolvedValue([]);
  vi.mocked(api.nodeAnalytics).mockResolvedValue(PUBLIC_SUMMARY);
  vi.mocked(api.myNodeReport).mockResolvedValue(null);
  vi.mocked(api.myNodeConfigHistory).mockResolvedValue({ versions: [] });
  vi.mocked(api.myNodeEvents).mockResolvedValue({ events: [] });
  // usePolling logs every failure it keeps; the tests below fail on purpose.
  vi.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("an owned node's page", () => {
  it.each([
    ["private", true],
    ["public", false],
  ])("reads a %s node from the owner routes alone", async (_, isPrivate) => {
    own(mine({ location_private: isPrivate }));
    renderPage();

    await screen.findByRole("heading", { name: "RF Configuration" });
    expect(api.myNode).toHaveBeenCalledWith(ID);
    expect(api.myNodeAnalytics).toHaveBeenCalledWith(ID);
    expect(api.nodes).not.toHaveBeenCalled();
    expect(api.nodeAnalytics).not.toHaveBeenCalled();
    // The unfuzzed figures, from the owner's own summary.
    expect(screen.getByText("Total Detections").nextElementSibling).toHaveTextContent(/^56$/);
  });

  it("shows the whole RF configuration with the true positions", async () => {
    own(mine({ location_private: true }));
    renderPage();
    await screen.findByRole("heading", { name: "RF Configuration" });

    expect(reading("Center Frequency")).toHaveTextContent("174.928 MHz");
    expect(reading("Sample Rate")).toHaveTextContent("2.05 MSps");
    expect(reading("RX Location")).toHaveTextContent("52.37022, 4.89517 / 30 ft");
    expect(reading("TX Location")).toHaveTextContent("52.10000, 5.20000 / 1,200 ft");
    expect(reading("TX Callsign")).toHaveTextContent("5A");
    expect(reading("Beam Width")).toHaveTextContent("45°");
    expect(reading("Beam Azimuth")).toHaveTextContent("90°");
    expect(reading("Max Range")).toHaveTextContent("60 km");
    expect(reading("CPI")).toHaveTextContent("0.5 s");
    expect(reading("Delay Tolerance")).toHaveTextContent("2 µs");
    expect(reading("Doppler Tolerance")).toHaveTextContent("5 Hz");
  });

  it("dashes what the node has not declared", async () => {
    own(mine({ location: { rx_lat: null, rx_lon: null, rx_alt_ft: null, tx_lat: 52.1, tx_lon: null, tx_alt_ft: null } }));
    renderPage();
    await screen.findByRole("heading", { name: "RF Configuration" });

    expect(reading("RX Location")).toHaveTextContent(/^—$/);
    // Half a coordinate pair is no position.
    expect(reading("TX Location")).toHaveTextContent(/^—$/);
  });

  it("titles the page by the owner's label, with the ref beside it", async () => {
    own(mine({ label: "Roof", name: "Roof" }));
    renderPage();

    const title = await heading();
    expect(title).toHaveTextContent(/^Roof$/);
    expect(screen.getByText(REF)).toHaveClass("mono");
  });

  it("links the owner's title to the node's own site, which only the owner holds the id for", async () => {
    own(mine());
    renderPage();

    const title = await heading();
    expect(within(title).getByRole("link")).toHaveAttribute("href", `https://${ID}.retnode.com`);
  });

  it("marks a private node as private in its heading", async () => {
    own(mine({ location_private: true }));
    renderPage();

    const title = await heading();
    expect(within(title).getByText("Private")).toBeInTheDocument();
  });

  it("marks nothing on a public node", async () => {
    own(mine({ location_private: false }));
    renderPage();

    const title = await heading();
    await screen.findByRole("heading", { name: "RF Configuration" });
    expect(within(title).queryByText("Private")).not.toBeInTheDocument();
  });

  it("says so when the server has measured nothing yet, and shows the rest", async () => {
    own(mine(), null);
    renderPage();

    expect(await screen.findByText(/Nothing measured for this node yet/)).toBeInTheDocument();
    expect(screen.queryByText("Total Frames")).not.toBeInTheDocument();
    expect(screen.queryByText("Node not found")).not.toBeInTheDocument();
    expect(reading("TX Callsign")).toHaveTextContent("5A");
  });

  it("keeps the rest of the page when its metrics fail, and retries them", async () => {
    own(mine());
    vi.mocked(api.myNodeAnalytics).mockRejectedValueOnce(new HttpError(503, "Service Unavailable", null));
    renderPage();

    const notice = (await screen.findByText(/Could not load this node.s metrics/)).closest(".notice") as HTMLElement;
    expect(notice).toHaveTextContent("503 Service Unavailable");
    expect(screen.queryByText(/Nothing measured/)).not.toBeInTheDocument();
    expect(reading("TX Callsign")).toHaveTextContent("5A");

    fireEvent.click(within(notice).getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Total Frames")).toBeInTheDocument();
    expect(screen.queryByText(/Could not load this node.s metrics/)).not.toBeInTheDocument();
  });

  it("reads the public page for a node that left the account after the list was read", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([mine()]);
    vi.mocked(api.myNode).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    vi.mocked(api.myNodeAnalytics).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    renderPage();

    expect(await screen.findByText("Detailed metrics and trust analysis")).toBeInTheDocument();
    expect(api.nodeAnalytics).toHaveBeenCalledWith(REF);
    expect(screen.queryByRole("button", { name: "Edit label" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /release this node/i })).not.toBeInTheDocument();
  });
});

describe("a node the viewer does not own", () => {
  it("reads the public routes and offers no owner controls", async () => {
    vi.mocked(api.nodes).mockResolvedValue({ nodes: { [REF]: { frequency: 195e6, location: {} } } });
    renderPage();

    await screen.findByRole("heading", { name: "RF Configuration" });
    expect(api.nodeAnalytics).toHaveBeenCalledWith(REF);
    expect(api.nodes).toHaveBeenCalled();
    for (const ownerRoute of [api.myNode, api.myNodeAnalytics, api.myNodeReport, api.myNodeConfigHistory, api.myNodeEvents]) {
      expect(ownerRoute).not.toHaveBeenCalled();
    }
    expect(reading("Center Frequency")).toHaveTextContent("195.000 MHz");
    // The published figures, not anybody's own.
    expect(screen.getByText("Total Detections").nextElementSibling).toHaveTextContent(/^3$/);
    // None of the owner's view: no settings, no records, no ownership.
    expect(screen.queryByRole("heading", { name: "Label and privacy" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Last report" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Ownership" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit label" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /release this node/i })).not.toBeInTheDocument();
    expect(screen.queryByText("TX Callsign")).not.toBeInTheDocument();
    // Its ref alone, linked to nothing: only the owner holds the node's id.
    const title = await heading();
    expect(title).toHaveTextContent(REF);
    expect(within(title).queryByRole("link")).not.toBeInTheDocument();
  });

  it("says when it could not check whether the node is the viewer's, for a node the listing shows is public", async () => {
    vi.mocked(api.myNodes).mockRejectedValue(new HttpError(503, "Service Unavailable", null));
    vi.mocked(api.nodes).mockResolvedValue({ nodes: { [REF]: { frequency: 195e6, location: {} } } });
    renderPage();

    const notice = (await screen.findByText(/Could not check whether this node is yours/)).closest(".notice");
    expect(notice).toHaveTextContent("This page shows what anyone can see.");
    expect(screen.getByText("Total Detections")).toBeInTheDocument();
    expect(reading("Center Frequency")).toHaveTextContent("195.000 MHz");
    // The ref went to the public route only once the listing had shown it public.
    expect(vi.mocked(api.nodes).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(api.nodeAnalytics).mock.invocationCallOrder[0],
    );
  });

  it("gives the week, the day and how much of the week it rests on", async () => {
    vi.mocked(api.nodeAnalytics).mockResolvedValue({
      node_ref: REF,
      metrics: { availability_7d: 0.9731, availability_24h: 1, availability_measured_s: 42 * 3600 },
    });
    renderPage();
    const row = (label: string) => screen.getByText(label).closest("tr")!;
    await screen.findByText("Availability (7 d)");
    expect(within(row("Availability (7 d)")).getByText("97.3%")).toBeInTheDocument();
    expect(within(row("Availability (24 h)")).getByText("100.0%")).toBeInTheDocument();
    expect(within(row("Availability measured over")).getByText("42.0h")).toBeInTheDocument();
  });
});

describe("the position warning", () => {
  const warning = async () => {
    const alerts = await screen.findAllByRole("alert");
    return alerts.find((a) => a.textContent?.includes("Position not configured.")) ?? null;
  };

  it.each([
    ["missing_rx", "Add its receiver position in the node configuration."],
    ["missing_tx", "Add its illuminator position in the node configuration."],
    ["missing_both", "Add its receiver and illuminator positions in the node configuration."],
  ] as const)("is a Notice on a private node missing %s, from the owner's record", async (status, hint) => {
    own(mine({ location_private: true, position_status: status }));
    renderPage();

    const notice = await warning();
    expect(notice).toHaveClass("notice");
    expect(notice).not.toHaveClass("error");
    expect(notice).toHaveTextContent(`Position not configured. ${POSITION_STATUS_EXPLANATION} ${hint}`);
    expect(api.nodes).not.toHaveBeenCalled();
  });

  it("is absent for a positioned node", async () => {
    own(mine({ position_status: "positioned" }));
    renderPage();

    await screen.findByRole("heading", { name: "RF Configuration" });
    expect(screen.queryByText(/Position not configured/)).not.toBeInTheDocument();
  });

  it("warns on the public page from the published listing", async () => {
    vi.mocked(api.nodes).mockResolvedValue({ nodes: { [REF]: { position_status: "missing_tx", location: {} } } });
    renderPage();

    expect(await warning()).toHaveTextContent("Add its illuminator position in the node configuration.");
  });
});

describe("the heading on a phone", () => {
  const rules = stylesheet.replace(/\/\*[\s\S]*?\*\//g, "");

  it("wraps the row, then the title, then the name inside it", () => {
    expect(declarations(rules, ".node-detail-heading {")["flex-wrap"]).toBe("wrap");
    const title = declarations(rules, ".node-detail-heading .node-detail-title {");
    expect(title["flex-wrap"]).toBe("wrap");
    expect(title["min-width"]).toBe("0");
    const name = declarations(rules, ".node-detail-name {");
    expect(name["overflow-wrap"]).toBe("anywhere");
    expect(name["min-width"]).toBe("0");
  });

  it("puts the name in the classes that wrap, and Back beside the heading rather than in it", async () => {
    own(mine());
    renderPage();

    const title = await heading();
    expect(title).toHaveClass("node-detail-title");
    expect(title.parentElement).toHaveClass("node-detail-heading");
    expect(within(title).getByText(REF).closest(".node-detail-name")).not.toBeNull();
    expect(screen.getByRole("heading", { level: 1, name: REF })).toBe(title);
    expect(within(title.parentElement!).getByRole("button", { name: "Back" })).toBeInTheDocument();
    expect(within(title).queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("label and privacy", () => {
  it("renames the node in place, and the heading takes the label", async () => {
    own(mine());
    vi.mocked(api.updateMyNode).mockResolvedValue(mine({ label: "Roof", name: "Roof" }));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Edit label" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Label" }), { target: { value: "Roof" } });
    fireEvent.submit(screen.getByRole("textbox", { name: "Label" }).closest("form")!);

    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(/^Roof$/));
    expect(api.updateMyNode).toHaveBeenCalledWith(ID, { name: "Roof" });
    expect(screen.getByText("Label saved.")).toBeInTheDocument();
    // The ref the page no longer titles itself by is still on it.
    expect(screen.getByText(REF)).toHaveClass("mono");
    // Read once: the change is the server's answer, not a reload.
    expect(api.myNode).toHaveBeenCalledTimes(1);
  });

  it("makes the location private in place, and the heading marks it", async () => {
    own(mine({ location_private: false }));
    vi.mocked(api.updateMyNode).mockResolvedValue(mine({ location_private: true }));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Change location privacy" }));
    fireEvent.click(screen.getByRole("radio", { name: /^Private/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    const title = await heading();
    await waitFor(() => expect(within(title).getByText("Private")).toBeInTheDocument());
    expect(api.updateMyNode).toHaveBeenCalledWith(ID, { location_private: true });
    expect(within(card("Label and privacy")).getByText("Location is now private.")).toBeInTheDocument();
  });
});

describe("the node's records", () => {
  it("shows the last report, the configuration history and the history, for this node", async () => {
    own(mine());
    renderPage();

    for (const title of ["Last report", "Configuration history", "History"]) {
      expect(await screen.findByRole("heading", { name: title })).toBeInTheDocument();
    }
    expect(api.myNodeReport).toHaveBeenCalledWith(ID);
    expect(api.myNodeConfigHistory).toHaveBeenCalledWith(ID, 50);
    expect(api.myNodeEvents).toHaveBeenCalledWith(ID, 100);
  });

  it("fetches them again after an edit, so the entry it recorded shows", async () => {
    const renamed: OwnerEvent = { kind: "renamed", at: new Date().toISOString(), actor: "you", epoch: null };
    own(mine());
    vi.mocked(api.myNodeEvents).mockResolvedValueOnce({ events: [] }).mockResolvedValue({ events: [renamed] });
    vi.mocked(api.updateMyNode).mockResolvedValue(mine({ label: "Roof", name: "Roof" }));
    renderPage();
    expect(await screen.findByText("Nothing recorded since this node joined your account.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Edit label" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Label" }), { target: { value: "Roof" } });
    fireEvent.submit(screen.getByRole("textbox", { name: "Label" }).closest("form")!);

    const history = await screen.findByRole("heading", { name: "History" });
    const entry = await within(history.closest(".card") as HTMLElement).findByText("Label changed");
    expect(entry.closest("p")).toHaveTextContent("Label changed by you");
    expect(api.myNodeEvents).toHaveBeenCalledTimes(2);
    expect(api.myNodeReport).toHaveBeenCalledTimes(2);
    expect(api.myNodeConfigHistory).toHaveBeenCalledTimes(2);
  });
});

describe("ownership and releasing", () => {
  it("shows the address the node was claimed with", async () => {
    own(mine({ claimed_with: "ada@example.com" }));
    renderPage();

    expect(await screen.findByText(/ada@example.com/)).toBeInTheDocument();
  });

  it("says so plainly when a node was assigned rather than claimed", async () => {
    own(mine({ claimed_with: null }));
    renderPage();

    expect(await screen.findByText(/assigned to you rather than claimed/i)).toBeInTheDocument();
  });

  it("asks before releasing, naming what happens next", async () => {
    own(mine());
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /release this node/i }));

    expect(screen.getByText(/whoever claims it next/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Yes, release it" })).toHaveClass("btn-danger");
    expect(api.releaseNode).not.toHaveBeenCalled();
  });

  it("backs out on Keep it, releasing nothing", async () => {
    own(mine());
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /release this node/i }));

    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));

    expect(screen.getByRole("button", { name: /release this node/i })).toBeInTheDocument();
    expect(screen.queryByText(/whoever claims it next/i)).not.toBeInTheDocument();
    expect(api.releaseNode).not.toHaveBeenCalled();
  });

  it("releases by node_id on the confirmation, then says so where the controls were", async () => {
    own(mine());
    vi.mocked(api.myNodes).mockResolvedValueOnce([mine()]).mockResolvedValue([]);
    vi.mocked(api.releaseNode).mockResolvedValue({ ok: true });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /release this node/i }));

    fireEvent.click(screen.getByRole("button", { name: "Yes, release it" }));

    const status = await screen.findByText(/^Released\./);
    expect(api.releaseNode).toHaveBeenCalledWith(ID);
    expect(status).toHaveAttribute("role", "status");
    expect(status).toHaveTextContent("Released. This node is no longer on your account. Go to My Nodes");
    expect(within(status).getByRole("link", { name: "Go to My Nodes" })).toHaveAttribute("href", "/overview");
    // The card stays, with the note in place of its controls, and takes focus
    // from the button that went with them.
    expect(card("Ownership")).toContainElement(status);
    expect(status).toHaveFocus();
    expect(screen.queryByRole("button", { name: /release this node/i })).not.toBeInTheDocument();
    // The page reads the node again as anyone would, and the note, focus and
    // all, stays the same note through it.
    expect(await screen.findByText("Detailed metrics and trust analysis")).toBeInTheDocument();
    expect(screen.getByText(/^Released\./)).toBe(status);
    expect(status).toHaveFocus();
  });

  it("drops the owner's view at once, and reads the node again as anyone would", async () => {
    const after = deferred<OwnedNode[]>();
    own(mine({ label: "Roof", name: "Roof" }));
    vi.mocked(api.myNodes).mockResolvedValueOnce([mine({ label: "Roof", name: "Roof" })]).mockReturnValue(after.promise);
    vi.mocked(api.releaseNode).mockResolvedValue({ ok: true });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /release this node/i }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, release it" }));

    await screen.findByText(/^Released\./);
    // Before the public answer is in: nothing of the owner's is left up.
    expect(screen.queryByText("TX Callsign")).not.toBeInTheDocument();
    expect(screen.queryByText("Total Frames")).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Label and privacy" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Last report" })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).not.toHaveTextContent("Roof");

    await act(async () => after.resolve([]));

    expect(await screen.findByText("Detailed metrics and trust analysis")).toBeInTheDocument();
    expect(api.nodeAnalytics).toHaveBeenCalledWith(REF);
    expect(screen.getByText("Total Detections").nextElementSibling).toHaveTextContent(/^3$/);
    expect(screen.getByText(/^Released\./)).toHaveAttribute("role", "status");
    expect(screen.queryByRole("button", { name: "Edit label" })).not.toBeInTheDocument();
  });

  it("leaves only the note for a private node, which has no public page, and asks no public route for it", async () => {
    own(mine({ label: "Roof", name: "Roof", location_private: true }));
    vi.mocked(api.myNodes).mockResolvedValueOnce([mine({ location_private: true })]).mockResolvedValue([]);
    vi.mocked(api.nodeAnalytics).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    vi.mocked(api.releaseNode).mockResolvedValue({ ok: true });
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /release this node/i }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, release it" }));

    const status = await screen.findByText(/^Released\./);
    await settle();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(new RegExp(`^${REF}$`));
    expect(screen.queryByText("Node not found")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText("RF Configuration")).not.toBeInTheDocument();
    expect(screen.queryByText("TX Callsign")).not.toBeInTheDocument();
    expect(screen.getByText(/^Released\./)).toBe(status);
    expect(status).toHaveFocus();
    expect(api.nodeAnalytics).not.toHaveBeenCalled();
    expect(api.nodes).not.toHaveBeenCalled();
    // Nothing read again at all: the page knows the outcome.
    expect(api.myNodes).toHaveBeenCalledTimes(1);
  });

  it("keeps the node and its controls when the release fails, and says why", async () => {
    own(mine());
    vi.mocked(api.releaseNode).mockRejectedValue(new HttpError(502, "Bad Gateway", null));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /release this node/i }));

    fireEvent.click(screen.getByRole("button", { name: "Yes, release it" }));

    const notice = (await screen.findByText("502 Bad Gateway")).closest(".notice");
    expect(notice).toHaveClass("error");
    expect(screen.getByText(/ada@example.com/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Yes, release it" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Edit label" })).toBeInTheDocument();
    expect(screen.queryByText(/no longer on your account/)).not.toBeInTheDocument();
    // Keep it clears the failure along with the question.
    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(screen.queryByText("502 Bad Gateway")).not.toBeInTheDocument();
  });

  it("says the node has left the account when the server no longer has it there", async () => {
    own(mine());
    vi.mocked(api.myNodes).mockResolvedValueOnce([mine()]).mockResolvedValue([]);
    vi.mocked(api.releaseNode).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: /release this node/i }));

    fireEvent.click(screen.getByRole("button", { name: "Yes, release it" }));

    const status = await screen.findByText(/^This node is no longer on your account\./);
    expect(status).toHaveAttribute("role", "status");
    expect(screen.queryByText(/Released\./)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit label" })).not.toBeInTheDocument();
  });

  it("sends the owner of a polled radar to its radar page rather than offering a release", async () => {
    own(
      mine({
        node_id: "bla0a1b2c3d",
        claimed_with: null,
        location_private: true,
        polled: { address: "radar.example.com:3000", unprotected: false, liveness: "streaming", trust_state: "probation" },
      }),
    );
    renderPage();

    expect(await screen.findByRole("link", { name: /its radar page/i })).toHaveAttribute("href", `/radars/${REF}`);
    expect(screen.queryByRole("button", { name: /release this node/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/assigned to you rather than claimed/i)).not.toBeInTheDocument();
  });
});

/** Every route the page reads with no session behind it: what anyone is sent. */
const publicRoutes = () => [api.nodeAnalytics, api.nodes];
/** Each owner route, all under /api/auth/me. */
const ownerRoutes = () => [
  api.myNodes,
  api.myNode,
  api.myNodeAnalytics,
  api.updateMyNode,
  api.releaseNode,
  api.myNodeReport,
  api.myNodeConfigHistory,
  api.myNodeEvents,
];

/** The note in the Ownership card, once the node has left the account. */
const goneNote = () => screen.findByText(/^This node is no longer on your account\./);

describe("a private node's ref and the public routes", () => {
  it.each([
    ["a 429 from the session zone", new HttpError(429, "Too Many Requests", null), "429 Too Many Requests"],
    ["a 503", new HttpError(503, "Service Unavailable", null), "503 Service Unavailable"],
    ["a timeout", new DOMException("No response within 10000 ms", "TimeoutError"), "No response within 10000 ms"],
  ])("keeps the ref to itself when the owner's list fails with %s, and Retry asks the list again", async (_, failure, message) => {
    own(mine({ location_private: true }));
    vi.mocked(api.myNodes).mockRejectedValueOnce(failure);
    renderPage();

    // jsdom's DOMException is no Error, so it reads with its name in front.
    const failed = new RegExp(`^Could not load this node: (TimeoutError: )?${message}$`);
    const notice = (await screen.findByText(failed)).closest(".notice") as HTMLElement;
    expect(notice).toHaveClass("error");
    expect(api.nodeAnalytics).not.toHaveBeenCalled();
    // The listing names no node by being asked, so it can say whether the ref
    // is public without hearing it.
    expect(api.nodes).toHaveBeenCalledWith();
    expect(screen.queryByText("Node not found")).not.toBeInTheDocument();

    fireEvent.click(within(notice).getByRole("button", { name: "Retry" }));
    expect(await screen.findByRole("heading", { name: "RF Configuration" })).toBeInTheDocument();
    expect(within(await heading()).getByText("Private")).toBeInTheDocument();
    expect(api.nodeAnalytics).not.toHaveBeenCalled();
  });

  it("says the list failed when the public listing fails too", async () => {
    vi.mocked(api.myNodes).mockRejectedValue(new HttpError(429, "Too Many Requests", null));
    vi.mocked(api.nodes).mockRejectedValue(new HttpError(502, "Bad Gateway", null));
    renderPage();

    expect(await screen.findByText("Could not load this node: 429 Too Many Requests")).toBeInTheDocument();
    expect(api.nodeAnalytics).not.toHaveBeenCalled();
  });

  it("says the list failed for a node listed as public that has no figures published", async () => {
    vi.mocked(api.myNodes).mockRejectedValue(new HttpError(503, "Service Unavailable", null));
    vi.mocked(api.nodes).mockResolvedValue({ nodes: { [REF]: { location: {} } } });
    vi.mocked(api.nodeAnalytics).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    renderPage();

    expect(await screen.findByText("Could not load this node: 503 Service Unavailable")).toBeInTheDocument();
    expect(screen.queryByText("Node not found")).not.toBeInTheDocument();
  });

  it("finds nothing in the listing for a ref that names one of Object's own keys", async () => {
    vi.mocked(api.myNodes).mockRejectedValue(new HttpError(503, "Service Unavailable", null));
    renderPage("/nodes/constructor");

    expect(await screen.findByText("Could not load this node: 503 Service Unavailable")).toBeInTheDocument();
    expect(api.nodeAnalytics).not.toHaveBeenCalled();
  });

  it("reads a node made private on this page as private: no public read after it is released", async () => {
    own(mine({ location_private: false }));
    vi.mocked(api.myNodes).mockResolvedValueOnce([mine({ location_private: false })]).mockResolvedValue([]);
    vi.mocked(api.updateMyNode).mockResolvedValue(mine({ location_private: true }));
    vi.mocked(api.releaseNode).mockResolvedValue({ ok: true });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Change location privacy" }));
    fireEvent.click(screen.getByRole("radio", { name: /^Private/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Location is now private.");
    fireEvent.click(screen.getByRole("button", { name: /release this node/i }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, release it" }));

    await screen.findByText(/^Released\./);
    await settle();
    for (const route of publicRoutes()) expect(route).not.toHaveBeenCalled();
  });

  it("reads a node made public on this page again as anyone would once it is released", async () => {
    own(mine({ location_private: true }));
    vi.mocked(api.myNodes).mockResolvedValueOnce([mine({ location_private: true })]).mockResolvedValue([]);
    vi.mocked(api.updateMyNode).mockResolvedValue(mine({ location_private: false }));
    vi.mocked(api.releaseNode).mockResolvedValue({ ok: true });
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Change location privacy" }));
    fireEvent.click(screen.getByRole("radio", { name: /^Public/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await screen.findByText("Location is now public.");
    fireEvent.click(screen.getByRole("button", { name: /release this node/i }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, release it" }));

    expect(await screen.findByText("Detailed metrics and trust analysis")).toBeInTheDocument();
    expect(api.nodeAnalytics).toHaveBeenCalledWith(REF);
    expect(screen.getByText(/^Released\./)).toHaveAttribute("role", "status");
  });

  it("says a private node has left the account when a retry finds it gone, and asks no public route", async () => {
    own(mine({ label: "Roof", name: "Roof", location_private: true }));
    vi.mocked(api.myNodeAnalytics).mockRejectedValueOnce(new HttpError(503, "Service Unavailable", null));
    renderPage();
    const notice = (await screen.findByText(/Could not load this node.s metrics/)).closest(".notice") as HTMLElement;

    vi.mocked(api.myNodes).mockResolvedValue([]);
    fireEvent.click(within(notice).getByRole("button", { name: "Retry" }));

    const note = await goneNote();
    expect(note).toHaveAttribute("role", "status");
    expect(note).toHaveFocus();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(new RegExp(`^${REF}$`));
    expect(screen.queryByRole("heading", { name: "Label and privacy" })).not.toBeInTheDocument();
    expect(screen.queryByText("TX Callsign")).not.toBeInTheDocument();
    for (const route of publicRoutes()) expect(route).not.toHaveBeenCalled();
  });

  it("keeps a private node's view when a retry's list fails, whatever a stale listing says", async () => {
    own(mine({ location_private: true }));
    vi.mocked(api.myNodeAnalytics).mockRejectedValueOnce(new HttpError(503, "Service Unavailable", null));
    // The listing is rebuilt on a timer, so a node made private a moment ago
    // can still be in it.
    vi.mocked(api.nodes).mockResolvedValue({ nodes: { [REF]: { location: {} } } });
    renderPage();
    const notice = (await screen.findByText(/Could not load this node.s metrics/)).closest(".notice") as HTMLElement;

    vi.mocked(api.myNodes).mockRejectedValue(new HttpError(429, "Too Many Requests", null));
    fireEvent.click(within(notice).getByRole("button", { name: "Retry" }));

    expect(await screen.findByText(/Could not refresh this node: 429 Too Many Requests/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "RF Configuration" })).toBeInTheDocument();
    for (const route of publicRoutes()) expect(route).not.toHaveBeenCalled();
  });

  it("says a private node has left the account when the owner routes no longer answer for it", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([mine({ location_private: true })]);
    vi.mocked(api.myNode).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    vi.mocked(api.myNodeAnalytics).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    renderPage();

    expect(await goneNote()).toHaveTextContent("This node is no longer on your account. Go to My Nodes");
    expect(screen.queryByText(/^Released\./)).not.toBeInTheDocument();
    expect(screen.queryByText("Node not found")).not.toBeInTheDocument();
    for (const route of publicRoutes()) expect(route).not.toHaveBeenCalled();
  });
});

describe("a change that finds the node gone from the account", () => {
  it("takes the owner's view down and says so, as a release answered 404 does", async () => {
    own(mine({ label: "Roof", name: "Roof", location_private: true }));
    // Gone from the account, so its list no longer names it either.
    vi.mocked(api.myNodes).mockResolvedValueOnce([mine({ location_private: true })]).mockResolvedValue([]);
    vi.mocked(api.updateMyNode).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Edit label" }));
    fireEvent.change(screen.getByRole("textbox", { name: "Label" }), { target: { value: "Mast" } });
    fireEvent.submit(screen.getByRole("textbox", { name: "Label" }).closest("form")!);

    const note = await goneNote();
    expect(note).toHaveAttribute("role", "status");
    expect(note).toHaveFocus();
    expect(within(card("Ownership")).getByRole("link", { name: "Go to My Nodes" })).toHaveAttribute("href", "/overview");
    expect(screen.queryByText(/^Released\./)).not.toBeInTheDocument();
    // Nothing of the owner's is left up, the form included.
    expect(screen.queryByRole("textbox", { name: "Label" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Label and privacy" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Last report" })).not.toBeInTheDocument();
    expect(screen.queryByText("TX Callsign")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /release this node/i })).not.toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(new RegExp(`^${REF}$`));
    await settle();
    for (const route of publicRoutes()) expect(route).not.toHaveBeenCalled();
  });

  it("reads a public node again as anyone would, under the note", async () => {
    own(mine({ location_private: false }));
    vi.mocked(api.myNodes).mockResolvedValueOnce([mine({ location_private: false })]).mockResolvedValue([]);
    vi.mocked(api.updateMyNode).mockRejectedValue(new HttpError(404, "Not Found", { detail: "Node not found" }));
    renderPage();

    fireEvent.click(await screen.findByRole("button", { name: "Change location privacy" }));
    fireEvent.click(screen.getByRole("radio", { name: /^Private/ }));
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    const note = await goneNote();
    expect(await screen.findByText("Detailed metrics and trust analysis")).toBeInTheDocument();
    expect(api.nodeAnalytics).toHaveBeenCalledWith(REF);
    expect(screen.getByText(/^This node is no longer on your account\./)).toBe(note);
    expect(screen.queryByRole("button", { name: "Change location privacy" })).not.toBeInTheDocument();
  });
});

describe("a records card that finds the node gone from the account", () => {
  const records = [
    ["report", () => api.myNodeReport],
    ["configuration history", () => api.myNodeConfigHistory],
    ["events", () => api.myNodeEvents],
  ] as const;
  const notFound = () => new HttpError(404, "Not Found", { detail: "Node not found" });

  it.each(records)("takes a private node's owner view down for a 404 from the %s, and asks no public route", async (_, route) => {
    own(mine({ location_private: true }));
    vi.mocked(route()).mockRejectedValue(notFound());
    renderPage();

    const note = await goneNote();
    expect(note).toHaveAttribute("role", "status");
    expect(note).toHaveFocus();
    expect(note).toHaveTextContent("This node is no longer on your account. Go to My Nodes");
    expect(screen.queryByRole("heading", { name: "Last report" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Configuration history" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "History" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Label and privacy" })).not.toBeInTheDocument();
    expect(screen.queryByText("TX Callsign")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /release this node/i })).not.toBeInTheDocument();
    expect(screen.queryByText("Node not found")).not.toBeInTheDocument();
    expect(screen.queryByText(/Could not load the/)).not.toBeInTheDocument();
    await settle();
    for (const route of publicRoutes()) expect(route).not.toHaveBeenCalled();
  });

  it.each(records)("reads a public node again as anyone would, under the note, for a 404 from the %s", async (_, route) => {
    own(mine({ location_private: false }));
    vi.mocked(api.myNodes).mockResolvedValueOnce([mine({ location_private: false })]).mockResolvedValue([]);
    vi.mocked(route()).mockRejectedValue(notFound());
    renderPage();

    const note = await goneNote();
    expect(await screen.findByText("Detailed metrics and trust analysis")).toBeInTheDocument();
    expect(api.nodeAnalytics).toHaveBeenCalledWith(REF);
    expect(screen.getByText(/^This node is no longer on your account\./)).toBe(note);
    expect(screen.queryByRole("heading", { name: "Last report" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change location privacy" })).not.toBeInTheDocument();
  });

  it.each(records)("keeps the owner view and the card's own error with Retry for a 500 from the %s", async (_, route) => {
    own(mine({ location_private: true }));
    vi.mocked(route()).mockRejectedValueOnce(new HttpError(500, "Internal Server Error", null));
    renderPage();

    const retry = await screen.findByRole("button", { name: "Retry" });
    expect(retry.closest(".card")).toHaveTextContent(/Could not load the|Could not load this node's history/);
    expect(screen.getByRole("heading", { name: "Label and privacy" })).toBeInTheDocument();
    expect(screen.queryByText(/no longer on your account/)).not.toBeInTheDocument();
    for (const route of publicRoutes()) expect(route).not.toHaveBeenCalled();
  });
});

describe("on the admin console", () => {
  const realLocation = window.location;

  afterEach(() => {
    Object.defineProperty(window, "location", { value: realLocation, writable: true, configurable: true });
  });

  it.each(["admin.retina.fm", "staging-admin.retina.fm", "admin.localhost"])(
    "reads the public routes alone on %s, which has no owner routes",
    async (hostname) => {
      Object.defineProperty(window, "location", {
        value: { hostname, pathname: `/nodes/${REF}`, search: "", hash: "", href: `https://${hostname}/nodes/${REF}` },
        writable: true,
        configurable: true,
      });
      // Whoever the session belongs to: it is never asked.
      own(mine());
      vi.mocked(api.adminNodeRefs).mockResolvedValue({ [REF]: ID });
      vi.mocked(api.adminNodeClaim).mockResolvedValue({
        node_id: ID,
        node_ref: REF,
        status: "unclaimed",
        owner: null,
        email: null,
        verified: false,
        undeliverable: false,
        updated_at: null,
        owned_since: null,
        challenge: null,
      });
      renderPage();

      expect(await screen.findByText("Detailed metrics and trust analysis")).toBeInTheDocument();
      const claim = await screen.findByRole("region", { name: "Claim" });
      expect(await within(claim).findByText("Unclaimed", { selector: ".badge" })).toBeInTheDocument();
      expect(api.adminNodeClaim).toHaveBeenCalledWith(ID);
      expect(console.error).not.toHaveBeenCalled();
      expect(api.nodeAnalytics).toHaveBeenCalledWith(REF);
      for (const route of ownerRoutes()) expect(route).not.toHaveBeenCalled();
      expect(screen.queryByRole("heading", { name: "Ownership" })).not.toBeInTheDocument();
    },
  );
});

describe("NodeDetailPage route identity", () => {
  it("removes A's details and owner actions while B is loading, including a failed B fetch", async () => {
    const a = mine({ node_id: "ret-a", node_ref: "a", name: "a" });
    const b = mine({ node_id: "ret-b", node_ref: "b", name: "b" });
    const next = deferred<OwnedNode>();
    vi.mocked(api.myNodes).mockResolvedValue([a, b]);
    vi.mocked(api.myNodeAnalytics).mockResolvedValue(SUMMARY);
    vi.mocked(api.myNode).mockImplementation((id) => (id === "ret-b" ? next.promise : Promise.resolve(a)));
    vi.mocked(api.nodeAnalytics).mockRejectedValue(new HttpError(404, "Not Found", null));
    renderPage("/nodes/a");
    await screen.findByRole("button", { name: /release this node/i });

    fireEvent.click(screen.getByText("Go to B"));

    expect(screen.queryByRole("button", { name: /release this node/i })).not.toBeInTheDocument();
    expect(screen.queryByText("TX Callsign")).not.toBeInTheDocument();
    expect(screen.getByText("Loading…")).toBeInTheDocument();
    // B left the account meanwhile, and has no public page either.
    await act(async () => next.reject(new HttpError(404, "Not Found", null)));
    expect(await screen.findByText("Node not found")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /release this node/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit label" })).not.toBeInTheDocument();
  });
});
