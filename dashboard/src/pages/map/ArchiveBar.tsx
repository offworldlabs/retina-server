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
  HOUR_MS,
  formatLocalBeside,
  formatUtcClock,
  formatUtcHm,
  formatUtcInput,
  parseUtcInput,
} from "./archiveTimeline";
import { ARCHIVE_LAG_MS, AT_LIVE_EDGE_TITLE, UNREADABLE_MESSAGE } from "./replay/useArchiveReplay";
import type {
  ArchiveAdsbState,
  ArchiveArcsState,
  ArchiveClamp,
  ArchiveError,
  LoadStatus,
  ReplayRate,
} from "./replay/useArchiveReplay";
import type { AvailabilityHour } from "./replay/types";
import type { TrackAvailability } from "./replay/useTrackAvailability";
import "./ArchiveBar.css";

export interface ArchiveBarProps {
  clockMs: number;
  playing: boolean;
  /** The clock is at the live edge, where play has nothing to show. */
  atLiveEdge?: boolean;
  /** The link or seek that put the clock where it is asked for another instant. */
  clamp?: ArchiveClamp | null;
  rate: ReplayRate;
  windowRange: { from: number; to: number } | null;
  status: LoadStatus;
  error: ArchiveError | null;
  truncated: boolean;
  count: number;
  trackCount: number;
  availability: TrackAvailability;
  /** The detection-arcs layer's state; its notice shows only when it has something to say. */
  arcs?: ArchiveArcsState;
  /** The ADS-B traffic layer's state; its notice shows only when it failed or waits out a 429. */
  adsb?: ArchiveAdsbState;
  onTogglePlay(): void;
  onStep(deltaS: number): void;
  onRate(r: ReplayRate): void;
  onSeek(ms: number): void;
  onJumpToNow(): void;
  onRetry(): void;
}

const RATES: ReplayRate[] = [1, 5, 20];
const SECOND_MS = 1000;
const num = new Intl.NumberFormat("en-US");
const plural = (n: number, word: string) => `${num.format(n)} ${word}${n === 1 ? "" : "s"}`;

interface Notice {
  role: "status" | "alert";
  tone: "is-loading" | "is-error" | "is-warning" | "is-empty" | "is-summary";
  text: string;
  retry?: boolean;
}

/** A quiet line of its own under the tracks notice. */
interface SideNotice {
  text: string;
  tone: "is-warning" | "is-pending";
}

// What the bar says while a 429 is waited out: in hand, nothing for the
// visitor to do, so no alert and no Retry.
const TRACKS_WAITING = "Too many requests to the archive. Trying again in a moment…";
const ARCS_WAITING = "Too many requests for detection arcs. Trying again in a moment…";
const AVAILABILITY_WAITING = "Too many requests for availability. Trying again in a moment…";
const ADSB_WAITING = "Too many requests for ADS-B traffic. Trying again in a moment…";

function errorText(error: ArchiveError | null): string {
  if (!error) return "Archive request failed.";
  const { kind, status } = error;
  switch (kind) {
    case "invalid":
    case "refused":
      // The route's own reason where it gave one, or that the stretch is too
      // dense to show; otherwise the app's line for the status.
      return error.message.trim() || "The archive did not accept this window.";
    case "unavailable":
      return `The archive is unavailable right now (${status ?? 503}).`;
    case "unreachable":
      return "Couldn't reach the archive.";
    case "timeout":
      return "The archive didn't answer in time.";
    case "unexpected":
      // A window the server sent and the map could not read says so; any other unexpected failure is the generic line.
      if (status === null && error.message === UNREADABLE_MESSAGE) return error.message;
      return status === null ? "Archive request failed." : `Archive request failed (${status}).`;
    case "server":
    case "unauthorized":
      return status === null ? "Archive request failed." : `Archive request failed (${status}).`;
    default: {
      const unhandled: never = kind;
      return unhandled;
    }
  }
}

/** Why the clock is not where the link or seek asked, and where it is instead. */
function clampText(clamp: ArchiveClamp, clockMs: number): string {
  const asked = clamp.by === "link" ? "The link's time" : "That time";
  const why = clamp.askedMs > clockMs ? "is past the latest archived moment" : "is before the archive starts";
  // The time alone on the day asked for; the date too when the shown instant is on another.
  const sameDay = formatUtcInput(clamp.askedMs).slice(0, 10) === formatUtcInput(clockMs).slice(0, 10);
  const shown = sameDay ? formatUtcClock(clockMs) : formatUtcInput(clockMs).replace("T", " ");
  return `${asked} ${why}; showing ${shown} UTC.`;
}

