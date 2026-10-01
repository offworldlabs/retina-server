import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";

import { api } from "../api/client";
import { HttpError } from "@retina/shared";
import { MemoryRouter } from "react-router-dom";
import NodeManagementPage from "../pages/admin/NodeManagementPage";
import { PolledRadars } from "../pages/admin/PolledRadars";

// The node page's own methods too, for the one test that renders it whole.
vi.mock("../api/client", () => ({
  api: {
    adminPolledRadars: vi.fn(),
    setAdminPolledRadarTrust: vi.fn(),
    adminWithdrawPolledRadar: vi.fn(),
    adminCheckConnection: vi.fn(),
    adminConnectPolledRadar: vi.fn(),
    nodes: vi.fn().mockResolvedValue({ nodes: {} }),
    analytics: vi.fn().mockResolvedValue({ nodes: {} }),
    adminNodeContacts: vi.fn().mockResolvedValue({}),
    adminNodeRefs: vi.fn().mockResolvedValue({}),
  },
}));

const ON_PROBATION = {
  node_id: "bla0a1b2c3d",
  node_ref: "nde0a1b2c3d4e5f",
  endpoint: "radar-one.example.com:3000",
  liveness: "streaming",
  last_frame_at: "2026-09-23T12:00:00+00:00",
  epoch: 2,
  trust_state: "probation",
  events: [],
  owner: { email: "ada@example.com", signed_in: false },
};
const GRADUATED = {
  node_id: "bla1f2e3d4c",
  node_ref: "nde1f2e3d4c5b6a",
  endpoint: "radar-two.example.com:3000",
  liveness: "stalled",
  last_frame_at: null,
  epoch: 1,
  trust_state: "graduated",
  events: [{ kind: "graduated", epoch: 1, actor: "admin:ada@example.com", at: "2026-09-23T11:00:00+00:00" }],
  owner: { email: "bob@example.com", signed_in: true },
};

function row(nodeId: string) {
  return screen.getByText(nodeId).closest("tr") as HTMLElement;
}

