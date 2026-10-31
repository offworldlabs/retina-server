import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { HttpError } from "@retina/shared";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ArchiveBar, { windowTouchesCold, type ArchiveBarProps } from "./ArchiveBar";
import { parseUtcInput } from "./archiveTimeline";
import { ARCHIVE_EARLIEST_MS, ARCHIVE_LAG_MS, AT_LIVE_EDGE_TITLE, UNREADABLE_MESSAGE, archiveErrorOf } from "./replay/useArchiveReplay";

const at = (iso: string) => Date.parse(iso);
const CLOCK = at("2026-10-18T20:14:05Z");
const NOW = at("2026-10-19T12:00:00Z");
const WINDOW = { from: at("2026-10-18T19:59:05Z"), to: at("2026-10-18T20:29:05Z") };

function props(over: Partial<ArchiveBarProps> = {}): ArchiveBarProps {
  return {
    clockMs: CLOCK,
    playing: false,
    rate: 1,
    windowRange: WINDOW,
    status: "ready",
    error: null,
    truncated: false,
    count: 1234,
    trackCount: 56,
    availability: { hours: [], range: null, status: "ready", error: null, onViewCenter: vi.fn() },
    onTogglePlay: vi.fn(),
    onStep: vi.fn(),
    onRate: vi.fn(),
    onSeek: vi.fn(),
    onJumpToNow: vi.fn(),
    onRetry: vi.fn(),
    ...over,
  };
}

/** Renders the bar and lets the lazily loaded timeline in the strip settle. */
async function setup(over: Partial<ArchiveBarProps> = {}) {
  const p = props(over);
  const view = render(<ArchiveBar {...p} />);
  await screen.findByTestId("edsc-timeline");
  const field = screen.getByLabelText("Archive time (UTC)") as HTMLInputElement;
  return { p, view, field, rerender: (o: Partial<ArchiveBarProps>) => view.rerender(<ArchiveBar {...p} {...o} />) };
}

const focus = (el: HTMLElement) => act(() => el.focus());
const blur = (el: HTMLElement) => act(() => el.blur());
const type = (el: HTMLInputElement, value: string) => fireEvent.change(el, { target: { value } });
const key = (el: HTMLElement, k: string) => fireEvent.keyDown(el, { key: k });

// A fixed wall clock, so the archive edge (now less ARCHIVE_LAG_MS) is decided by the test, not the day it runs.
beforeEach(() => {
  vi.spyOn(Date, "now").mockReturnValue(NOW);
});
afterEach(() => vi.restoreAllMocks());

