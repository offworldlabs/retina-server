import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import EventsPage from "../pages/admin/EventsPage";
import eventLogCss from "../pages/admin/eventLog.css?raw";
import { api } from "../api/client";
import { stubViewport } from "./matchMedia";

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
  vi.unstubAllGlobals();
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

  it("heads each phone card with the time and severity, the message as its body", async () => {
    stubViewport(390);
    const { container } = render(<EventsPage />);
    await screen.findByText("signed in");

    expect(container.querySelector(".card.event-log")).not.toBeNull();
    const cells = container.querySelectorAll("tbody > tr:first-child > td");
    expect(cells[0]).toHaveAttribute("data-hint", "title");
    expect(cells[1]).toHaveClass("event-severity");
    expect(cells[1]).toHaveAttribute("data-label", "");
    expect(cells[3]).toHaveClass("event-message");
    expect(cells[3]).toHaveAttribute("data-label", "");
  });
});

describe("the event log's phone stylesheet", () => {
  const css = eventLogCss.replace(/\/\*[\s\S]*?\*\//g, "").trim();
  const rows = '.event-log .table-wrapper[data-phone="cards"] > table > tbody > tr';

  it("keeps every rule inside the phone media block", () => {
    const open = css.indexOf("{");
    expect(css.slice(0, open).trim()).toBe("@media (max-width: 640px)");
    expect(css.endsWith("}")).toBe(true);
    // Nothing follows the block's closing brace and nothing precedes its start.
    const body = css.slice(open + 1, -1);
    expect(body.match(/[{}]/g)!.join("")).toBe("{}".repeat(5));
    expect(body).not.toContain("@media");
  });

  it("holds the five rules that lay a card out", () => {
    const body = css.slice(css.indexOf("{") + 1, -1).replace(/\s+/g, " ");
    for (const rule of [
      `${rows} { flex-flow: row wrap; align-items: center; column-gap: 8px; }`,
      `${rows} > td { flex: 0 0 100%; }`,
      `${rows} > td[data-hint="title"] { flex: 0 1 auto; }`,
      `${rows} > td.event-severity { order: -1; flex: 0 0 auto; padding: 0 0 6px; }`,
      `${rows} > td.event-message { order: -1; }`,
    ]) {
      expect(body).toContain(rule);
    }
  });
});
