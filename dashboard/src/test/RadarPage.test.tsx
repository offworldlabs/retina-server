import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { HttpError } from "@retina/shared";

import { api } from "../api/client";
import RadarPage from "../pages/user/RadarPage";

const state = vi.hoisted(() => ({ auth: { polledRadarRegistration: true } }));
vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

vi.mock("../api/client", () => ({
  api: { myNodes: vi.fn(), probePolledRadarAddress: vi.fn(), movePolledRadar: vi.fn(), releaseNode: vi.fn() },
}));

const RADAR = {
  node_id: "bla0a1b2c3d",
  node_ref: "nde0123456789",
  name: "nde0123456789",
  status: "active",
  last_heartbeat: null,
  is_synthetic: false,
  rx_lat: 10.5,
  rx_lon: -30.25,
  frequency: 204_640_000,
  location_private: true,
  claimed_with: null,
  polled: { address: "radar.example.com:3000", unprotected: false, liveness: "streaming", trust_state: "probation" },
};

const PROBED = {
  address: "http://moved.example.com:3000",
  rx: { latitude: 10.5, longitude: -30.25, altitude_m: 12, name: "Receiver" },
  tx: { latitude: 10.75, longitude: -30.5, altitude_m: 300, name: "Transmitter" },
  fc_hz: 204_640_000,
  fs_hz: 2_000_000,
  cpi_s: 0.75,
  fingerprint: "a".repeat(64),
  protected: true,
};

