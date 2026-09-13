import { describe, it, expect, vi, beforeEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { ComponentProps } from "react";

import { LocationPrivacyControl } from "../components/LocationPrivacyControl";
import { api } from "../api/client";

// The control takes its writes as props so the same component can drive the
// owner and the admin routes; the tests wire it to the owner ones and assert
// through the mocked client, which is where the HTTP method actually lives.
vi.mock("../api/client", () => ({
  api: {
    myNodeLocationPrivacy: vi.fn(),
    clearMyNodeLocationPrivacy: vi.fn(),
  },
}));

const NODE = "node-1";

function renderControl(
  props: Partial<ComponentProps<typeof LocationPrivacyControl>> = {},
) {
  return render(
    <LocationPrivacyControl
      nodeId={NODE}
      isPrivate={false}
      source="registration"
      uncertaintyKm={2.5}
      onSave={(next) => api.myNodeLocationPrivacy(NODE, next)}
      onReset={() => api.clearMyNodeLocationPrivacy(NODE)}
      {...props}
    />,
  );
}

const publicRadio = () => screen.getByRole("radio", { name: /^Public/ }) as HTMLInputElement;
const privateRadio = () => screen.getByRole("radio", { name: /^Private/ }) as HTMLInputElement;

describe("LocationPrivacyControl", () => {
  beforeEach(() => {
    vi.mocked(api.myNodeLocationPrivacy).mockReset();
    vi.mocked(api.clearMyNodeLocationPrivacy).mockReset();
  });

  it("renders the public state with the quoted displacement", () => {
    renderControl();
    expect(publicRadio().checked).toBe(true);
    expect(privateRadio().checked).toBe(false);
    expect(
      screen.getByText("Shown on the map at an approximate position, displaced up to 2.5 km"),
    ).toBeInTheDocument();
    expect(screen.getByText("Set at onboarding")).toBeInTheDocument();
    // No override in play, so nothing to reset to.
    expect(screen.queryByText("Use the onboarding choice")).not.toBeInTheDocument();
  });

  it("renders the private state", () => {
    renderControl({ isPrivate: true, source: "override" });
    expect(privateRadio().checked).toBe(true);
    expect(publicRadio().checked).toBe(false);
    expect(
      screen.getByText(/Hidden from the public map: no marker, no coverage/),
    ).toBeInTheDocument();
    expect(screen.getByText("Use the onboarding choice")).toBeInTheDocument();
  });

  it("names the fuzz instead of a distance when none is known", () => {
    renderControl({ uncertaintyKm: null });
    expect(
      screen.getByText(
        "Shown on the map at an approximate position, displaced by the network's location fuzz",
      ),
    ).toBeInTheDocument();
  });

  it("PUTs the new choice and shows it immediately", async () => {
    vi.mocked(api.myNodeLocationPrivacy).mockResolvedValue({
      node_id: NODE,
      location_private: true,
      location_privacy_source: "override",
    });
    const onApplied = vi.fn();
    renderControl({ onApplied });

    fireEvent.click(privateRadio());

    // Optimistic: the radio moves before the request settles.
    expect(privateRadio().checked).toBe(true);
    expect(api.myNodeLocationPrivacy).toHaveBeenCalledWith(NODE, true);

    await waitFor(() => expect(onApplied).toHaveBeenCalledTimes(1));
    expect(privateRadio().checked).toBe(true);
    expect(screen.getByText("Set here")).toBeInTheDocument();
    expect(screen.getByText("Use the onboarding choice")).toBeInTheDocument();
  });

  it("DELETEs the override when reset, and adopts the returned source", async () => {
    vi.mocked(api.clearMyNodeLocationPrivacy).mockResolvedValue({
      node_id: NODE,
      location_private: false,
      location_privacy_source: "registration",
    });
    renderControl({ isPrivate: true, source: "override" });

    fireEvent.click(screen.getByText("Use the onboarding choice"));
    expect(api.clearMyNodeLocationPrivacy).toHaveBeenCalledWith(NODE);

    await waitFor(() => expect(screen.getByText("Set at onboarding")).toBeInTheDocument());
    expect(publicRadio().checked).toBe(true);
    expect(screen.queryByText("Use the onboarding choice")).not.toBeInTheDocument();
  });

  it("rolls back to the server's state when the write fails", async () => {
    vi.mocked(api.myNodeLocationPrivacy).mockRejectedValue(new Error("500 Server Error"));
    const onApplied = vi.fn();
    renderControl({ onApplied });

    fireEvent.click(privateRadio());
    expect(privateRadio().checked).toBe(true);

    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
    expect(publicRadio().checked).toBe(true);
    expect(privateRadio().checked).toBe(false);
    expect(screen.getByText("Set at onboarding")).toBeInTheDocument();
    expect(onApplied).not.toHaveBeenCalled();
  });

  it("disables both options while a write is in flight", async () => {
    let resolve!: (v: any) => void;
    vi.mocked(api.myNodeLocationPrivacy).mockReturnValue(
      new Promise((r) => { resolve = r; }),
    );
    renderControl();

    fireEvent.click(privateRadio());
    expect(privateRadio()).toBeDisabled();
    expect(publicRadio()).toBeDisabled();
    expect(screen.getByText("Saving…")).toBeInTheDocument();

    resolve({ node_id: NODE, location_private: true, location_privacy_source: "override" });
    await waitFor(() => expect(privateRadio()).not.toBeDisabled());
  });
});
