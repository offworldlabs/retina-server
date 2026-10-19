/**
 * The archive replay controls: the instant in UTC (with the visitor's local
 * time beside it), play / pause / step and rates, the availability strip,
 * a scrubber across the loaded window, and what the load is doing.
 *
 * Presentational. The replay hook owns the clock, the window and the fetch;
 * this bar only reports gestures, and keeps nothing of its own beyond the
 * text being typed into the time field.
 */

import { useLayoutEffect, useRef, useState, type ChangeEvent, type KeyboardEvent } from "react";

import AvailabilityStrip from "./AvailabilityStrip";
import {
  formatLocalBeside,
  formatUtcClock,
  formatUtcHm,
  formatUtcInput,
  parseUtcInput,
  type AvailabilityHourLike,
} from "./archiveTimeline";
import { ARCHIVE_LAG_MS, AT_LIVE_EDGE_TITLE } from "./replay/useArchiveReplay";
import "./ArchiveBar.css";

type Rate = 1 | 5 | 20;
type LoadStatus = "idle" | "loading" | "ready" | "error";

export interface ArchiveBarProps {
  clockMs: number;
  playing: boolean;
  /** The clock is at the live edge, where play has nothing to show. */
  atLiveEdge?: boolean;
  /** The link or seek that put the clock where it is asked for another instant. */
  clamp?: { by: "link" | "seek"; askedMs: number } | null;
  rate: Rate;
  windowRange: { from: number; to: number } | null;
  status: LoadStatus;
  error: { kind: string; status?: number; message: string } | null;
  truncated: boolean;
  count: number;
  trackCount: number;
  availability: {
    hours: AvailabilityHourLike[];
    range: { from: string; to: string } | null;
    status: LoadStatus;
    error: string | null;
    onViewCenter(centerMs: number): void;
  };
  onTogglePlay(): void;
  onStep(deltaS: number): void;
  onRate(r: Rate): void;
  onSeek(ms: number): void;
  onJumpToNow(): void;
  onRetry(): void;
}

const RATES: Rate[] = [1, 5, 20];
const SECOND_MS = 1000;
const num = new Intl.NumberFormat("en-US");
const plural = (n: number, word: string) => `${num.format(n)} ${word}${n === 1 ? "" : "s"}`;

interface Notice {
  role: "status" | "alert";
  tone: "is-loading" | "is-error" | "is-warning" | "is-empty" | "is-summary";
  text: string;
  retry?: boolean;
}

function errorText(error: ArchiveBarProps["error"]): string {
  const message = error?.message?.trim() ?? "";
  switch (error?.kind) {
    case "invalid":
      return message || "The archive did not accept this window.";
    case "unavailable":
      return `The archive is unavailable right now (${error.status ?? 503}).`;
    case "network":
      return "Couldn't reach the archive.";
    default: {
      const status = error?.status != null ? ` (${error.status})` : "";
      return message ? `Archive request failed${status}: ${message}` : `Archive request failed${status}.`;
    }
  }
}

/** Why the clock is not where the link or seek asked, and where it is instead. */
function clampText(clamp: NonNullable<ArchiveBarProps["clamp"]>, clockMs: number): string {
  const asked = clamp.by === "link" ? "The link's time" : "That time";
  const why = clamp.askedMs > clockMs ? "is past the latest archived moment" : "is before the archive starts";
  // The time alone on the day asked for; the date too when the shown instant is on another.
  const sameDay = formatUtcInput(clamp.askedMs).slice(0, 10) === formatUtcInput(clockMs).slice(0, 10);
  const shown = sameDay ? formatUtcClock(clockMs) : formatUtcInput(clockMs).replace("T", " ");
  return `${asked} ${why}; showing ${shown} UTC.`;
}

/**
 * One notice at a time, by load state: what the visitor can act on wins. Once
 * the window is in, a link or seek that landed elsewhere than it asked says so
 * before anything about the window.
 */