/** The window overlaps an hour whose rows live only in cold storage. An hour that ends where the window starts, or starts where it ends, is not touched. */
export function windowTouchesCold(
  hours: readonly AvailabilityHour[] | undefined,
  windowRange: { from: number; to: number } | null,
): boolean {
  if (!windowRange || !hours) return false;
  return hours.some(
    (h) => h.cold === true && h.start_ms < windowRange.to && h.start_ms + HOUR_MS > windowRange.from,
  );
}

/**
 * One notice at a time, by load state: what the visitor can act on wins. Once
 * the window is in, a link or seek that landed elsewhere than it asked says so
 * before anything about the window. A 429 is never an error here (ArchiveError
 * leaves it out): the replay waits it out and says so in a status.
 */
function noticeFor({ status, error, truncated, count, trackCount, windowRange, clamp, clockMs, availability }: ArchiveBarProps): Notice | null {
  switch (status) {
    case "loading": {
      const cold = windowTouchesCold(availability.hours, windowRange) ? " from cold storage" : "";
      return {
        role: "status",
        tone: "is-loading",
        text: windowRange
          ? `Loading ${formatUtcHm(windowRange.from)}–${formatUtcHm(windowRange.to)} UTC${cold}…`
          : "Loading the archive…",
      };
    }
    case "waiting":
      return { role: "status", tone: "is-loading", text: TRACKS_WAITING };
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

/**
 * The arcs layer's own line, apart from the tracks notice: what the visitor
 * should know about the arcs. Its wait on a 429 goes unsaid while the tracks
 * notice already says the archive is being waited out.
 */
function arcsNoticeFor(arcs: ArchiveArcsState | undefined, tracksWaiting: boolean): SideNotice | null {
  if (!arcs) return null;
  if (arcs.status === "error") return { text: "Detection arcs could not be loaded for this time.", tone: "is-warning" };
  if (arcs.status === "waiting") return tracksWaiting ? null : { text: ARCS_WAITING, tone: "is-pending" };
  if (arcs.status !== "off" && !arcs.local) {
    return { text: "Some detections from this time are no longer kept on the server, so arcs may be missing.", tone: "is-warning" };
  }
  // The usual state of the newest hour, nothing gone wrong: said plainly, as the loading line is.
  if (arcs.status === "pending") return { text: "Arcs for the newest hour appear after the archive's hourly write.", tone: "is-pending" };
  return null;
}

/**
 * The ADS-B layer's own line, on a failure (the tracks replay on without it)
 * or while it waits out a 429, which goes unsaid while the tracks or the arcs
 * already say the archive is being waited out: all of them share one budget.
 */
function adsbNoticeFor(
  adsb: ArchiveAdsbState | undefined,
  tracksWaiting: boolean,
  arcs: ArchiveArcsState | undefined,
): SideNotice | null {
  if (adsb?.status === "error") return { text: "ADS-B traffic could not be loaded for this time.", tone: "is-warning" };
  if (adsb?.status === "waiting") {
    return tracksWaiting || arcs?.status === "waiting" ? null : { text: ADSB_WAITING, tone: "is-pending" };
  }
  return null;
}

/**
 * The availability strip's wait on a 429, which the strip itself does not
 * word: a line of its own, unless the tracks, the arcs or the ADS-B traffic
 * already say the archive is being waited out.
 */
function availabilityNoticeFor(
  availability: TrackAvailability,
  tracksWaiting: boolean,
  arcs: ArchiveArcsState | undefined,
  adsb: ArchiveAdsbState | undefined,
): SideNotice | null {
  if (availability.status !== "waiting" || tracksWaiting || arcs?.status === "waiting" || adsb?.status === "waiting") return null;
  return { text: AVAILABILITY_WAITING, tone: "is-pending" };
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
  const tracksWaiting = props.status === "waiting";
  const arcsNotice = arcsNoticeFor(props.arcs, tracksWaiting);
  const availabilityNotice = availabilityNoticeFor(availability, tracksWaiting, props.arcs, props.adsb);
  const adsbNotice = adsbNoticeFor(props.adsb, tracksWaiting, props.arcs);
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

      {arcsNotice && (
        <div role="status" className={`archive-notice ${arcsNotice.tone}`}>
          <span className="archive-notice-text">{arcsNotice.text}</span>
        </div>
      )}

      {adsbNotice && (
        <div role="status" className={`archive-notice ${adsbNotice.tone}`}>
          <span className="archive-notice-text">{adsbNotice.text}</span>
        </div>
      )}

      {availabilityNotice && (
        <div role="status" className={`archive-notice ${availabilityNotice.tone}`}>
          <span className="archive-notice-text">{availabilityNotice.text}</span>
        </div>
      )}
    </section>
  );
}
