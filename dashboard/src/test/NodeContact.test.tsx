import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "../api/client";
import NodeManagementPage, { contactLabel } from "../pages/admin/NodeManagementPage";

// Every api method the page reaches for, not only the three this file drives:
// an unmocked one throws out of the effect and takes the render with it.
vi.mock("../api/client", () => ({
  api: {
    nodes: vi.fn(),
    analytics: vi.fn(),
    adminNodeContacts: vi.fn(),
    adminNodeRefs: vi.fn(),
    adminNodeLocationPrivacy: vi.fn().mockResolvedValue({ location_private: false, location_privacy_source: "default" }),
    setAdminNodeLocationPrivacy: vi.fn(),
    clearAdminNodeLocationPrivacy: vi.fn(),
  },
}));

describe("contactLabel", () => {
  it("joins the names it has", () => {
    expect(contactLabel({ first_name: "Ada", last_name: "Lovelace", email: "ada@example.com" })).toBe(
      "Ada Lovelace",
    );
  });

  it("falls back to the address when there is no name", () => {
    expect(contactLabel({ first_name: null, last_name: null, email: "ada@example.com" })).toBe(
      "ada@example.com",
    );
  });

  it("uses one name when that is all there is", () => {
    expect(contactLabel({ first_name: "Ada", last_name: null, email: null })).toBe("Ada");
  });

  it("falls through to the phone when that is the only field set", () => {
    expect(contactLabel({ first_name: null, last_name: null, email: null, phone: "+44 20 7946 0000" })).toBe(
      "+44 20 7946 0000",
    );
  });

  it("is a dash when the node reported nothing", () => {
    expect(contactLabel(undefined)).toBe("—");
    expect(contactLabel({ first_name: null, last_name: null, email: null, phone: null })).toBe("—");
  });
});

// The node listing is a public feed: keyed on node_ref, carrying no node_id.
// The contacts it is joined against are keyed on node_id, so the two only meet
// through the admin ref map.
const NODES = {
  nodes: {
    nde1a2b3c4d00: { name: "Ada's Node", status: "online" },
    nde9f8e7d6c00: { name: "No Contact Node", status: "online" },
  },
};
const NODE_IDS = { nde1a2b3c4d00: "ret1a2b3c4d", nde9f8e7d6c00: "ret9f8e7d6c" };
const ADA_CONTACT = { ret1a2b3c4d: { first_name: "Ada", last_name: "Lovelace", email: "ada@example.com" } };

/** The Contact cell of one card, found through its own label.
 *
 *  Asserting on the card as a whole cannot see this cell: Frequency and Uptime
 *  render the same dash for a node with neither, so a card-wide assertion holds
 *  whatever the contact does. */
function contactCell(nodeName: string) {
  const card = screen.getByText(nodeName).closest(".node-card") as HTMLElement;
  return within(card).getByText("Contact").nextElementSibling;
}

function renderPage() {
  return render(
    <MemoryRouter>
      <NodeManagementPage />
    </MemoryRouter>,
  );
}

describe("NodeManagementPage contact rendering", () => {
  beforeEach(() => {
    (api.nodes as any).mockResolvedValue(NODES);
    (api.analytics as any).mockResolvedValue({ nodes: {} });
    (api.adminNodeRefs as any).mockResolvedValue(NODE_IDS);
  });

  it("shows the contact label on a node with a contact on file", async () => {
    (api.adminNodeContacts as any).mockResolvedValue(ADA_CONTACT);

    renderPage();

    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
  });

  it("shows a dash for a node with no contact on file", async () => {
    (api.adminNodeContacts as any).mockResolvedValue(ADA_CONTACT);

    renderPage();

    await screen.findByText("Ada Lovelace");
    expect(contactCell("No Contact Node")).toHaveTextContent("—");
    expect(contactCell("Ada's Node")).toHaveTextContent("Ada Lovelace");
  });

  it("still renders the node cards when the contacts fetch fails", async () => {
    (api.adminNodeContacts as any).mockRejectedValue(new Error("501: no such route yet"));

    renderPage();

    expect(await screen.findByText("Ada's Node")).toBeInTheDocument();
    expect(screen.getByText("No Contact Node")).toBeInTheDocument();
    await waitFor(() => expect(contactCell("Ada's Node")).toHaveTextContent("—"));
  });
});
