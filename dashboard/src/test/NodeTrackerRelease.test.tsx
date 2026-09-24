import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "../api/client";
import NodeManagementPage from "../pages/admin/NodeManagementPage";

vi.mock("../api/client", () => ({
  api: {
    nodes: vi.fn(),
    analytics: vi.fn(),
    adminNodeContacts: vi.fn(),
    adminNodeOwners: vi.fn(),
    adminNodeReports: vi.fn(),
    adminNodeRefs: vi.fn(),
    adminNodeLocationPrivacy: vi.fn().mockResolvedValue({ location_private: false, location_privacy_source: "default" }),
    setAdminNodeLocationPrivacy: vi.fn(),
    clearAdminNodeLocationPrivacy: vi.fn(),
    adminPolledRadars: vi.fn().mockResolvedValue({ probation_enabled: true, radars: [] }),
  },
}));

const NODES = {
  nodes: {
    nde1a2b3c4d00: { name: "Tracking Node", status: "online" },
    nde2b3c4d5e00: { name: "Older Node", status: "online" },
    nde9f8e7d6c00: { name: "Silent Node", status: "offline" },
  },
};
const NODE_IDS = { nde1a2b3c4d00: "ret1a2b3c4d", nde2b3c4d5e00: "ret2b3c4d5e", nde9f8e7d6c00: "ret9f8e7d6c" };
// The route answers a list keyed by node_id inside each row. A node on a
// contract before 1.6.0 reports versions without the tracker; one that has
// never beaten has no row at all.
const REPORTS = [
  { node_id: "ret1a2b3c4d", versions: { retina_node: "0.9.2", retina_tracker: "0.3.0" } },
  { node_id: "ret2b3c4d5e", versions: { retina_node: "0.9.1" } },
];

function cell(nodeName: string, label: string) {
  const card = screen.getByText(nodeName).closest(".node-card") as HTMLElement;
  return within(card).getByText(label).nextElementSibling;
}

function renderPage() {
  return render(
    <MemoryRouter>
      <NodeManagementPage />
    </MemoryRouter>,
  );
}

describe("NodeManagementPage tracker release", () => {
  beforeEach(() => {
    (api.nodes as any).mockResolvedValue(NODES);
    (api.analytics as any).mockResolvedValue({ nodes: {} });
    (api.adminNodeRefs as any).mockResolvedValue(NODE_IDS);
    (api.adminNodeContacts as any).mockResolvedValue({});
    (api.adminNodeOwners as any).mockResolvedValue({});
    (api.adminNodeReports as any).mockResolvedValue(REPORTS);
  });

  it("shows the release the node's last heartbeat named", async () => {
    renderPage();

    await screen.findByText("Tracking Node");
    expect(await screen.findByText("0.3.0")).toBeInTheDocument();
    expect(cell("Tracking Node", "Tracker release")).toHaveTextContent("0.3.0");
  });

  it("shows a dash for a node that names no tracker or has never beaten", async () => {
    renderPage();

    await screen.findByText("0.3.0");
    expect(cell("Older Node", "Tracker release")).toHaveTextContent(/^—$/);
    expect(cell("Silent Node", "Tracker release")).toHaveTextContent(/^—$/);
  });

  it("still renders the node cards when the reports fetch fails", async () => {
    (api.adminNodeReports as any).mockRejectedValue(new Error("500"));

    renderPage();

    expect(await screen.findByText("Tracking Node")).toBeInTheDocument();
    expect(cell("Tracking Node", "Tracker release")).toHaveTextContent(/^—$/);
  });

  it("still renders the node cards when the reports come back in a shape it cannot read", async () => {
    (api.adminNodeReports as any).mockResolvedValue({});

    renderPage();

    expect(await screen.findByText("Tracking Node")).toBeInTheDocument();
    expect(cell("Tracking Node", "Tracker release")).toHaveTextContent(/^—$/);
  });
});
