import { act, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { api } from "../api/client";
import DetectionsPage from "../pages/user/DetectionsPage";
import { stubViewport } from "./matchMedia";

vi.mock("../api/client", () => ({
  api: { nodes: vi.fn(), aircraft: vi.fn(), myNodes: vi.fn(), myAircraft: vi.fn() },
}));
vi.mock("../context/AuthContext", () => ({ useAuth: () => ({ user: null, loading: false }) }));

const HILL = "ndehill00000001";

async function show() {
  vi.mocked(api.nodes).mockResolvedValue({ nodes: { [HILL]: { name: "Hilltop" } } });
  vi.mocked(api.aircraft).mockResolvedValue({
    aircraft: [{ hex: "aa0001", flight: "TST1", lat: 51.5, lon: -0.1, alt_baro: 32000, gs: 420, track: 90, seen: 1, node_ref: HILL }],
  });
  render(
    <MemoryRouter>
      <DetectionsPage />
    </MemoryRouter>,
  );
  for (let i = 0; i < 5; i++) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
  }
  return screen.getByText("aa0001").closest("tr")!;
}

const cell = (row: HTMLElement, label: string) => row.querySelector<HTMLElement>(`td[data-label="${label}"]`)!;

describe("DetectionsPage as cards", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.useFakeTimers();
    vi.spyOn(console, "error").mockImplementation(() => {});
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it("heads a phone's card with the hex, keeps Seen by a plain line and puts the readings behind More", async () => {
    stubViewport(390);
    const row = await show();

    expect(row.querySelector("td")).toHaveAttribute("data-hint", "title");
    expect(cell(row, "Seen by")).not.toHaveAttribute("data-hint");
    expect(cell(row, "Lat")).toHaveAttribute("data-hint", "secondary");

    expect(row).not.toHaveAttribute("data-card-open");
    fireEvent.click(row.querySelector("button")!);
    expect(row).toHaveAttribute("data-card-open");
  });

  it("hides no column on a tablet", async () => {
    stubViewport(800);
    const row = await show();

    expect(row.querySelectorAll('[data-hint="hidden"]')).toHaveLength(0);
    expect(row.querySelectorAll("td")).toHaveLength(9);
  });
});
