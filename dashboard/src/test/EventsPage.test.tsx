import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import EventsPage from "../pages/admin/EventsPage";
import { api } from "../api/client";

vi.mock("../api/client", () => ({
  api: { adminEvents: vi.fn() },
}));

const EVENTS = [
  { ts: 1, severity: "info", category: "user", message: "signed in" },
  { ts: 2, severity: "info", category: "node", message: "ret-a connected" },
  { ts: 3, severity: "info", category: "config", message: "config reloaded" },
  { ts: 4, severity: "warning", category: "user", message: "role changed" },
  { ts: 5, severity: "error", category: "mirror", message: "mirror failed" },
];

beforeEach(() => {
  vi.mocked(api.adminEvents).mockReset().mockResolvedValue(EVENTS);
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("EventsPage", () => {
  it("narrows the log to alerts and back", async () => {
    render(<EventsPage />);
    await screen.findByText("signed in");
    const toggle = screen.getByRole("button", { name: "Alerts only" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");

    fireEvent.click(toggle);

    expect(toggle).toHaveAttribute("aria-pressed", "true");
    // Info about a user is the one kind of event that is not an alert.
    expect(screen.queryByText("signed in")).toBeNull();
    for (const kept of ["ret-a connected", "config reloaded", "role changed", "mirror failed"]) {
      expect(screen.getByText(kept)).toBeInTheDocument();
    }

    fireEvent.click(toggle);
    expect(screen.getByText("signed in")).toBeInTheDocument();
  });

  it("refreshes the log on its own", async () => {
    vi.useFakeTimers();
    render(<EventsPage />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(api.adminEvents).toHaveBeenCalledTimes(1);

    vi.mocked(api.adminEvents).mockResolvedValue([
      { ts: 6, severity: "warning", category: "node", message: "ret-b went quiet" },
      ...EVENTS,
    ]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });

    expect(api.adminEvents).toHaveBeenCalledTimes(2);
    expect(screen.getByText("ret-b went quiet")).toBeInTheDocument();
  });

  it("holds an older page still while it is being read", async () => {
    vi.useFakeTimers();
    const many = Array.from({ length: 30 }, (_, i) => ({
      ts: 100 - i,
      severity: "info",
      category: "node",
      message: `event ${i}`,
    }));
    vi.mocked(api.adminEvents).mockResolvedValue(many);
    render(<EventsPage />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });

    // Five more arrive at the top just as the reader turns the page.
    vi.mocked(api.adminEvents).mockResolvedValue([
      ...Array.from({ length: 5 }, (_, i) => ({ ts: 200 - i, severity: "info", category: "node", message: `new ${i}` })),
      ...many,
    ]);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(60_000);
    });

    // Page two still starts where page one ended.
    expect(screen.getByText("event 25")).toBeInTheDocument();
    expect(screen.queryByText("event 20")).toBeNull();
    expect(screen.getByText(/refresh paused/)).toBeInTheDocument();
  });
});