describe("PolledRadars", () => {
  beforeEach(() => {
    (api.adminPolledRadars as any).mockResolvedValue({ probation_enabled: true, polling_enabled: true, radars: [ON_PROBATION, GRADUATED] });
    (api.setAdminPolledRadarTrust as any).mockResolvedValue({});
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.clearAllMocks();
  });

  it("lists each radar with its address, epoch and trust", async () => {
    render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    const first = row("bla0a1b2c3d");
    expect(within(first).getByText("radar-one.example.com:3000")).toBeInTheDocument();
    expect(within(first).getByText("On probation")).toBeInTheDocument();
    expect(within(first).getByText("2")).toBeInTheDocument();
    expect(within(row("bla1f2e3d4c")).getByText("Graduated")).toBeInTheDocument();
  });

  it("names who took the last decision, and when", async () => {
    render(<PolledRadars />);
    await screen.findByText("bla1f2e3d4c");
    expect(within(row("bla1f2e3d4c")).getByText(/Graduated by ada@example\.com/)).toBeInTheDocument();
    expect(within(row("bla0a1b2c3d")).getByText("—")).toBeInTheDocument();
  });

  it("graduates the epoch the page is showing, then reloads", async () => {
    render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    fireEvent.click(within(row("bla0a1b2c3d")).getByRole("button", { name: "Graduate bla0a1b2c3d" }));
    await waitFor(() => expect(api.setAdminPolledRadarTrust).toHaveBeenCalledWith("bla0a1b2c3d", "graduated", 2));
    await waitFor(() => expect(api.adminPolledRadars).toHaveBeenCalledTimes(2));
  });

  it("returns a graduated radar to probation at its epoch", async () => {
    render(<PolledRadars />);
    await screen.findByText("bla1f2e3d4c");
    fireEvent.click(within(row("bla1f2e3d4c")).getByRole("button", { name: "Return to probation bla1f2e3d4c" }));
    await waitFor(() => expect(api.setAdminPolledRadarTrust).toHaveBeenCalledWith("bla1f2e3d4c", "probation", 1));
  });

  it("holds the buttons from the decision until the reloaded table lands", async () => {
    render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    let decided!: (answer: unknown) => void;
    (api.setAdminPolledRadarTrust as any).mockReturnValueOnce(new Promise((resolve) => (decided = resolve)));
    let land!: (listing: unknown) => void;
    (api.adminPolledRadars as any).mockReturnValueOnce(new Promise((resolve) => (land = resolve)));
    const graduate = () => within(row("bla0a1b2c3d")).getByRole("button", { name: "Graduate bla0a1b2c3d" });
    fireEvent.click(graduate());
    await waitFor(() => expect(graduate()).toBeDisabled());
    expect(within(row("bla1f2e3d4c")).getByRole("button", { name: "Return to probation bla1f2e3d4c" })).toBeDisabled();
    decided({});
    await waitFor(() => expect(api.adminPolledRadars).toHaveBeenCalledTimes(2));
    expect(graduate()).toBeDisabled();
    land({ probation_enabled: true, polling_enabled: true, radars: [{ ...ON_PROBATION, trust_state: "graduated" }, GRADUATED] });
    expect(await screen.findByRole("button", { name: "Return to probation bla0a1b2c3d" })).toBeEnabled();
  });

  it("sends nothing when the confirmation is declined", async () => {
    (window.confirm as any).mockReturnValue(false);
    render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    fireEvent.click(within(row("bla0a1b2c3d")).getByRole("button", { name: "Graduate bla0a1b2c3d" }));
    expect(window.confirm).toHaveBeenCalled();
    expect(api.setAdminPolledRadarTrust).not.toHaveBeenCalled();
  });

  it("shows the server's refusal and reloads, when the radar has moved on", async () => {
    const detail = "bla0a1b2c3d is at epoch 3 (probation), not epoch 2; reload and look again";
    (api.setAdminPolledRadarTrust as any).mockRejectedValue(new HttpError(409, "Conflict", { detail }));
    render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    fireEvent.click(within(row("bla0a1b2c3d")).getByRole("button", { name: "Graduate bla0a1b2c3d" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    await waitFor(() => expect(api.adminPolledRadars).toHaveBeenCalledTimes(2));
  });

  it("still reports a failure that carries no message", async () => {
    (api.setAdminPolledRadarTrust as any).mockRejectedValue(new Error(""));
    render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    fireEvent.click(within(row("bla0a1b2c3d")).getByRole("button", { name: "Graduate bla0a1b2c3d" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not apply the decision. Try again.");
  });

  it("names each radar's owner, and flags one who has not signed in yet", async () => {
    render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    expect(within(row("bla0a1b2c3d")).getByText("ada@example.com")).toBeInTheDocument();
    expect(within(row("bla0a1b2c3d")).getByText("Not signed in yet")).toBeInTheDocument();
    expect(within(row("bla1f2e3d4c")).getByText("bob@example.com")).toBeInTheDocument();
    expect(within(row("bla1f2e3d4c")).queryByText("Not signed in yet")).toBeNull();
  });

  it("offers to take back only a connection nobody has signed in to", async () => {
    render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    expect(within(row("bla0a1b2c3d")).getByRole("button", { name: "Take back bla0a1b2c3d" })).toBeInTheDocument();
    expect(within(row("bla1f2e3d4c")).queryByRole("button", { name: /Take back/ })).toBeNull();
  });

  it("takes a connection back after asking, then reloads", async () => {
    (api.adminWithdrawPolledRadar as any).mockResolvedValue({ ok: true });
    render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    fireEvent.click(within(row("bla0a1b2c3d")).getByRole("button", { name: "Take back bla0a1b2c3d" }));
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining("ada@example.com"));
    await waitFor(() => expect(api.adminWithdrawPolledRadar).toHaveBeenCalledWith("bla0a1b2c3d"));
    await waitFor(() => expect(api.adminPolledRadars).toHaveBeenCalledTimes(2));
  });

  it("takes nothing back when the confirmation is declined", async () => {
    (window.confirm as any).mockReturnValue(false);
    render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    fireEvent.click(within(row("bla0a1b2c3d")).getByRole("button", { name: "Take back bla0a1b2c3d" }));
    expect(api.adminWithdrawPolledRadar).not.toHaveBeenCalled();
  });

  it("shows why a connection could not be taken back", async () => {
    const detail = "Its owner has signed in, so the radar is theirs to remove.";
    (api.adminWithdrawPolledRadar as any).mockRejectedValue(new HttpError(409, detail, { detail }));
    render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    fireEvent.click(within(row("bla0a1b2c3d")).getByRole("button", { name: "Take back bla0a1b2c3d" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
  });

  it("says whether an administrator or the owner registered a radar", async () => {
    const at = "2026-09-28T10:00:00+00:00";
    (api.adminPolledRadars as any).mockResolvedValue({
      probation_enabled: true,
      polling_enabled: true,
      radars: [
        { ...ON_PROBATION, events: [{ kind: "registered", epoch: 1, actor: "admin:al@example.com", at }] },
        { ...GRADUATED, events: [{ kind: "registered", epoch: 1, actor: "0b8a3c2e-1f4d-4e5a-9b6c-7d8e9f0a1b2c", at }] },
      ],
    });
    render(<PolledRadars />);
    await screen.findByText("bla0a1b2c3d");
    expect(within(row("bla0a1b2c3d")).getByText(/Registered by al@example\.com/)).toBeInTheDocument();
    expect(within(row("bla1f2e3d4c")).getByText(/Registered by its owner/)).toBeInTheDocument();
  });

  it("offers to connect a radar only where this server polls", async () => {
    (api.adminPolledRadars as any).mockResolvedValue({ probation_enabled: true, polling_enabled: true, radars: [] });
    const { unmount } = render(<PolledRadars />);
    expect(await screen.findByRole("heading", { name: "Connect a radar" })).toBeInTheDocument();
    unmount();

    (api.adminPolledRadars as any).mockResolvedValue({ probation_enabled: true, polling_enabled: false, radars: [] });
    render(<PolledRadars />);
    expect(await screen.findByText(/This server does not poll radars/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Connect a radar" })).toBeNull();
  });

  it("reloads the list once a radar is connected", async () => {
    (api.adminPolledRadars as any).mockResolvedValue({ probation_enabled: true, polling_enabled: true, radars: [] });
    (api.adminCheckConnection as any).mockResolvedValue({
      address: "http://radar.example.com:3000",
      rx: { latitude: 10.5, longitude: -30.25, altitude_m: 12, name: null },
      tx: { latitude: 10.75, longitude: -30.5, altitude_m: 300, name: null },
      fc_hz: 204_640_000,
      fs_hz: 2_000_000,
      cpi_s: 0.75,
      fingerprint: "a".repeat(64),
      protected: true,
      account: { email: "ada@example.com", exists: true },
    });
    (api.adminConnectPolledRadar as any).mockResolvedValue({
      node_id: "bla0a1b2c3d",
      epoch: 1,
      trust_state: "probation",
      owner: { email: "ada@example.com", created: false },
    });
    render(<PolledRadars />);
    await screen.findByRole("heading", { name: "Connect a radar" });
    fireEvent.change(screen.getByLabelText("Operator’s email"), { target: { value: "ada@example.com" } });
    fireEvent.change(screen.getByLabelText("Radar address"), { target: { value: "radar.example.com:3000" } });
    fireEvent.click(screen.getByRole("button", { name: "Check radar" }));
    fireEvent.click(await screen.findByRole("button", { name: "Connect to ada@example.com" }));
    await waitFor(() => expect(api.adminPolledRadars).toHaveBeenCalledTimes(2));
  });

  it("says when the probation fence is switched off", async () => {
    (api.adminPolledRadars as any).mockResolvedValue({ probation_enabled: false, polling_enabled: true, radars: [GRADUATED] });
    render(<PolledRadars />);
    expect(await screen.findByText(/Probation is switched off/)).toBeInTheDocument();
  });

  it("says so when no radar is registered", async () => {
    (api.adminPolledRadars as any).mockResolvedValue({ probation_enabled: true, polling_enabled: true, radars: [] });
    render(<PolledRadars />);
    expect(await screen.findByText("No polled radars are registered")).toBeInTheDocument();
  });

  it("shows the failure in place of the table when the list cannot load", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    (api.adminPolledRadars as any).mockRejectedValue(new Error("boom"));
    render(<PolledRadars />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Could not load polled radars: boom");
    expect(screen.queryByRole("table")).toBeNull();
  });
});

describe("NodeManagementPage", () => {
  it("carries the polled radars, which its public-feed node list cannot show on probation", async () => {
    (api.adminPolledRadars as any).mockResolvedValue({ probation_enabled: true, polling_enabled: true, radars: [ON_PROBATION] });
    render(
      <MemoryRouter>
        <NodeManagementPage />
      </MemoryRouter>,
    );
    expect(await screen.findByRole("heading", { name: "Polled radars" })).toBeInTheDocument();
    expect(await screen.findByText("bla0a1b2c3d")).toBeInTheDocument();
  });
});

describe("NodeManagementPage, when its node list is not there", () => {
  function renderPage() {
    (api.adminPolledRadars as any).mockResolvedValue({ probation_enabled: true, polling_enabled: true, radars: [ON_PROBATION] });
    render(
      <MemoryRouter>
        <NodeManagementPage />
      </MemoryRouter>,
    );
  }

  it("still offers the polled radars while the list loads", async () => {
    (api.nodes as any).mockReturnValueOnce(new Promise(() => {}));
    renderPage();
    expect(await screen.findByText("bla0a1b2c3d")).toBeInTheDocument();
  });

  it("still offers the polled radars when the list fails", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    (api.nodes as any).mockRejectedValueOnce(new Error("feed down"));
    renderPage();
    expect(await screen.findByText(/Could not load the node list/)).toBeInTheDocument();
    expect(await screen.findByText("bla0a1b2c3d")).toBeInTheDocument();
  });
});
