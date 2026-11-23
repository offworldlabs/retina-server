import { render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";

import { api } from "../api/client";
import MlatVerificationPage from "../pages/admin/MlatVerificationPage";
import NetworkHealthPage from "../pages/admin/NetworkHealthPage";
import NodeManagementPage from "../pages/admin/NodeManagementPage";
import { PolledRadars } from "../pages/admin/PolledRadars";
import { TABLET } from "../styles/breakpoints";
import { stubViewport } from "./matchMedia";

vi.mock("../api/client", () => ({
  api: {
    nodes: vi.fn(),
    analytics: vi.fn(),
    aircraft: vi.fn().mockResolvedValue({ aircraft: [] }),
    fleetDashboard: vi.fn().mockResolvedValue({}),
    adminNodeContacts: vi.fn(),
    adminNodeOwners: vi.fn(),
    adminNodeReports: vi.fn().mockResolvedValue([]),
    adminNodeRefs: vi.fn(),
    adminPolledRadars: vi.fn(),
    mlatVerification: vi.fn(),
    mlatAccuracy: vi.fn().mockResolvedValue({ n_samples: 0 }),
    mlatHistory: vi.fn(),
  },
}));

const REF = "nde1a2b3c4d00";
const NODE_ID = "ret1a2b3c4d";
const EMAIL = "owner@example.com";

afterEach(() => {
  vi.unstubAllGlobals();
});

beforeEach(() => {
  vi.mocked(api.nodes).mockResolvedValue({ nodes: { [REF]: { name: "Ada's Node", status: "online" } } });
  vi.mocked(api.analytics).mockResolvedValue({ nodes: {} });
  vi.mocked(api.adminNodeRefs).mockResolvedValue({ [REF]: NODE_ID });
  vi.mocked(api.adminNodeContacts).mockResolvedValue({});
  vi.mocked(api.adminNodeOwners).mockResolvedValue({ [NODE_ID]: { email: EMAIL } });
  vi.mocked(api.adminPolledRadars).mockResolvedValue({ probation_enabled: true, polling_enabled: false, radars: [] });
});

function renderPage(page: React.ReactElement) {
  return render(<MemoryRouter>{page}</MemoryRouter>);
}

/** The header cell and the body cell under it, by the column's text. */
function column(container: HTMLElement, title: string) {
  const heads = Array.from(container.querySelectorAll("thead th"));
  const index = heads.findIndex((th) => th.textContent === title);
  expect(index, title).toBeGreaterThanOrEqual(0);
  const body = container.querySelector("tbody tr")?.children[index] ?? null;
  return { head: heads[index], body };
}

describe("the Network Health table", () => {
  it("heads a phone card with the node ref and keeps SNR, trust and reputation behind More", async () => {
    stubViewport(390);
    const { container } = renderPage(<NetworkHealthPage />);
    await screen.findByText(REF);
    expect(column(container, "Node ref").body).toHaveAttribute("data-hint", "title");
    for (const title of ["Avg SNR", "Trust", "Reputation"]) {
      expect(column(container, title).body, title).toHaveAttribute("data-hint", "secondary");
    }
  });

  it("drops SNR, trust and reputation on a tablet and keeps the node ID", async () => {
    stubViewport(820);
    const { container } = renderPage(<NetworkHealthPage />);
    await screen.findByText(REF);
    for (const title of ["Avg SNR", "Trust", "Reputation"]) {
      expect(column(container, title).head, title).toHaveAttribute("data-hint", "hidden");
    }
    expect(column(container, "Node ID").head).not.toHaveAttribute("data-hint", "hidden");
  });
});

describe("a node card's emails", () => {
  const ownerEmail = (card: HTMLElement) => within(card).getByText("Owner email").nextElementSibling as HTMLElement;
  const card = async () => (await screen.findByText("Ada's Node")).closest(".node-card") as HTMLElement;

  it("offers a line break just before the @ on a phone", async () => {
    stubViewport(390);
    renderPage(<NodeManagementPage />);
    const value = ownerEmail(await card());
    await waitFor(() => expect(value.querySelector("wbr")).not.toBeNull());
    const wbr = value.querySelector("wbr")!;
    expect(wbr.previousSibling?.textContent).toBe("owner");
    expect(wbr.nextSibling?.textContent).toBe("@example.com");
  });

  it("is one text node, the address, on a desktop", async () => {
    stubViewport(1440);
    renderPage(<NodeManagementPage />);
    const value = ownerEmail(await card());
    await waitFor(() => expect(value).toHaveTextContent(EMAIL));
    expect(value.childNodes).toHaveLength(1);
    expect(value.firstChild?.nodeValue).toBe(EMAIL);
  });

  it("listens for the tablet width once for the page, not once per address", async () => {
    stubViewport(390);
    const base = window.matchMedia;
    const subscribers = new Set<unknown>();
    vi.stubGlobal("matchMedia", (query: string) => {
      const list = base(query);
      if (query !== TABLET) return list;
      return Object.assign(Object.create(list), {
        addEventListener: (type: string, fn: unknown) => {
          subscribers.add(fn);
          list.addEventListener(type, fn as EventListener);
        },
        removeEventListener: (type: string, fn: unknown) => {
          subscribers.delete(fn);
          list.removeEventListener(type, fn as EventListener);
        },
      });
    });
    // The polled radars table below the cards listens for the same width until
    // its fetch fails and it is replaced, so what is left counts the cards.
    vi.mocked(api.adminPolledRadars).mockRejectedValue(new Error("unreachable"));
    const refs = ["nde1a2b3c4d00", "nde1a2b3c4d01", "nde1a2b3c4d02"];
    const ids = ["ret1a2b3c4d", "ret9f8e7d6c", "ret2b3c4d5e"];
    vi.mocked(api.nodes).mockResolvedValue({
      nodes: Object.fromEntries(refs.map((ref, i) => [ref, { name: `Node ${i}`, status: "online" }])),
    });
    vi.mocked(api.adminNodeRefs).mockResolvedValue(Object.fromEntries(refs.map((ref, i) => [ref, ids[i]])));
    vi.mocked(api.adminNodeOwners).mockResolvedValue(
      Object.fromEntries(ids.map((id, i) => [id, { email: `owner${i}@example.com` }])),
    );
    vi.mocked(api.adminNodeContacts).mockResolvedValue(
      Object.fromEntries(ids.map((id, i) => [id, { email: `site${i}@example.com` }])),
    );
    const { container } = renderPage(<NodeManagementPage />);
    await waitFor(() => expect(container.querySelectorAll("wbr")).toHaveLength(6));
    await waitFor(() => expect(subscribers.size).toBe(1));
  });

  it("shows a dash when there is no address", async () => {
    stubViewport(390);
    vi.mocked(api.adminNodeOwners).mockResolvedValue({});
    renderPage(<NodeManagementPage />);
    const value = ownerEmail(await card());
    expect(value).toHaveTextContent("—");
    expect(value.querySelector("wbr")).toBeNull();
  });
});

describe("the polled radars table", () => {
  it("gives the buttons a card line of their own and folds epoch and last decision behind More", async () => {
    stubViewport(390);
    vi.mocked(api.adminPolledRadars).mockResolvedValue({
      probation_enabled: true,
      polling_enabled: false,
      radars: [{
        node_id: "bla0a1b2c3d",
        node_ref: "nde0a1b2c3d4e5f",
        endpoint: "radar-one.example.com:3000",
        liveness: "streaming",
        last_frame_at: null,
        epoch: 2,
        trust_state: "probation",
        events: [],
        owner: { email: "ada@example.com", signed_in: true },
      }],
    });
    const { container } = render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    const cells = Array.from(container.querySelectorAll("tbody tr > td:not([data-card-more])"));
    expect(cells[cells.length - 1]).toHaveAttribute("data-label", "");
    expect(column(container, "Epoch").body).toHaveAttribute("data-hint", "secondary");
    expect(column(container, "Last decision").body).toHaveAttribute("data-hint", "secondary");
  });
});

describe("the MLAT tracks table", () => {
  beforeEach(() => {
    vi.mocked(api.mlatVerification).mockResolvedValue({
      match_threshold_km: 10,
      tracks: [{
        solver_hex: "mn00000000a1",
        truth_hex: "a1a1a1",
        position_error_km: 1.2,
        velocity_error_ms: 4.2,
        altitude_error_m: 120,
        n_nodes: 3,
        rms_delay: 0.41,
        rms_doppler: 1.8,
        timestamp_ms: Date.now() - 5_000,
      }],
    });
  });

  it("drops the RMS columns on a tablet", async () => {
    stubViewport(820);
    const { container } = renderPage(<MlatVerificationPage />);
    await screen.findByRole("link", { name: "mn00000000a1" });
    expect(column(container, "RMS delay").head).toHaveAttribute("data-hint", "hidden");
    expect(column(container, "RMS Doppler").head).toHaveAttribute("data-hint", "hidden");
  });

  it("folds the RMS columns behind More on a phone", async () => {
    stubViewport(390);
    const { container } = renderPage(<MlatVerificationPage />);
    await screen.findByRole("link", { name: "mn00000000a1" });
    expect(column(container, "RMS delay").body).toHaveAttribute("data-hint", "secondary");
    expect(column(container, "RMS Doppler").body).toHaveAttribute("data-hint", "secondary");
  });
});
