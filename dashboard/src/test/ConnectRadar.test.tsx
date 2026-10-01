import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { HttpError } from "@retina/shared";

import { api } from "../api/client";
import { ConnectRadar } from "../pages/admin/ConnectRadar";

vi.mock("../api/client", () => ({
  api: {
    adminCheckConnection: vi.fn(),
    adminConnectPolledRadar: vi.fn(),
  },
}));

const DECLARED = {
  address: "http://radar.example.com:3000",
  rx: { latitude: 10.5, longitude: -30.25, altitude_m: 12, name: "Receiver" },
  tx: { latitude: 10.75, longitude: -30.5, altitude_m: 300, name: "Transmitter" },
  fc_hz: 204_640_000,
  fs_hz: 2_000_000,
  cpi_s: 0.75,
  fingerprint: "a".repeat(64),
  protected: true,
};
const NEW_ADDRESS = { ...DECLARED, account: { email: "ada@example.com", exists: false } };
const KNOWN_ADDRESS = { ...DECLARED, account: { email: "ada@example.com", exists: true } };

function fill(email: string, address = "radar.example.com:3000") {
  fireEvent.change(screen.getByLabelText("Operator’s email"), { target: { value: email } });
  fireEvent.change(screen.getByLabelText("Radar address"), { target: { value: address } });
}

async function checked(email = " Ada@Example.com ") {
  fill(email);
  fireEvent.click(screen.getByRole("button", { name: "Check radar" }));
  await screen.findByText("What the radar declares");
}

describe("ConnectRadar", () => {
  const onConnected = vi.fn();

  beforeEach(() => {
    vi.mocked(api.adminCheckConnection).mockResolvedValue(NEW_ADDRESS);
    vi.mocked(api.adminConnectPolledRadar).mockResolvedValue({
      node_id: "bla0a1b2c3d",
      epoch: 1,
      trust_state: "probation",
      owner: { email: "ada@example.com", created: true },
    });
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("checks only once there is an address to connect it to", () => {
    render(<ConnectRadar onConnected={onConnected} />);
    const check = screen.getByRole("button", { name: "Check radar" });

    fill("", "radar.example.com:3000");
    expect(check).toBeDisabled();
    fill("not-an-address");
    expect(check).toBeDisabled();
    fill("ada@example");
    expect(check).toBeDisabled();
    fill("ada@example.com");
    expect(check).toBeEnabled();
  });

  it("checks the radar for the address as typed", async () => {
    render(<ConnectRadar onConnected={onConnected} />);

    await checked();

    expect(api.adminCheckConnection).toHaveBeenCalledWith("radar.example.com:3000", "Ada@Example.com");
  });

  it("flags an address with no account, for the administrator to check by eye", async () => {
    render(<ConnectRadar onConnected={onConnected} />);

    await checked();

    expect(screen.getByText(/No account for ada@example\.com\. One will be created/)).toBeInTheDocument();
    expect(screen.getByText(/Check the spelling/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create account and connect" })).toBeEnabled();
  });

  it("names the account an address already has", async () => {
    vi.mocked(api.adminCheckConnection).mockResolvedValue(KNOWN_ADDRESS);
    render(<ConnectRadar onConnected={onConnected} />);

    await checked();

    expect(screen.getByText(/goes to the account at ada@example\.com/)).toBeInTheDocument();
    expect(screen.queryByText(/No account for/)).toBeNull();
    expect(screen.getByRole("button", { name: "Connect to ada@example.com" })).toBeInTheDocument();
  });

  it("connects what it showed, private unless chosen otherwise, then starts again", async () => {
    render(<ConnectRadar onConnected={onConnected} />);
    await checked();

    fireEvent.click(screen.getByRole("button", { name: "Create account and connect" }));

    await waitFor(() =>
      expect(api.adminConnectPolledRadar).toHaveBeenCalledWith({
        address: "radar.example.com:3000",
        fingerprint: "a".repeat(64),
        publication: "private",
        email: "Ada@Example.com",
      }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent(
      "Connected bla0a1b2c3d to ada@example.com, on an account made for it.",
    );
    expect(onConnected).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText("Operator’s email")).toHaveValue("");
    expect(screen.getByLabelText("Radar address")).toHaveValue("");
  });

  it("connects it public when the administrator chooses so", async () => {
    render(<ConnectRadar onConnected={onConnected} />);
    await checked();

    fireEvent.click(screen.getByRole("radio", { name: /Public/ }));
    fireEvent.click(screen.getByRole("button", { name: "Create account and connect" }));

    await waitFor(() =>
      expect(api.adminConnectPolledRadar).toHaveBeenCalledWith(expect.objectContaining({ publication: "public" })),
    );
  });

  it("says a radar that answers without a password will be marked unprotected", async () => {
    vi.mocked(api.adminCheckConnection).mockResolvedValue({ ...NEW_ADDRESS, protected: false });
    render(<ConnectRadar onConnected={onConnected} />);

    await checked();

    expect(screen.getByText(/It will be marked unprotected/)).toBeInTheDocument();
  });

  it("shows a refused check beside the address", async () => {
    vi.mocked(api.adminCheckConnection).mockRejectedValue(new Error("This server does not poll radars."));
    render(<ConnectRadar onConnected={onConnected} />);

    fill("ada@example.com");
    fireEvent.click(screen.getByRole("button", { name: "Check radar" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("This server does not poll radars.");
  });

  it("shows what a radar declares now when it changed before the connection", async () => {
    const moved = { ...DECLARED, rx: { ...DECLARED.rx, latitude: 10.625 }, fingerprint: "b".repeat(64) };
    vi.mocked(api.adminConnectPolledRadar).mockRejectedValue(
      new HttpError(409, "The radar's configuration has changed since you checked it.", {
        code: "config_changed",
        probe: moved,
      }),
    );
    render(<ConnectRadar onConnected={onConnected} />);
    await checked();

    fireEvent.click(screen.getByRole("button", { name: "Create account and connect" }));

    expect(await screen.findByText(/10\.62500/)).toBeInTheDocument();
    expect(screen.getByRole("alert").closest(".card")).toHaveTextContent("What the radar declares");
    expect(screen.getByText(/pins have changed: check them above before connecting it/)).toBeInTheDocument();
    expect(onConnected).not.toHaveBeenCalled();
  });

  it("shows a refused connection beside the button", async () => {
    vi.mocked(api.adminConnectPolledRadar).mockRejectedValue(
      new HttpError(409, "This radar is already registered.", { code: "endpoint_registered" }),
    );
    render(<ConnectRadar onConnected={onConnected} />);
    await checked();

    fireEvent.click(screen.getByRole("button", { name: "Create account and connect" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This radar is already registered.");
    expect(alert.closest(".card")).toHaveTextContent("Connect it");
    expect(onConnected).not.toHaveBeenCalled();
  });

  it("holds the operator's address still while the radar is checked", async () => {
    vi.mocked(api.adminCheckConnection).mockReturnValue(new Promise(() => {}));
    render(<ConnectRadar onConnected={onConnected} />);

    fill("ada@example.com");
    fireEvent.click(screen.getByRole("button", { name: "Check radar" }));

    expect(await screen.findByLabelText("Operator’s email")).toBeDisabled();
  });

  it("goes back to the address with what was typed kept", async () => {
    render(<ConnectRadar onConnected={onConnected} />);
    await checked();

    fireEvent.click(screen.getByRole("button", { name: "Change address" }));

    expect(screen.getByLabelText("Operator’s email")).toHaveValue("Ada@Example.com");
    expect(screen.getByLabelText("Radar address")).toHaveValue("radar.example.com:3000");
  });
});
