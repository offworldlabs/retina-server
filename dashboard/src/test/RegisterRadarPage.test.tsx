import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { HttpError } from "@retina/shared";

import { api } from "../api/client";
import RegisterRadarPage from "../pages/user/RegisterRadarPage";

const state = vi.hoisted(() => ({ auth: { polledRadarRegistration: true } }));
vi.mock("../context/AuthContext", () => ({ useAuth: () => state.auth }));

vi.mock("../api/client", () => ({
  api: { probePolledRadar: vi.fn(), registerPolledRadar: vi.fn() },
}));

const PROBED = {
  address: "http://radar.example.com:3000",
  rx: { latitude: 10.5, longitude: -30.25, altitude_m: 12, name: "Receiver" },
  tx: { latitude: 10.75, longitude: -30.5, altitude_m: 300, name: "Transmitter" },
  fc_hz: 204_640_000,
  fs_hz: 2_000_000,
  cpi_s: 0.75,
  fingerprint: "a".repeat(64),
  protected: false,
};

function renderPage() {
  render(
    <MemoryRouter initialEntries={["/radars/new"]}>
      <Routes>
        <Route path="/radars/new" element={<RegisterRadarPage />} />
        <Route path="/onboarding" element={<div>my nodes</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

async function check(address = "radar.example.com:3000") {
  fireEvent.change(screen.getByLabelText("Radar address"), { target: { value: address } });
  fireEvent.click(screen.getByRole("button", { name: "Check radar" }));
  await screen.findByText("What your radar declares");
}

const register = () => screen.getByRole("button", { name: "Register radar" });

describe("RegisterRadarPage", () => {
  beforeEach(() => {
    state.auth = { polledRadarRegistration: true };
    vi.mocked(api.probePolledRadar).mockResolvedValue(PROBED);
    vi.mocked(api.registerPolledRadar).mockResolvedValue({
      node_id: "bla0a1b2c3d",
      epoch: 1,
      trust_state: "probation",
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("says so, and offers no form, where registration is not open", () => {
    state.auth = { polledRadarRegistration: false };
    renderPage();

    expect(screen.getByText(/not open yet/)).toBeInTheDocument();
    expect(screen.queryByLabelText("Radar address")).toBeNull();
  });

  it("shows what the radar declares, read-only, once checked", async () => {
    renderPage();
    await check();

    expect(api.probePolledRadar).toHaveBeenCalledWith("radar.example.com:3000");
    expect(screen.getByText("10.50000, -30.25000, 12 m")).toBeInTheDocument();
    expect(screen.getByText("10.75000, -30.50000, 300 m")).toBeInTheDocument();
    expect(screen.getByText("204.640 MHz")).toBeInTheDocument();
    expect(screen.getByText(/km$/)).toBeInTheDocument();
    expect(screen.getByText(/move the pin in blah2.s own config/i)).toBeInTheDocument();
    expect(screen.queryByRole("spinbutton")).toBeNull();
  });

  it("warns when nothing protects the radar, and not when something does", async () => {
    renderPage();
    await check();
    expect(screen.getByText(/Anyone who finds this address can read your radar/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Change address" }));
    vi.mocked(api.probePolledRadar).mockResolvedValue({ ...PROBED, protected: true });
    await check("https://owner:pw@radar.example.com");
    expect(screen.queryByText(/Anyone who finds this address can read your radar/)).toBeNull();
  });

  it("shows the probe's refusal under the address, in its own words", async () => {
    const detail = "This radar still has blah2's example configuration.";
    vi.mocked(api.probePolledRadar).mockRejectedValue(new HttpError(422, "", { detail, code: "example_config" }));
    renderPage();
    fireEvent.change(screen.getByLabelText("Radar address"), { target: { value: "radar.example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Check radar" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(detail);
    expect(screen.getByLabelText("Radar address")).toBeInTheDocument();
  });

  it("holds registration until the owner chooses public or private", async () => {
    renderPage();
    await check();

    expect(screen.getByRole("radio", { name: /Public/ })).not.toBeChecked();
    expect(screen.getByRole("radio", { name: /Private/ })).not.toBeChecked();
    expect(register()).toBeDisabled();
    fireEvent.click(screen.getByRole("radio", { name: /Private/ }));
    expect(register()).toBeEnabled();
  });

  it("registers what the owner confirmed, then goes to their nodes", async () => {
    renderPage();
    await check();
    fireEvent.click(screen.getByRole("radio", { name: /Private/ }));
    fireEvent.click(register());

    await waitFor(() =>
      expect(api.registerPolledRadar).toHaveBeenCalledWith({
        address: "radar.example.com:3000",
        fingerprint: "a".repeat(64),
        publication: "private",
      }),
    );
    expect(await screen.findByText("my nodes")).toBeInTheDocument();
  });

  it("shows a radar that changed since it was checked, for the owner to confirm again", async () => {
    const moved = { ...PROBED, rx: { ...PROBED.rx, latitude: 10.625 }, fingerprint: "b".repeat(64) };
    vi.mocked(api.registerPolledRadar).mockRejectedValueOnce(
      new HttpError(409, "", { detail: "The radar's configuration has changed.", code: "config_changed", probe: moved }),
    );
    renderPage();
    await check();
    fireEvent.click(screen.getByRole("radio", { name: /Public/ }));
    fireEvent.click(register());

    expect(await screen.findByText("10.62500, -30.25000, 12 m")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("The radar's configuration has changed.");
    expect(screen.getByText(/check them above before registering/)).toBeInTheDocument();
    fireEvent.click(register());
    await waitFor(() =>
      expect(api.registerPolledRadar).toHaveBeenLastCalledWith(expect.objectContaining({ fingerprint: "b".repeat(64) })),
    );
  });

  it("keeps the owner on the page when registration is refused", async () => {
    vi.mocked(api.registerPolledRadar).mockRejectedValue(
      new HttpError(409, "", { detail: "This radar is already registered.", code: "endpoint_registered" }),
    );
    renderPage();
    await check();
    fireEvent.click(screen.getByRole("radio", { name: /Public/ }));
    fireEvent.click(register());

    expect(await screen.findByRole("alert")).toHaveTextContent("This radar is already registered.");
    expect(screen.getByText("What your radar declares")).toBeInTheDocument();
  });

  it("holds the check while the server is checking", async () => {
    let answer!: (probe: unknown) => void;
    vi.mocked(api.probePolledRadar).mockReturnValueOnce(new Promise((resolve) => (answer = resolve)));
    renderPage();
    fireEvent.change(screen.getByLabelText("Radar address"), { target: { value: "radar.example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Check radar" }));

    expect(await screen.findByText(/up to 15 seconds/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Checking…" })).toBeDisabled();
    answer(PROBED);
    await screen.findByText("What your radar declares");
  });

  it("shows a refused re-check beside the pins, not by the button", async () => {
    renderPage();
    await check();
    vi.mocked(api.probePolledRadar).mockRejectedValueOnce(
      new HttpError(422, "", { detail: "The radar's detection timestamp did not advance.", code: "stalled" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Check again" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("did not advance");
    expect(alert.closest(".card")).toHaveTextContent("What your radar declares");
  });
});
