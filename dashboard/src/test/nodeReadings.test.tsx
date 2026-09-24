import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter, Route, Routes } from "react-router-dom";

import { api } from "../api/client";
import NetworkHealthPage from "../pages/admin/NetworkHealthPage";
import NodeManagementPage from "../pages/admin/NodeManagementPage";
import NodeDetailPage from "../pages/user/NodeDetailPage";
import RFEnvironmentPage from "../pages/user/RFEnvironmentPage";
import TunnelLinkPage from "../pages/user/TunnelLinkPage";

// Every api method these pages reach for: an unmocked one throws out of the
// effect and takes the render with it.
vi.mock("../api/client", () => ({
  api: {
    nodes: vi.fn(),
    analytics: vi.fn(),
    nodeAnalytics: vi.fn(),
    myNodes: vi.fn(),
    aircraft: vi.fn(),
    fleetDashboard: vi.fn(),
    adminNodeRefs: vi.fn(),
    adminNodeContacts: vi.fn(),
    adminNodeOwners: vi.fn(),
    adminNodeReports: vi.fn(),
    adminNodeLocationPrivacy: vi.fn(),
    adminPolledRadars: vi.fn(),
  },
}));
vi.mock("recharts", async (importOriginal) => ({
  ...await importOriginal<typeof import("recharts")>(),
  ResponsiveContainer: () => null,
}));

const REF = "nde0a1b2c3d4e5f";

// A summary whose frame metrics have counted nothing while its detection area
// has: the case where pages reading only one counter disagree.
const SUMMARY = {
  metrics: { total_detections: 0, total_frames: 100 },
  detection_area: { n_detections: 42 },
};

function serve(status: string | null = "active") {
  vi.mocked(api.nodes).mockResolvedValue({ nodes: { [REF]: { name: "Rooftop", status, node_ref: REF } } });
  vi.mocked(api.analytics).mockResolvedValue({ nodes: { [REF]: SUMMARY } });
  vi.mocked(api.nodeAnalytics).mockResolvedValue({ ...SUMMARY, node_ref: REF });
  vi.mocked(api.myNodes).mockResolvedValue([]);
  vi.mocked(api.aircraft).mockResolvedValue({ aircraft: [] });
  vi.mocked(api.fleetDashboard).mockResolvedValue({});
  vi.mocked(api.adminNodeRefs).mockResolvedValue({});
  vi.mocked(api.adminNodeContacts).mockResolvedValue({});
  vi.mocked(api.adminNodeOwners).mockResolvedValue({});
  vi.mocked(api.adminPolledRadars).mockResolvedValue({ probation_enabled: true, radars: [] });
  vi.mocked(api.adminNodeReports).mockResolvedValue([]);
}

const valueBeside = async (label: string) => (await screen.findByText(label)).nextElementSibling;

beforeEach(() => {
  vi.resetAllMocks();
});

describe("a node's detection count", () => {
  it("reads 42 on Node Management", async () => {
    serve();
    render(<MemoryRouter><NodeManagementPage /></MemoryRouter>);
    const card = (await screen.findByText("Rooftop")).closest(".node-card") as HTMLElement;
    expect(within(card).getByText("Detections").nextElementSibling).toHaveTextContent(/^42$/);
  });

  it("reads 42 on the node's own page", async () => {
    serve();
    render(
      <MemoryRouter initialEntries={[`/nodes/${REF}`]}>
        <Routes><Route path="/nodes/:nodeId" element={<NodeDetailPage />} /></Routes>
      </MemoryRouter>,
    );
    expect(await valueBeside("Total Detections")).toHaveTextContent(/^42$/);
  });

  it("reads 42 on RF Environment, and the rate is taken from the same count", async () => {
    serve();
    render(<RFEnvironmentPage />);
    expect(await valueBeside("Detections")).toHaveTextContent(/^42$/);
    expect(screen.getByText("Detection Rate").nextElementSibling).toHaveTextContent("42.0%");
  });
});

describe("a node's labels", () => {
  // The listing behind the page is keyed on the ref and carries no node_id.
  it("calls the ref on RF Environment a node ref", async () => {
    serve();
    render(<RFEnvironmentPage />);
    expect(await valueBeside("Node ref")).toHaveTextContent(REF);
    expect(screen.queryByText("Node ID")).toBeNull();
  });
});

describe("a node's liveness", () => {
  it("counts a node with no status as offline in the stat card and the row alike", async () => {
    serve(null);
    render(<MemoryRouter><NetworkHealthPage /></MemoryRouter>);
    expect(await valueBeside("Nodes Online")).toHaveTextContent("0 / 1");
    const row = screen.getByText(REF).closest("tr") as HTMLElement;
    expect(within(row).getByText("Offline")).toBeInTheDocument();
  });

  it("does not offer a node that never connected a local display", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ node_id: "ret1a2b3c4d", name: "Rooftop", status: "never_connected" }]);
    render(<TunnelLinkPage />);
    const row = (await screen.findByText(/Rooftop/)).closest("tr") as HTMLElement;
    expect(within(row).getByText("Never connected")).toBeInTheDocument();
    expect(within(row).queryByText(/node-ip/)).toBeNull();
  });
});
