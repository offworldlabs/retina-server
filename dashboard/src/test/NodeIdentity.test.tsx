import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "../api/client";
import NodeManagementPage from "../pages/admin/NodeManagementPage";
import NetworkHealthPage from "../pages/admin/NetworkHealthPage";
import CustodyPage from "../pages/admin/CustodyPage";

// Every api method either page reaches for: an unmocked one throws out of the
// effect and takes the render with it.
vi.mock("../api/client", () => ({
  api: {
    nodes: vi.fn(),
    analytics: vi.fn(),
    aircraft: vi.fn().mockResolvedValue({ aircraft: [] }),
    fleetDashboard: vi.fn().mockResolvedValue({}),
    adminNodeContacts: vi.fn().mockResolvedValue({}),
    adminNodeRefs: vi.fn(),
    custody: vi.fn(),
    adminNodeLocationPrivacy: vi.fn().mockResolvedValue({
      location_private: false,
      location_privacy_source: "default",
    }),
    setAdminNodeLocationPrivacy: vi.fn(),
    clearAdminNodeLocationPrivacy: vi.fn(),
    adminPolledRadars: vi.fn().mockResolvedValue({ probation_enabled: true, radars: [] }),
    setAdminPolledRadarTrust: vi.fn(),
  },
}));

// The public feeds key on node_ref and carry no node_id, which is the whole
// point of the boundary — so that is how the fixtures are shaped.
const REF = "nde1a2b3c4d00";
const NODE_ID = "ret1a2b3c4d";
const NODES = { nodes: { [REF]: { name: "Ada's Node", status: "online", node_ref: REF } } };

function renderPage(Page) {
  return render(
    <MemoryRouter>
      <Page />
    </MemoryRouter>,
  );
}

function setup({ refs, contacts }: { refs?: Record<string, string>; contacts?: Record<string, any> } = {}) {
  refs = refs ?? { [REF]: NODE_ID };
  contacts = contacts ?? {};
  (api.nodes as any).mockResolvedValue(NODES);
  (api.analytics as any).mockResolvedValue({ nodes: {} });
  (api.adminNodeRefs as any).mockResolvedValue(refs);
  (api.adminNodeContacts as any).mockResolvedValue(contacts);
}

describe("Node Management: both identifiers", () => {
  it("shows the node_ref and the node_id on the card", async () => {
    setup();

    renderPage(NodeManagementPage);

    const card = (await screen.findByText("Ada's Node")).closest(".node-card") as HTMLElement;
    await waitFor(() => expect(within(card).getByText("Node ID").nextElementSibling).toHaveTextContent(NODE_ID));
    expect(within(card).getByText("Node ref").nextElementSibling).toHaveTextContent(REF);
  });

  it("links the node to the site its node_id names, not its ref", async () => {
    setup();

    renderPage(NodeManagementPage);

    const link = await screen.findByRole("link", { name: /Ada's Node/ });
    expect(link).toHaveAttribute("href", `https://${NODE_ID}.retnode.com`);
  });

  it("offers no link for a ref it cannot resolve to a node id", async () => {
    setup({ refs: {} });

    renderPage(NodeManagementPage);

    expect(await screen.findByText("Ada's Node")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("link", { name: /Ada's Node/ })).toBeNull());
  });

  it("joins the contact on the node_id the contacts route is keyed on", async () => {
    setup({ contacts: { [NODE_ID]: { first_name: "Ada", last_name: "Lovelace" } } });

    renderPage(NodeManagementPage);

    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
  });

  it("asks for location privacy by node_id, which the override is keyed on", async () => {
    setup();

    renderPage(NodeManagementPage);

    await waitFor(() => expect(api.adminNodeLocationPrivacy).toHaveBeenCalledWith(NODE_ID));
  });

  it("does not ask for location privacy under a ref", async () => {
    setup({ refs: {} });

    renderPage(NodeManagementPage);

    expect(await screen.findByText("Ada's Node")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByText(/no node id/i)).toBeInTheDocument());
    expect(api.adminNodeLocationPrivacy).not.toHaveBeenCalledWith(REF);
  });
});

describe("Network Health: both identifiers", () => {
  it("shows the ref and the node id in their own columns", async () => {
    setup();

    renderPage(NetworkHealthPage);

    const row = (await screen.findByText(REF)).closest("tr") as HTMLElement;
    await waitFor(() => expect(within(row).getByText(NODE_ID)).toBeInTheDocument());
  });

  it("links the ref to the site the node_id names", async () => {
    setup();

    renderPage(NetworkHealthPage);

    const link = await screen.findByRole("link", { name: new RegExp(REF) });
    expect(link).toHaveAttribute("href", `https://${NODE_ID}.retnode.com`);
  });
});

describe("Chain of custody: both identifiers", () => {
  it("names the node behind the ref the chain is published under", async () => {
    setup();
    (api.custody as any).mockResolvedValue({ node_keys: { [REF]: { signing_mode: "hardware" } } });

    renderPage(CustodyPage);

    const row = (await screen.findByText(REF)).closest("tr") as HTMLElement;
    await waitFor(() => expect(within(row).getByText(NODE_ID)).toBeInTheDocument());
  });
});