describe("ArchiveBar controls", () => {
  it("is a labelled region", async () => {
    await setup();
    expect(screen.getByRole("region", { name: "Archive replay" })).toHaveClass("archive-bar");
  });

  it("fire their callbacks", async () => {
    const { p } = await setup();
    fireEvent.click(screen.getByRole("button", { name: "Step back 1 second" }));
    expect(p.onStep).toHaveBeenCalledWith(-1);
    fireEvent.click(screen.getByRole("button", { name: "Step forward 1 second" }));
    expect(p.onStep).toHaveBeenLastCalledWith(1);
    fireEvent.click(screen.getByRole("button", { name: "Play" }));
    expect(p.onTogglePlay).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Playback rate 20×" }));
    expect(p.onRate).toHaveBeenCalledWith(20);
    fireEvent.click(screen.getByRole("button", { name: "Jump to now" }));
    expect(p.onJumpToNow).toHaveBeenCalledTimes(1);
  });

  it("name the play button after what it will do", async () => {
    const { rerender } = await setup({ playing: false });
    expect(screen.getByRole("button", { name: "Play" })).toHaveTextContent("▶");
    rerender({ playing: true });
    expect(screen.getByRole("button", { name: "Pause" })).toHaveTextContent("⏸");
    expect(screen.queryByRole("button", { name: "Play" })).toBeNull();
  });

  it("press the current rate and only it", async () => {
    await setup({ rate: 5 });
    expect(screen.getByRole("button", { name: "Playback rate 1×" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "Playback rate 5×" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Playback rate 20×" })).toHaveAttribute("aria-pressed", "false");
  });

  it("keep Space on a focused button from reaching the map's pause shortcut", async () => {
    await setup();
    const onWindowKey = vi.fn();
    window.addEventListener("keydown", onWindowKey);
    try {
      key(screen.getByRole("button", { name: "Playback rate 5×" }), " ");
      expect(onWindowKey).not.toHaveBeenCalled();
      key(screen.getByRole("button", { name: "Playback rate 5×" }), "s");
      expect(onWindowKey).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener("keydown", onWindowKey);
    }
  });
});

describe("ArchiveBar time field", () => {
  it("shows the clock in UTC, to the second, labelled UTC", async () => {
    const { field } = await setup();
    expect(field).toHaveAttribute("type", "datetime-local");
    expect(field).toHaveAttribute("step", "1");
    expect(field.value).toMatch(/^2026-10-18T20:14:05/);
    expect(parseUtcInput(field.value)).toBe(CLOCK);
    expect(field.closest("label")).toHaveTextContent("UTC");
  });

  it("follows the clock while it is not focused", async () => {
    const { field, rerender } = await setup();
    rerender({ clockMs: CLOCK + 7000 });
    expect(parseUtcInput(field.value)).toBe(CLOCK + 7000);
  });

  it("commits a typed instant on Enter, read as UTC", async () => {
    const { p, field } = await setup();
    focus(field);
    type(field, "2026-10-18T19:30:00");
    expect(p.onSeek).not.toHaveBeenCalled();
    key(field, "Enter");
    expect(p.onSeek).toHaveBeenCalledTimes(1);
    expect(p.onSeek).toHaveBeenCalledWith(at("2026-10-18T19:30:00Z"));
  });

  it("commits a typed instant on blur", async () => {
    const { p, field } = await setup();
    focus(field);
    type(field, "2026-10-17T08:05:09");
    blur(field);
    expect(p.onSeek).toHaveBeenCalledWith(at("2026-10-17T08:05:09Z"));
  });

  it("does not seek twice when Enter is followed by blur", async () => {
    const { p, field, rerender } = await setup();
    focus(field);
    type(field, "2026-10-18T19:30:00");
    key(field, "Enter");
    rerender({ clockMs: at("2026-10-18T19:30:04Z") }); // the replay seeked and played on
    blur(field);
    expect(p.onSeek).toHaveBeenCalledTimes(1);
  });

  it("keeps a draft the clock does not overwrite while focused", async () => {
    const { p, field, rerender } = await setup();
    focus(field);
    rerender({ clockMs: CLOCK + 3000 });
    expect(parseUtcInput(field.value)).toBe(CLOCK); // frozen at focus
    type(field, "2026-10-18T18:00:00");
    rerender({ clockMs: CLOCK + 6000 });
    expect(parseUtcInput(field.value)).toBe(at("2026-10-18T18:00:00Z"));
    expect(p.onSeek).not.toHaveBeenCalled();
  });

  it("does not seek when focused and left without an edit, however far the clock moved", async () => {
    const { p, field, rerender } = await setup();
    focus(field);
    rerender({ clockMs: CLOCK + 60_000 });
    key(field, "Enter");
    blur(field);
    expect(p.onSeek).not.toHaveBeenCalled();
    expect(parseUtcInput(field.value)).toBe(CLOCK + 60_000); // following the clock again
  });

  it("does not seek for a value under a second from the clock", async () => {
    const { p, field } = await setup({ clockMs: CLOCK + 400 });
    focus(field);
    type(field, "2026-10-18T20:14:05");
    key(field, "Enter");
    expect(p.onSeek).not.toHaveBeenCalled();
  });

  it("marks an unparsable draft invalid and does not seek on Enter or blur", async () => {
    const { p, field } = await setup();
    focus(field);
    expect(field).not.toHaveAttribute("aria-invalid");
    type(field, ""); // what a half-typed datetime-local reports
    key(field, "Enter");
    expect(p.onSeek).not.toHaveBeenCalled();
    expect(field).toHaveAttribute("aria-invalid", "true");
    blur(field);
    expect(p.onSeek).not.toHaveBeenCalled();
    // Unfocused, it shows the clock again and is no longer invalid.
    expect(parseUtcInput(field.value)).toBe(CLOCK);
    expect(field).not.toHaveAttribute("aria-invalid");
  });

  it("reverts the draft on Escape without seeking, and keeps that Escape from the map", async () => {
    const { p, field, rerender } = await setup();
    const onWindowKey = vi.fn();
    window.addEventListener("keydown", onWindowKey);
    try {
      focus(field);
      type(field, "2026-10-18T10:00:00");
      rerender({ clockMs: CLOCK + 2000 });
      key(field, "Escape");
      expect(parseUtcInput(field.value)).toBe(CLOCK + 2000);
      expect(onWindowKey).not.toHaveBeenCalled();
      blur(field);
      expect(p.onSeek).not.toHaveBeenCalled();
      // With nothing to revert, Escape is the map's again.
      focus(field);
      key(field, "Escape");
      expect(onWindowKey).toHaveBeenCalledTimes(1);
    } finally {
      window.removeEventListener("keydown", onWindowKey);
    }
  });

  it("clamps a later-than-the-archive-edge instant to now less ARCHIVE_LAG_MS", async () => {
    const { p, field } = await setup();
    focus(field);
    type(field, "2026-10-25T00:00:00");
    key(field, "Enter");
    expect(p.onSeek).toHaveBeenCalledWith(NOW - ARCHIVE_LAG_MS);
  });

  it("commits a change at once when the field is not focused (a native picker)", async () => {
    const { p, field } = await setup();
    expect(document.activeElement).not.toBe(field);
    type(field, "2026-10-16T06:00:00");
    expect(p.onSeek).toHaveBeenCalledWith(at("2026-10-16T06:00:00Z"));
  });
});

describe("ArchiveBar local time", () => {
  it("reads the visitor's wall clock and offset at the replay instant", async () => {
    vi.spyOn(Date.prototype, "getTimezoneOffset").mockReturnValue(-180);
    const { view } = await setup();
    const local = view.container.querySelector(".archive-local");
    expect(local).toHaveTextContent("local 23:14, UTC+3");
  });

  it("follows the instant being typed, so it never contradicts the field", async () => {
    vi.spyOn(Date.prototype, "getTimezoneOffset").mockReturnValue(240);
    const { view, field } = await setup();
    focus(field);
    type(field, "2026-10-18T03:30:00");
    expect(view.container.querySelector(".archive-local")).toHaveTextContent("local 23:30, UTC-4");
  });
});

describe("ArchiveBar scrubber", () => {
  it("spans the loaded window to its last second, with the clock as its value", async () => {
    const { view } = await setup();
    const scrub = screen.getByRole("slider", { name: "Replay position" });
    expect(scrub).toHaveAttribute("min", String(WINDOW.from));
    expect(scrub).toHaveAttribute("max", String(WINDOW.to - 1000));
    expect(scrub).toHaveAttribute("step", "1000");
    expect((scrub as HTMLInputElement).value).toBe(String(CLOCK));
    expect(scrub).toHaveAttribute("aria-valuetext", "20:14:05 UTC");
    expect(scrub).toBeEnabled();
    const labels = [...view.container.querySelectorAll(".archive-window-label")].map((l) => l.textContent);
    expect(labels).toEqual(["19:59", "20:29 UTC"]);
  });

  it("seeks where it is dragged", async () => {
    const { p } = await setup();
    const scrub = screen.getByRole("slider", { name: "Replay position" });
    fireEvent.change(scrub, { target: { value: String(WINDOW.from + 60_000) } });
    expect(p.onSeek).toHaveBeenCalledWith(WINDOW.from + 60_000);
  });

  it("stops a seek at now less ARCHIVE_LAG_MS when the window reaches past it", async () => {
    const recent = { from: NOW - 10 * 60_000, to: NOW + 20 * 60_000 };
    const { p } = await setup({ windowRange: recent, clockMs: NOW - 60_000 });
    const scrub = screen.getByRole("slider", { name: "Replay position" });
    fireEvent.change(scrub, { target: { value: String(NOW + 5 * 60_000) } });
    expect(p.onSeek).toHaveBeenCalledWith(NOW - ARCHIVE_LAG_MS);
  });

  it("holds a clock outside the window at the window's edge", async () => {
    await setup({ clockMs: WINDOW.to + 90_000 });
    const scrub = screen.getByRole("slider", { name: "Replay position" }) as HTMLInputElement;
    expect(scrub.value).toBe(String(WINDOW.to - 1000));
  });

  it("is disabled until a window is loaded", async () => {
    await setup({ windowRange: null, status: "idle" });
    expect(screen.getByRole("slider", { name: "Replay position" })).toBeDisabled();
  });
});

describe("ArchiveBar notices", () => {
  const notice = (role: "status" | "alert") => screen.getByRole(role);

  it("names the window being loaded", async () => {
    await setup({ status: "loading" });
    expect(notice("status")).toHaveTextContent("Loading 19:59–20:29 UTC…");
    expect(notice("status")).toHaveClass("archive-notice", "is-loading");
  });

  it("says a load touching an hour kept only in cold storage is from cold storage", async () => {
    const hour = (iso: string, cold?: boolean) => ({ start_ms: at(iso), count: 5, unclassified: 0, cold });
    const withHours = (...hours: ReturnType<typeof hour>[]) => ({
      availability: { hours, range: null, status: "ready" as const, error: null, onViewCenter: vi.fn() },
    });
    const view = await setup({ status: "loading", ...withHours(hour("2026-10-18T20:00:00Z", true)) });
    expect(notice("status")).toHaveTextContent("Loading 19:59–20:29 UTC from cold storage…");
    expect(notice("status")).toHaveClass("archive-notice", "is-loading");

    // Warm hours only, or none touched: the line as before.
    view.rerender(withHours(hour("2026-10-18T20:00:00Z"), hour("2026-10-18T10:00:00Z", true)));
    expect(notice("status")).toHaveTextContent("Loading 19:59–20:29 UTC…");
    expect(notice("status")).not.toHaveTextContent("cold");

    // A window that ends where the cold hour starts does not touch it.
    const before = { from: at("2026-10-18T19:30:00Z"), to: at("2026-10-18T20:00:00Z") };
    view.rerender({ windowRange: before, ...withHours(hour("2026-10-18T20:00:00Z", true)) });
    expect(notice("status")).toHaveTextContent("Loading 19:30–20:00 UTC…");
    expect(windowTouchesCold(withHours(hour("2026-10-18T19:00:00Z", true)).availability.hours, { from: at("2026-10-18T20:00:00Z"), to: at("2026-10-18T20:30:00Z") })).toBe(false);
  });

  it("counts the minute fetched before the shown window when it says the load is from cold storage", async () => {
    const hour = (iso: string, cold?: boolean) => ({ start_ms: at(iso), count: 5, unclassified: 0, cold });
    const withHours = (...hours: ReturnType<typeof hour>[]) => ({
      availability: { hours, range: null, status: "ready" as const, error: null, onViewCenter: vi.fn() },
    });
    // Shown from 20:00:30, fetched from 19:59:30: only that lead-in overlaps the cold hour before 20:00.
    const view = await setup({
      status: "loading",
      windowRange: { from: at("2026-10-18T20:00:30Z"), to: at("2026-10-18T20:30:30Z") },
      ...withHours(hour("2026-10-18T19:00:00Z", true)),
    });
    expect(notice("status")).toHaveTextContent("Loading 20:00–20:30 UTC from cold storage…");

    // Shown from 20:01:00, fetched from 20:00:00: the cold hour ends exactly where the fetch starts.
    view.rerender({ windowRange: { from: at("2026-10-18T20:01:00Z"), to: at("2026-10-18T20:31:00Z") }, ...withHours(hour("2026-10-18T19:00:00Z", true)) });
    expect(notice("status")).toHaveTextContent("Loading 20:01–20:31 UTC…");
    expect(notice("status")).not.toHaveTextContent("cold");
  });

  it("still says it is loading before the window is known", async () => {
    await setup({ status: "loading", windowRange: null });
    expect(notice("status")).toHaveTextContent("Loading the archive…");
  });

  // Each through the hook's own reading of it, so the line is the one the bar shows for that answer.
  it.each([
    [new HttpError(422, "Unprocessable Entity", { detail: "The window is capped at 60 minutes." }), "The window is capped at 60 minutes."],
    [new HttpError(400, "Bad Request", { detail: "to must be after from" }), "to must be after from"],
    [new HttpError(503, "Service Unavailable", null), "The archive is unavailable right now (503)."],
    [new TypeError("Failed to fetch"), "Couldn't reach the archive."],
    [new HttpError(504, "Gateway Timeout", null), "Couldn't reach the archive."],
    [new DOMException("No response within 30000 ms", "TimeoutError"), "The archive didn't answer in time."],
    [new HttpError(500, "", null), "Archive request failed (500)."],
    [new Error("boom"), "Archive request failed."],
  ])("alerts %s with a Retry", async (err, text) => {
    const { p } = await setup({ status: "error", error: archiveErrorOf(err) });
    const alert = notice("alert");
    expect(alert).toHaveTextContent(text);
    expect(alert).toHaveClass("archive-notice", "is-error");
    expect(screen.queryByRole("status")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(p.onRetry).toHaveBeenCalledTimes(1);
  });

  it("says the replay is waiting out the archive's limit and will try again by itself, with no Retry", async () => {
    await setup({ status: "waiting" });
    expect(notice("status")).toHaveTextContent(/^Too many requests to the archive\. Trying again in a moment…$/);
    expect(notice("status")).toHaveClass("archive-notice", "is-loading");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("shows the message of an unexpected error that has no status", async () => {
    await setup({ status: "error", error: { kind: "unexpected", status: null, message: UNREADABLE_MESSAGE, retryable: true } });
    expect(notice("alert")).toHaveTextContent("The archive sent a window the map could not read.");
  });

  it("says where the replay stops when the window was truncated", async () => {
    await setup({ truncated: true, count: 50_000 });
    expect(notice("status")).toHaveTextContent(
      "More than 50,000 solves in this window, so the replay stops at 20:29:05 UTC. Play or seek on to load the rest.",
    );
    expect(notice("status")).toHaveClass("is-warning");
  });

  it("falls back to the count when a truncated window has no range", async () => {
    await setup({ truncated: true, count: 50_000, windowRange: null });
    expect(notice("status")).toHaveTextContent("More than 50,000 solves in this window; showing the first 50,000.");
    expect(notice("status")).toHaveClass("is-warning");
  });

  it("says when the window is empty", async () => {
    await setup({ count: 0, trackCount: 0 });
    expect(notice("status")).toHaveTextContent("No published solves in this window.");
    expect(notice("status")).toHaveClass("is-empty");
  });

  it("summarises a loaded window quietly", async () => {
    await setup({ count: 12_345, trackCount: 56 });
    expect(notice("status")).toHaveTextContent("56 tracks · 12,345 solves");
    expect(notice("status")).toHaveClass("is-summary");
  });

  it("counts one in the singular", async () => {
    await setup({ count: 1, trackCount: 1 });
    expect(notice("status")).toHaveTextContent("1 track · 1 solve");
  });

  it("shows nothing before a load starts", async () => {
    await setup({ status: "idle", windowRange: null });
    expect(screen.queryByRole("status")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});

describe("ArchiveBar at the live edge (L1)", () => {
  const play = () => screen.getByRole("button", { name: "Play" });

  it("disables Play, with a title that points to Jump to now", async () => {
    const { p } = await setup({ atLiveEdge: true });
    expect(play()).toBeDisabled();
    expect(play()).toHaveAttribute("title", AT_LIVE_EDGE_TITLE);
    expect(play()).toHaveAccessibleDescription(AT_LIVE_EDGE_TITLE);
    fireEvent.click(play());
    expect(p.onTogglePlay).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Jump to now" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "Step back 1 second" })).toBeEnabled();
  });

  it("keeps Pause while playing into the edge, and enables Play off it", async () => {
    const { p, rerender } = await setup({ atLiveEdge: true, playing: true });
    const pause = screen.getByRole("button", { name: "Pause" });
    expect(pause).toBeEnabled();
    expect(pause).toHaveAttribute("title", "Pause");
    fireEvent.click(pause);
    expect(p.onTogglePlay).toHaveBeenCalledTimes(1);
    rerender({ atLiveEdge: false, playing: false });
    expect(play()).toBeEnabled();
    expect(play()).toHaveAttribute("title", "Play");
  });
});

describe("ArchiveBar on a clamped link or seek (L2)", () => {
  const notice = () => screen.getByRole("status");
  const EDGE = NOW - ARCHIVE_LAG_MS;

  it("says a link past the edge shows the edge, by its time in UTC", async () => {
    await setup({ clockMs: EDGE, clamp: { by: "link", askedMs: NOW + 2 * 3_600_000 } });
    expect(notice()).toHaveTextContent(/^The link's time is past the latest archived moment; showing 11:58:00 UTC\.$/);
    expect(notice()).toHaveClass("archive-notice", "is-warning");
  });

  it("says a link before the archive shows its first instant, with the date", async () => {
    await setup({ clockMs: ARCHIVE_EARLIEST_MS, clamp: { by: "link", askedMs: Date.UTC(2015, 0, 1) } });
    expect(notice()).toHaveTextContent(
      /^The link's time is before the archive starts; showing 2020-01-01 00:00:00 UTC\.$/,
    );
  });

  it("says the same of a seek, with the date when the time shown is on another day", async () => {
    await setup({ clockMs: at("2026-10-18T23:59:30Z"), clamp: { by: "seek", askedMs: at("2026-10-19T00:00:10Z") } });
    expect(notice()).toHaveTextContent(/^That time is past the latest archived moment; showing 2026-10-18 23:59:30 UTC\.$/);
  });

  it("outranks what the window says once it is in, but not a load or an error", async () => {
    const past = /^The link's time is past the latest archived moment/;
    const { rerender } = await setup({ clockMs: EDGE, clamp: { by: "link", askedMs: NOW + 3_600_000 } });
    expect(notice()).toHaveTextContent(past);
    rerender({ truncated: true, count: 50_000 });
    expect(notice()).toHaveTextContent(past);
    rerender({ count: 0, trackCount: 0 });
    expect(notice()).toHaveTextContent(past);
    rerender({ status: "loading" });
    expect(notice()).toHaveTextContent("Loading 19:59–20:29 UTC…");
    rerender({ status: "error", error: archiveErrorOf(new TypeError("Failed to fetch")) });
    expect(screen.getByRole("alert")).toHaveTextContent("Couldn't reach the archive.");
    expect(screen.queryByRole("status")).toBeNull();
    // Cleared: the window's own summary again.
    rerender({ clamp: null });
    expect(notice()).toHaveTextContent("56 tracks · 1,234 solves");
  });
});

describe("ArchiveBar detection-arcs notice", () => {
  const ERROR = "Detection arcs could not be loaded for this time.";
  const KEPT = "Some detections from this time are no longer kept on the server, so arcs may be missing.";
  const PENDING = "Arcs for the newest hour appear after the archive's hourly write.";

  it("says the arcs could not be loaded, on a line of its own beside the tracks notice", async () => {
    await setup({ arcs: { status: "error", local: true } });
    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(2);
    expect(statuses[0]).toHaveTextContent("56 tracks · 1,234 solves");
    expect(statuses[1]).toHaveTextContent(new RegExp(`^${ERROR}$`));
    expect(statuses[1]).toHaveClass("archive-notice", "is-warning");
    expect(within(statuses[1]).queryByRole("button")).toBeNull();
  });

  it("says some of the window's detections are no longer kept on the server", async () => {
    await setup({ arcs: { status: "ready", local: false } });
    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(2);
    expect(statuses[1]).toHaveTextContent(new RegExp(`^${KEPT}$`));
  });

  it("says plainly that the newest hour's arcs come with the archive's hourly write", async () => {
    await setup({ arcs: { status: "pending", local: true } });
    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(2);
    expect(statuses[1]).toHaveTextContent(new RegExp(`^${PENDING}$`));
    expect(statuses[1]).toHaveClass("archive-notice");
    expect(statuses[1]).not.toHaveClass("is-warning");
  });

  it("says quietly that the arcs are waiting out the archive's limit, unless the tracks notice already says so", async () => {
    const { rerender } = await setup({ arcs: { status: "waiting", local: true } });
    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(2);
    expect(statuses[1]).toHaveTextContent(/^Too many requests for detection arcs\. Trying again in a moment…$/);
    expect(statuses[1]).toHaveClass("archive-notice", "is-pending");
    expect(screen.queryByRole("alert")).toBeNull();
    rerender({ status: "waiting", arcs: { status: "waiting", local: true } });
    expect(screen.getAllByRole("status").map((s) => s.textContent)).toEqual([
      "Too many requests to the archive. Trying again in a moment…",
    ]);
  });

  it("has no arcs line for ready arcs that are kept, for the layer off, or without the prop", async () => {
    const { rerender } = await setup({ arcs: { status: "ready", local: true } });
    expect(screen.getAllByRole("status")).toHaveLength(1);
    rerender({ arcs: { status: "off", local: false } });
    expect(screen.getAllByRole("status")).toHaveLength(1);
    rerender({ arcs: undefined });
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.queryByText(ERROR)).toBeNull();
    expect(screen.queryByText(KEPT)).toBeNull();
    expect(screen.queryByText(PENDING)).toBeNull();
  });
});

describe("ArchiveBar availability notice", () => {
  const waiting = () => ({ hours: [], range: null, status: "waiting" as const, error: null, onViewCenter: vi.fn() });

  it("says quietly that availability is waiting out the archive's limit, unless the tracks or the arcs already say so", async () => {
    const { rerender } = await setup({ availability: waiting() });
    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(2);
    expect(statuses[0]).toHaveTextContent("56 tracks · 1,234 solves");
    expect(statuses[1]).toHaveTextContent(/^Too many requests for availability\. Trying again in a moment…$/);
    expect(statuses[1]).toHaveClass("archive-notice", "is-pending");
    rerender({ availability: waiting(), arcs: { status: "waiting", local: true } });
    expect(screen.getAllByRole("status").map((s) => s.textContent)).toEqual([
      "56 tracks · 1,234 solves",
      "Too many requests for detection arcs. Trying again in a moment…",
    ]);
    rerender({ availability: waiting(), status: "waiting" });
    expect(screen.getAllByRole("status").map((s) => s.textContent)).toEqual([
      "Too many requests to the archive. Trying again in a moment…",
    ]);
  });

  it("shows only the ADS-B waiting line when availability and the traffic both wait out the archive's limit", async () => {
    await setup({ availability: waiting(), adsb: { status: "waiting" } });
    expect(screen.getAllByRole("status").map((s) => s.textContent)).toEqual([
      "56 tracks · 1,234 solves",
      "Too many requests for ADS-B traffic. Trying again in a moment…",
    ]);
  });
});

describe("ArchiveBar ADS-B traffic notice", () => {
  const FAILED = "ADS-B traffic could not be loaded for this time.";

  it("says the traffic could not be loaded, on a line of its own beside the others", async () => {
    await setup({ adsb: { status: "error" }, arcs: { status: "error", local: true } });
    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(3);
    expect(statuses[0]).toHaveTextContent("56 tracks · 1,234 solves");
    expect(statuses[2]).toHaveTextContent(new RegExp(`^${FAILED}$`));
    expect(statuses[2]).toHaveClass("archive-notice", "is-warning");
  });

  it("says quietly that the traffic is waiting out the archive's limit, unless the tracks or the arcs already say so", async () => {
    const { rerender } = await setup({ adsb: { status: "waiting" } });
    const statuses = screen.getAllByRole("status");
    expect(statuses).toHaveLength(2);
    expect(statuses[0]).toHaveTextContent("56 tracks · 1,234 solves");
    expect(statuses[1]).toHaveTextContent(/^Too many requests for ADS-B traffic\. Trying again in a moment…$/);
    expect(statuses[1]).toHaveClass("archive-notice", "is-pending");
    rerender({ adsb: { status: "waiting" }, arcs: { status: "waiting", local: true } });
    expect(screen.getAllByRole("status").map((s) => s.textContent)).toEqual([
      "56 tracks · 1,234 solves",
      "Too many requests for detection arcs. Trying again in a moment…",
    ]);
    rerender({ adsb: { status: "waiting" }, status: "waiting" });
    expect(screen.getAllByRole("status").map((s) => s.textContent)).toEqual([
      "Too many requests to the archive. Trying again in a moment…",
    ]);
  });

  it("has no line while the traffic loads, once it is ready, with the layer off, or without the prop", async () => {
    const { rerender } = await setup({ adsb: { status: "loading" } });
    expect(screen.getAllByRole("status")).toHaveLength(1);
    rerender({ adsb: { status: "ready" } });
    rerender({ adsb: { status: "off" } });
    rerender({ adsb: undefined });
    expect(screen.getAllByRole("status")).toHaveLength(1);
    expect(screen.queryByText(FAILED)).toBeNull();
  });
});

describe("ArchiveBar height for the legend", () => {
  let callbacks: Array<() => void>;
  let height: number;

  beforeEach(() => {
    callbacks = [];
    height = 183;
    class FakeObserver {
      constructor(cb: () => void) {
        callbacks.push(cb);
      }
      observe() {}
      disconnect() {}
    }
    vi.stubGlobal("ResizeObserver", FakeObserver);
    vi.spyOn(HTMLElement.prototype, "offsetHeight", "get").mockImplementation(() => height);
  });
  afterEach(() => vi.unstubAllGlobals());

  it("sets --archive-bar-h on the parent, follows resizes and removes it on unmount", async () => {
    const { view } = await setup();
    const parent = screen.getByRole("region", { name: "Archive replay" }).parentElement as HTMLElement;
    expect(parent.style.getPropertyValue("--archive-bar-h")).toBe("183px");

    height = 241;
    act(() => callbacks.forEach((cb) => cb()));
    expect(parent.style.getPropertyValue("--archive-bar-h")).toBe("241px");

    view.unmount();
    expect(parent.style.getPropertyValue("--archive-bar-h")).toBe("");
  });

  it("renders without a ResizeObserver", async () => {
    vi.stubGlobal("ResizeObserver", undefined);
    await setup();
    const parent = screen.getByRole("region", { name: "Archive replay" }).parentElement as HTMLElement;
    expect(parent.style.getPropertyValue("--archive-bar-h")).toBe("");
  });
});