function renderPage(ref = "nde0123456789") {
  render(
    <MemoryRouter initialEntries={[`/radars/${ref}`]}>
      <Routes>
        <Route path="/radars/:nodeRef" element={<RadarPage />} />
        <Route path="/onboarding" element={<div>my nodes</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

// The value cell of one row of the radar's own card.
async function reading(label: string) {
  const card = (await screen.findByRole("heading", { name: "Your radar" })).closest(".card") as HTMLElement;
  return within(card).getByText(label).closest("tr")!.children[1];
}

async function check(address = "moved.example.com:3000") {
  fireEvent.change(await screen.findByLabelText("Radar address"), { target: { value: address } });
  fireEvent.click(screen.getByRole("button", { name: "Check radar" }));
  await screen.findByText("What your radar declares");
}

const move = () => screen.getByRole("button", { name: "Move radar here" });

describe("RadarPage", () => {
  beforeEach(() => {
    state.auth = { polledRadarRegistration: true };
    vi.mocked(api.myNodes).mockResolvedValue([RADAR]);
    vi.mocked(api.probePolledRadarAddress).mockResolvedValue(PROBED);
    vi.mocked(api.movePolledRadar).mockResolvedValue({ node_id: RADAR.node_id, epoch: 2, trust_state: "probation" });
    vi.mocked(api.releaseNode).mockResolvedValue({ ok: true });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("shows the owner where the server polls their radar and how it is answering", async () => {
    renderPage();

    expect(await reading("Address")).toHaveTextContent("radar.example.com:3000");
    expect(await reading("Liveness")).toHaveTextContent("Streaming");
    expect(await reading("Protection")).toHaveTextContent("Password-protected");
    expect(await reading("Review")).toHaveTextContent("On probation");
    expect(await reading("Frequency")).toHaveTextContent("204.640 MHz");
  });

  it.each([
    ["pending", "Waiting for its first answer"],
    ["streaming", "Streaming"],
    ["stalled", "Answering, but sending no new frames"],
    ["unreachable", "Not answering"],
  ])("words %s liveness for the owner", async (liveness, words) => {
    vi.mocked(api.myNodes).mockResolvedValue([{ ...RADAR, polled: { ...RADAR.polled, liveness } }]);
    renderPage();

    expect(await reading("Liveness")).toHaveTextContent(words);
  });

  it("shows a liveness it has no words for as the poller wrote it", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ ...RADAR, polled: { ...RADAR.polled, liveness: "resolving" } }]);
    renderPage();

    expect(await reading("Liveness")).toHaveTextContent("resolving");
  });

  it("says when nothing protects the radar, and when an administrator has reviewed it", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([
      { ...RADAR, polled: { ...RADAR.polled, unprotected: true, trust_state: "graduated" } },
    ]);
    renderPage();

    expect(await reading("Protection")).toHaveTextContent("None");
    expect(await reading("Review")).toHaveTextContent("Reviewed");
  });

  it("links to the radar's detections and trust, by its ref", async () => {
    renderPage();

    expect(await screen.findByRole("link", { name: /Detections and trust/ })).toHaveAttribute(
      "href",
      "/nodes/nde0123456789",
    );
  });

  it("finds no radar under a ref the owner holds only as another kind of node", async () => {
    vi.mocked(api.myNodes).mockResolvedValue([{ ...RADAR, polled: null }]);
    renderPage();

    expect(await screen.findByText("Radar not found")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Remove this radar" })).toBeNull();
  });

  it("finds no radar under a ref the owner does not hold", async () => {
    renderPage("nde999999999999");

    expect(await screen.findByText("Radar not found")).toBeInTheDocument();
  });

  it("moves the radar to the address the owner confirmed, by node_id", async () => {
    renderPage();
    await check();

    expect(api.probePolledRadarAddress).toHaveBeenCalledWith("bla0a1b2c3d", "moved.example.com:3000");
    expect(screen.getByText("10.50000, -30.25000, 12 m")).toBeInTheDocument();
    fireEvent.click(move());

    await waitFor(() =>
      expect(api.movePolledRadar).toHaveBeenCalledWith("bla0a1b2c3d", {
        address: "moved.example.com:3000",
        fingerprint: "a".repeat(64),
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("Moved to http://moved.example.com:3000.");
    expect(screen.queryByText("What your radar declares")).toBeNull();
    expect(api.myNodes).toHaveBeenCalledTimes(2);
  });

  it("says before the move that another host or port starts probation", async () => {
    renderPage();
    await check();

    expect(screen.getByText(/Another host or port puts it on probation/)).toBeInTheDocument();
  });

  it("warns when nothing protects the radar at its new address", async () => {
    vi.mocked(api.probePolledRadarAddress).mockResolvedValue({ ...PROBED, protected: false });
    renderPage();
    await check();

    expect(screen.getByText(/Anyone who finds this address can read your radar/)).toBeInTheDocument();
  });

  it("shows the probe's refusal under the address, in its own words", async () => {
    const detail = "Nothing answered at that address.";
    vi.mocked(api.probePolledRadarAddress).mockRejectedValue(new HttpError(422, "", { detail, code: "unreachable" }));
    renderPage();
    fireEvent.change(await screen.findByLabelText("Radar address"), { target: { value: "moved.example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Check radar" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(screen.getByLabelText("Radar address")).toBeInTheDocument();
  });

  it("shows a radar that changed since it was checked, for the owner to confirm again", async () => {
    const changed = { ...PROBED, rx: { ...PROBED.rx, latitude: 10.625 }, fingerprint: "b".repeat(64) };
    vi.mocked(api.movePolledRadar).mockRejectedValueOnce(
      new HttpError(409, "", { detail: "The radar's configuration has changed.", code: "config_changed", probe: changed }),
    );
    renderPage();
    await check();
    fireEvent.click(move());

    expect(await screen.findByText("10.62500, -30.25000, 12 m")).toBeInTheDocument();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("The radar's configuration has changed.");
    expect(alert.closest(".card")).toHaveTextContent("What your radar declares");
    expect(screen.getByText(/check them above before moving it/)).toBeInTheDocument();
    fireEvent.click(move());
    await waitFor(() =>
      expect(api.movePolledRadar).toHaveBeenLastCalledWith(
        "bla0a1b2c3d",
        expect.objectContaining({ fingerprint: "b".repeat(64) }),
      ),
    );
  });

  it("keeps the owner on the page when the move is refused, and says why beside the button", async () => {
    vi.mocked(api.movePolledRadar).mockRejectedValue(
      new HttpError(409, "", { detail: "This radar is already registered.", code: "endpoint_registered" }),
    );
    renderPage();
    await check();
    fireEvent.click(move());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This radar is already registered.");
    expect(alert.closest(".card")).toContainElement(move());
  });

  it("offers no address change where registration is closed, and still offers removal", async () => {
    state.auth = { polledRadarRegistration: false };
    renderPage();

    await reading("Address");
    expect(screen.queryByLabelText("Radar address")).toBeNull();
    expect(screen.getByRole("button", { name: "Remove this radar" })).toBeInTheDocument();
  });

  it("removes only on the confirmation, by node_id, then goes to the owner's nodes", async () => {
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Remove this radar" }));
    expect(api.releaseNode).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Yes, remove it" }));
    await waitFor(() => expect(api.releaseNode).toHaveBeenCalledWith("bla0a1b2c3d"));
    expect(await screen.findByText("my nodes")).toBeInTheDocument();
  });

  it("keeps the radar when removal fails, and says why", async () => {
    vi.mocked(api.releaseNode).mockRejectedValue(new Error("Node not found"));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Remove this radar" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, remove it" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Node not found");
    expect(screen.queryByText("my nodes")).toBeNull();
  });

  it("clears a failed removal's error once the owner keeps the radar", async () => {
    vi.mocked(api.releaseNode).mockRejectedValue(new Error("Node not found"));
    renderPage();
    fireEvent.click(await screen.findByRole("button", { name: "Remove this radar" }));
    fireEvent.click(screen.getByRole("button", { name: "Yes, remove it" }));
    await screen.findByRole("alert");

    fireEvent.click(screen.getByRole("button", { name: "Keep it" }));
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
