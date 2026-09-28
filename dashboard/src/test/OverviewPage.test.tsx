import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useParams } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import OverviewPage from "../pages/user/OverviewPage";

vi.mock("../api/client", () => ({
  api: {
    nodes: vi.fn(), analytics: vi.fn(), aircraft: vi.fn(), myNodes: vi.fn(), myAircraft: vi.fn(),
  },
}));
vi.mock("recharts", async (importOriginal) => ({
  ...(await importOriginal<typeof import("recharts")>()),
  ResponsiveContainer: () => null,
}));

// A private node is on no public listing, so the owner's own copy is the only
// one the page has, and it carries the node_id the page must not show.
const OWNED = {
  node_id: "ret1a2b3c4d",
  node_ref: "nde0123456789",
  name: "nde0123456789",
  status: "active",
  position_status: "missing_rx",
  location_private: true,
  polled: null,
};

// Somebody else's node, on the public listing with analytics of its own.
const THEIRS_REF = "ndezzzzzzzzzzzz";
const THEIRS = {
  status: "active",
  name: "Their rooftop",
  config_hash: "feedfacecafe",
  position_status: "missing_tx",
};

const summary = (detections: number, tracks: number) => ({
  metrics: { total_detections: detections, total_tracks: tracks },
});

function renderWith(
  mine: object[],
  {
    fleet = {},
    analytics = {},
    aircraft = [],
  }: { fleet?: object; analytics?: object; aircraft?: object[] } = {},
) {
  vi.mocked(api.myNodes).mockResolvedValue(mine);
  vi.mocked(api.nodes).mockResolvedValue({ nodes: fleet });
  vi.mocked(api.analytics).mockResolvedValue({ nodes: analytics });
  vi.mocked(api.myAircraft).mockResolvedValue({ aircraft });
  // Answered, though the page must not ask: the whole fleet's aircraft.
  vi.mocked(api.aircraft).mockResolvedValue({ aircraft: [{ hex: "ff0001" }, { hex: "ff0002" }, { hex: "ff0003" }] });
  render(
    <MemoryRouter initialEntries={["/overview"]}>
      <Routes>
        <Route path="/overview" element={<OverviewPage />} />
        <Route path="/nodes/:ref" element={<Landed kind="detail" />} />
        <Route path="/radars/:ref" element={<Landed kind="radar" />} />
      </Routes>
    </MemoryRouter>,
  );
}

function Landed({ kind }: { kind: string }) {
  return <div>{`${kind} ${useParams().ref}`}</div>;
}

// By the tile's label, since the cards below reuse some of the same words.
const stat = (label: string) =>
  [...document.querySelectorAll(".stat-label")].find((el) => el.textContent === label)!.closest(".stat-card");

async function ownedCard(): Promise<HTMLElement> {
  const names = await screen.findAllByText("nde0123456789");
  return names.map((el) => el.closest<HTMLElement>(".node-card")).find(Boolean)!;
}

describe("OverviewPage", () => {
  beforeEach(() => vi.resetAllMocks());

  it("names an owned node by its node_ref", async () => {
    renderWith([OWNED]);
    expect(await screen.findAllByText("nde0123456789")).toHaveLength(2);
    expect(screen.queryByText("ret1a2b3c4d")).not.toBeInTheDocument();
  });

  it("shows a dash, not the node_id, for a node with neither a name nor a ref", async () => {
    renderWith([{ ...OWNED, node_ref: null, name: null }]);
    await screen.findByText("My Nodes");
    expect(screen.queryByText(/ret1a2b3c4d/)).not.toBeInTheDocument();
    expect(screen.getByRole("table").querySelector("td")).toHaveTextContent(/^—$/);
    expect(document.querySelector(".node-name")).toHaveTextContent(/—$/);
  });

  it("leaves every node the caller does not own out of the page", async () => {
    renderWith([OWNED], {
      fleet: { [THEIRS_REF]: THEIRS },
      analytics: { [OWNED.node_ref]: summary(5, 2), [THEIRS_REF]: summary(900, 70) },
    });
    await screen.findByText("My Nodes");
    expect(screen.queryByText("Their rooftop")).not.toBeInTheDocument();
    expect(stat("Nodes Online")).toHaveTextContent("1 / 1");
    expect(stat("Frame Detections")).toHaveTextContent("5");
    expect(stat("Tracks")).toHaveTextContent("2");
    expect(document.querySelectorAll(".node-card")).toHaveLength(1);
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(api.nodes).not.toHaveBeenCalled();
  });

  it("reads a private node's analytics, which the route returns to its owner", async () => {
    renderWith([OWNED], { analytics: { [OWNED.node_ref]: summary(1234, 56) } });
    const card = await ownedCard();
    expect(card).toHaveTextContent("Detections1,234");
    expect(card).toHaveTextContent("Tracks56");
  });

  it("shows a private node's config hash, which the public listing leaves out", async () => {
    renderWith([{ ...OWNED, config_hash: "0123456789abcdef" }]);
    expect(await screen.findByText("01234567")).toBeInTheDocument();
  });

  it("counts aircraft from the owner feed, not the public one", async () => {
    renderWith([OWNED], { aircraft: [{ hex: "aa0001" }, { hex: "aa0002" }] });
    await screen.findByText("My Nodes");
    expect(stat("Live Aircraft")).toHaveTextContent("2");
    expect(api.aircraft).not.toHaveBeenCalled();
  });

  it("counts a node the owner's list has never seen as offline", async () => {
    renderWith([OWNED, { ...OWNED, node_id: "ret00000000", node_ref: "nde000000000000", status: "never_connected" }]);
    await screen.findByText("My Nodes");
    expect(stat("Nodes Online")).toHaveTextContent("1 / 2");
  });

  it("sends a node's card to its detail page", async () => {
    renderWith([OWNED]);
    fireEvent.click(await ownedCard());
    expect(await screen.findByText("detail nde0123456789")).toBeInTheDocument();
  });

  it("sends a polled radar's card to the page that manages it", async () => {
    renderWith([{ ...OWNED, polled: { liveness: "streaming" } }]);
    fireEvent.click(await ownedCard());
    expect(await screen.findByText("radar nde0123456789")).toBeInTheDocument();
  });

  it("charts detections only where there is more than one node to compare", async () => {
    renderWith([OWNED]);
    await screen.findByText("My Nodes");
    expect(screen.queryByText("Detections by Node")).not.toBeInTheDocument();
  });

  it("charts detections across two or more nodes", async () => {
    renderWith([OWNED, { ...OWNED, node_id: "ret00000000", node_ref: "nde000000000000" }]);
    expect(await screen.findByText("Detections by Node")).toBeInTheDocument();
  });

  it("points an owner of nothing at how to connect a node", async () => {
    renderWith([]);
    expect(await screen.findByRole("link", { name: "Connect your node" })).toHaveAttribute("href", "/onboarding");
  });
});