function noticeFor({ status, error, truncated, count, trackCount, windowRange, clamp, clockMs }: ArchiveBarProps): Notice | null {
  switch (status) {
    case "loading":
      return {
        role: "status",
        tone: "is-loading",
        text: windowRange
          ? `Loading ${formatUtcHm(windowRange.from)}–${formatUtcHm(windowRange.to)} UTC…`
          : "Loading the archive…",
      };
    case "error":
      return { role: "alert", tone: "is-error", text: errorText(error), retry: true };
    case "ready":
      if (clamp) return { role: "status", tone: "is-warning", text: clampText(clamp, clockMs) };
      if (truncated) {
        const n = num.format(count);
        return {
          role: "status",
          tone: "is-warning",
          text: windowRange
            ? `More than ${n} solves in this window, so the replay stops at ${formatUtcClock(windowRange.to)} UTC. Play or seek on to load the rest.`
            : `More than ${n} solves in this window; showing the first ${n}.`,
        };
      }
      if (count === 0) return { role: "status", tone: "is-empty", text: "No published solves in this window." };
      return { role: "status", tone: "is-summary", text: `${plural(trackCount, "track")} · ${plural(count, "solve")}` };
    default:
      return null;
  }
}

export default function ArchiveBar(props: ArchiveBarProps) {
  const {
    clockMs,
    playing,
    atLiveEdge = false,
    rate,
    windowRange,
    availability,
    onTogglePlay,
    onStep,
    onRate,
    onSeek,
    onJumpToNow,
    onRetry,
  } = props;

  // The time field follows the clock until it takes focus. From then until it
  // loses it, its value is a draft the ticking clock leaves alone, so a replay
  // playing at 20x cannot rewrite the hour being typed. Only a draft the
  // visitor changed is committed: focusing and tabbing past must not seek
  // back to the instant the field was focused at.
  const [edit, setEdit] = useState<{ draft: string; dirty: boolean } | null>(null);
  const draftMs = edit ? parseUtcInput(edit.draft) : null;
  const invalid = Boolean(edit?.dirty) && draftMs === null;

  /** Seeks to the typed instant, never past the archive's edge (now less
   *  ARCHIVE_LAG_MS: the last two minutes are not archived yet), when it is a
   *  real instant at least a second from the clock. Returns where it landed,
   *  or null. */
  const commit = (value: string): number | null => {
    const ms = parseUtcInput(value);
    if (ms === null) return null;
    const target = Math.min(ms, Date.now() - ARCHIVE_LAG_MS);
    if (Math.abs(target - clockMs) >= SECOND_MS) onSeek(target);
    return target;
  };

  const onFieldChange = (e: ChangeEvent<HTMLInputElement>) => {
    const { value } = e.target;
    // Unfocused, the change came from a native picker: there is no typing to
    // wait for, so it seeks at once.
    if (edit) setEdit({ draft: value, dirty: true });
    else commit(value);
  };

  const onFieldKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (!edit?.dirty) return;
    if (e.key === "Enter") {
      e.preventDefault();
      const landed = commit(edit.draft);
      if (landed !== null) setEdit({ draft: formatUtcInput(landed), dirty: false });
    } else if (e.key === "Escape") {
      // Kept from the map, whose Escape would also drop the selected
      // aircraft; with nothing left to revert, Escape reaches it as usual.
      e.preventDefault();
      e.stopPropagation();
      setEdit({ draft: formatUtcInput(clockMs), dirty: false });
    }
  };

  const onFieldBlur = () => {
    if (edit?.dirty) commit(edit.draft);
    setEdit(null);
  };

  // The local reading describes what the field shows, so it agrees with a
  // time being typed rather than with the clock behind it.
  const shownMs = draftMs ?? clockMs;

  const lo = windowRange ? windowRange.from : 0;
  const hi = windowRange ? Math.max(windowRange.from, windowRange.to - SECOND_MS) : 0;
  const scrubValue = windowRange ? Math.min(Math.max(clockMs, lo), hi) : 0;

  const notice = noticeFor(props);
  // At the live edge play has nothing to show; only Jump to now goes further.
  const playBlocked = !playing && atLiveEdge;

  // The legend floats above this bar, whose height changes as its rows wrap
  // and as a notice comes and goes, so the bar publishes its height on the
  // element it shares with the legend.
  const rootRef = useRef<HTMLElement>(null);
  useLayoutEffect(() => {
    const root = rootRef.current;
    const host = root?.parentElement;
    if (!root || !host || typeof ResizeObserver === "undefined") return;
    const publish = () => host.style.setProperty("--archive-bar-h", `${root.offsetHeight}px`);
    publish();
    const observer = new ResizeObserver(publish);
    observer.observe(root);
    return () => {
      observer.disconnect();
      host.style.removeProperty("--archive-bar-h");
    };
  }, []);

  // The map binds Space to its pause toggle on window, buttons included. Here
  // Space on a focused button presses that button, as it does anywhere else.
  const keepSpaceOnButtons = (e: KeyboardEvent<HTMLElement>) => {
    if (e.key === " " && (e.target as HTMLElement).tagName === "BUTTON") e.stopPropagation();
  };

  return (
    <section ref={rootRef} className="archive-bar" aria-label="Archive replay" onKeyDown={keepSpaceOnButtons}>
      <div className="archive-row archive-row-main">
        <label className="archive-time">
          <span className="archive-time-zone">UTC</span>
          <input
            className="archive-time-input"
            type="datetime-local"
            step="1"
            aria-label="Archive time (UTC)"
            aria-invalid={invalid || undefined}
            value={edit ? edit.draft : formatUtcInput(clockMs)}
            onFocus={() => setEdit({ draft: formatUtcInput(clockMs), dirty: false })}
            onChange={onFieldChange}
            onKeyDown={onFieldKeyDown}
            onBlur={onFieldBlur}
          />
        </label>
        <span className="archive-local">{formatLocalBeside(shownMs)}</span>

        <div className="archive-controls" role="group" aria-label="Playback">
          <button
            type="button"
            className="btn btn-secondary archive-step"
            aria-label="Step back 1 second"
            title="Step back 1 second"
            onClick={() => onStep(-1)}
          >
            −1s
          </button>
          <button
            type="button"
            className="btn btn-secondary archive-play"
            aria-label={playing ? "Pause" : "Play"}
            title={playing ? "Pause" : playBlocked ? AT_LIVE_EDGE_TITLE : "Play"}
            disabled={playBlocked}
            onClick={onTogglePlay}
          >
            {playing ? "⏸" : "▶"}
          </button>
          <button
            type="button"
            className="btn btn-secondary archive-step"
            aria-label="Step forward 1 second"
            title="Step forward 1 second"
            onClick={() => onStep(1)}
          >
            +1s
          </button>
        </div>

        <div className="archive-rate" role="group" aria-label="Playback rate">
          {RATES.map((r) => (
            <button
              key={r}
              type="button"
              className="btn btn-secondary archive-rate-btn"
              aria-label={`Playback rate ${r}×`}
              aria-pressed={rate === r}
              onClick={() => onRate(r)}
            >
              {r}×
            </button>
          ))}
        </div>

        <button
          type="button"
          className="btn btn-secondary archive-jump"
          aria-label="Jump to now"
          title="Leave the archive and return to the live feed"
          onClick={onJumpToNow}
        >
          Jump to now
        </button>
      </div>

      <AvailabilityStrip
        clockMs={clockMs}
        windowRange={windowRange}
        availability={availability}
        onSeek={onSeek}
      />

      <div className="archive-row archive-scrubber-row">
        <span className="archive-window-label is-start">{windowRange ? formatUtcHm(windowRange.from) : "--:--"}</span>
        <input
          className="archive-scrubber"
          type="range"
          aria-label="Replay position"
          aria-valuetext={`${formatUtcClock(windowRange ? scrubValue : clockMs)} UTC`}
          min={lo}
          max={hi}
          step={SECOND_MS}
          value={scrubValue}
          disabled={!windowRange}
          // The window can reach past the archive's edge when the instant is
          // recent; the last two minutes are not archived yet, so a seek
          // stops at now less ARCHIVE_LAG_MS, where the replay engine clamps.
          onChange={(e) => onSeek(Math.min(Number(e.target.value), Date.now() - ARCHIVE_LAG_MS))}
        />
        <span className="archive-window-label is-end">
          {windowRange ? `${formatUtcHm(windowRange.to)} UTC` : "--:--"}
        </span>
      </div>

      {notice && (
        // Keyed by role so an error arrives as a new alert, which is what a
        // screen reader announces, rather than as a status changing its role.
        <div key={notice.role} role={notice.role} className={`archive-notice ${notice.tone}`}>
          <span className="archive-notice-text">{notice.text}</span>
          {notice.retry && (
            <button type="button" className="btn btn-secondary btn-sm archive-retry" onClick={onRetry}>
              Retry
            </button>
          )}
        </div>
      )}
    </section>
  );
}
