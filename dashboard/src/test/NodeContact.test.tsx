import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { api } from "../api/client";
import NodeManagementPage, { contactName, contactPhone } from "../pages/admin/NodeManagementPage";

// Every api method the page reaches for, not only the ones this file drives:
// an unmocked one throws out of the effect and takes the render with it.
vi.mock("../api/client", () => ({
  api: {
    nodes: vi.fn(),
    analytics: vi.fn(),
    adminNodeContacts: vi.fn(),
    adminNodeOwners: vi.fn(),
    adminNodeRefs: vi.fn(),
    adminNodeLocationPrivacy: vi.fn().mockResolvedValue({ location_private: false, location_privacy_source: "default" }),
    setAdminNodeLocationPrivacy: vi.fn(),
    clearAdminNodeLocationPrivacy: vi.fn(),
    adminPolledRadars: vi.fn().mockResolvedValue({ probation_enabled: true, radars: [] }),
    setAdminPolledRadarTrust: vi.fn(),
  },
}));

describe("contactName", () => {
  it("joins the names it has", () => {
    expect(contactName({ first_name: "Ada", last_name: "Lovelace", email: "ada@example.com" })).toBe("Ada Lovelace");
  });

  it("uses one name when that is all there is", () => {
    expect(contactName({ first_name: "Ada", last_name: null })).toBe("Ada");
  });

  it("is a dash without a name, whatever else was reported", () => {
    expect(contactName({ first_name: null, last_name: null, email: "ada@example.com", phone: "020" })).toBe("—");
    expect(contactName(undefined)).toBe("—");
  });
});

describe("contactPhone", () => {
  it("puts the country beside the number", () => {
    expect(contactPhone({ phone: "07700 900123", country: "GB" })).toBe("07700 900123 (GB)");
  });

  it("shows the number alone when no country came with it", () => {
    expect(contactPhone({ phone: "+44 20 7946 0000", country: null })).toBe("+44 20 7946 0000");
  });

  it("is a dash without a number, even with a country", () => {
    expect(contactPhone({ phone: null, country: "GB" })).toBe("—");
    expect(contactPhone(undefined)).toBe("—");
  });
});

// The node listing is a public feed: keyed on node_ref, carrying no node_id.
// The contacts and owners it is joined against are keyed on node_id, so they
// only meet through the admin ref map.
const NODES = {
  nodes: {
    nde1a2b3c4d00: { name: "Ada's Node", status: "online" },
    nde9f8e7d6c00: { name: "No Contact Node", status: "online" },
  },
};
const NODE_IDS = { nde1a2b3c4d00: "ret1a2b3c4d", nde9f8e7d6c00: "ret9f8e7d6c" };
const ADA_CONTACT = {
  ret1a2b3c4d: {
    first_name: "Ada",
    last_name: "Lovelace",
    email: "ada@example.com",
    phone: "07700 900123",
    country: "GB",
  },
};
const BOB_OWNS_ADAS_NODE = { ret1a2b3c4d: { user_id: "7", email: "bob@example.com", name: "Bob" } };

/** One row's value on one card, found through the row's own label.
 *
 *  Asserting on the card as a whole cannot see a single row: Frequency and
 *  Availability render the same dash for a node with neither, so a card-wide
 *  assertion holds whatever the row does. */
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

describe("NodeManagementPage contact rendering", () => {
  beforeEach(() => {
    (api.nodes as any).mockResolvedValue(NODES);
    (api.analytics as any).mockResolvedValue({ nodes: {} });
    (api.adminNodeRefs as any).mockResolvedValue(NODE_IDS);
    (api.adminNodeContacts as any).mockResolvedValue(ADA_CONTACT);
    (api.adminNodeOwners as any).mockResolvedValue(BOB_OWNS_ADAS_NODE);
  });

  it("gives each site contact field and the owner a row of its own", async () => {
    renderPage();

    await screen.findByText("Ada Lovelace");
    expect(cell("Ada's Node", "Site contact")).toHaveTextContent("Ada Lovelace");
    expect(cell("Ada's Node", "Site contact email")).toHaveTextContent("ada@example.com");
    expect(cell("Ada's Node", "Site contact phone")).toHaveTextContent("07700 900123 (GB)");
    expect(cell("Ada's Node", "Owner email")).toHaveTextContent("bob@example.com");
  });

  it("shows a dash in every row for a node with no contact and no owner", async () => {
    renderPage();

    await screen.findByText("Ada Lovelace");
    for (const label of ["Site contact", "Site contact email", "Site contact phone", "Owner email"]) {
      expect(cell("No Contact Node", label)).toHaveTextContent(/^—$/);
    }
  });

  it("still renders the node cards when the contacts fetch fails", async () => {
    (api.adminNodeContacts as any).mockRejectedValue(new Error("501: no such route yet"));

    renderPage();

    expect(await screen.findByText("Ada's Node")).toBeInTheDocument();
    expect(screen.getByText("No Contact Node")).toBeInTheDocument();
    await waitFor(() => expect(cell("Ada's Node", "Owner email")).toHaveTextContent("bob@example.com"));
    expect(cell("Ada's Node", "Site contact email")).toHaveTextContent("—");
  });

  it("still renders the node cards when the owners fetch fails", async () => {
    (api.adminNodeOwners as any).mockRejectedValue(new Error("500"));

    renderPage();

    expect(await screen.findByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("No Contact Node")).toBeInTheDocument();
    expect(cell("Ada's Node", "Owner email")).toHaveTextContent("—");
  });
});
