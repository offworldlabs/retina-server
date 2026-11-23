/* ------------------------------------------------------------------ */
/*  Archive replay: the clock, the window of /api/data/tracks loaded  */
/*  around it, and the frames the map ingests instead of a socket.    */
/*                                                                    */
/*  One session per stay in archive mode. It owns the clock, the      */
/*  indexes of the windows loaded (the one in use, and the one before */
/*  or after it), the one request out, the play and seek timers, and  */
/*  the feed's single subscriber; React reads its state through       */
/*  useSyncExternalStore and drives it from effects. A drag of the    */
/*  scrubber asks for no window until it settles: it moves between    */
/*  the windows held, and keeps a window asked for ahead of them.     */
/* ------------------------------------------------------------------ */

import { useEffect, useMemo } from "react";

import { api } from "../../../api/client";
import { describeFailure, retryDelayMs, type FailureKind, type RequestFailure } from "../../../utils/requestFailure";
import type { NodeGeometry } from "../bistaticArc";
import { ARC_TOTAL_LIFE_MS } from "../constants";
import type { FeedSource, InjectedFrame } from "../hooks";
import {
  ARCHIVE_SPAN_DEFAULT_MIN,
  ARCHIVE_SPAN_MAX_MIN,
  ARCHIVE_SPAN_MIN_MIN,
  writeArchiveHash,
} from "../useUrlHashState";
import type { DetectionWindow } from "./detectionArcs";
import { ARCS_OFF, DetectionsLoader, SEEK_SETTLE_MS, type ArchiveArcsState } from "./detectionsLoader";
import { ADSB_EXPIRY_MS, DARK_EXPIRY_MS, buildReplayIndex, synthesizeFrame, type ReplayIndex } from "./synthesize";
import type { TracksResponse } from "./types";
import { useStoreSession } from "./useStoreSession";

export type { ArchiveArcsState } from "./detectionsLoader";

/** The map's node registry by node_ref, read only when a frame is built. */
export interface NodesByRefRef {
  readonly current: Record<string, NodeGeometry> | null | undefined;
}

export type ReplayRate = 1 | 5 | 20;
/**
 * "waiting": the archive turned the request away for asking too often (a
 * 429), and the same request goes out again on its own once the wait it named
 * is over (retryDelayMs). What is held meanwhile goes on being shown.
 */
export type LoadStatus = "idle" | "loading" | "waiting" | "ready" | "error";

/**
 * Why the window asked for is not shown: a failed request, read as the rest of
 * the app reads one, or a stretch too dense to show. Never an abort, which
 * only means a newer request took its place, nor a 429, which is waited out
 * ("waiting") rather than reported.
 */
export type ArchiveError = RequestFailure & { kind: Exclude<FailureKind, "aborted" | "throttled"> };

/**
 * A link or a seek asked for an instant the archive cannot show (past the live
 * edge, or before ARCHIVE_EARLIEST_MS), and the clock went to the nearest one
 * it can.
 */
export interface ArchiveClamp {
  /** A link (opened, or a hashchange), or a seek (a step included). */
  by: "link" | "seek";
  /** The instant asked for. */
  askedMs: number;
}

export interface ArchiveReplay {
  /** Stable for one archive session; null when inactive. */
  source: FeedSource | null;
  /** The replay instant. While inactive, `t` as given (0 when null). */
  clockMs: number;
  /** The clock of the last frame emitted, i.e. what the map's stores hold. */
  frameNowMs: number | null;
  playing: boolean;
  /**
   * The clock is within LIVE_EDGE_MS of the live edge, where play has nothing
   * to show and does not start. It turns false on its own once the edge has
   * moved that far past the clock.
   */
  atLiveEdge: boolean;
  /**
   * Set while the clock stays where a clamped link or seek put it; the next
   * seek, step or play clears it, and so does playback moving the clock on.
   */
  clamp: ArchiveClamp | null;
  rate: ReplayRate;
  /**
   * The instants the window in use serves, which the scrubber spans: from the
   * end of its lead-in (WINDOW_LEAD_IN_MS) to its servable end, the first of
   * its own end, the archive's edge when it was fetched and, when the server
   * cut the response, the last row's publish time. A seek anywhere in it is
   * shown from that window with no request; `to` itself is too, unless it is
   * the window's own end, which the API leaves out. While a window loads, the
   * range it serves unless the server cuts it.
   */
  windowRange: { from: number; to: number } | null;
  status: LoadStatus;
  error: ArchiveError | null;
  truncated: boolean;
  count: number;
  trackCount: number;
  /** The detection arcs layer, apart from the tracks' status and error: its failure never stops replay. */
  arcs: ArchiveArcsState;
  togglePlay(): void;
  play(): void;
  pause(): void;
  setRate(r: ReplayRate): void;
  step(deltaS: number): void;
  seek(ms: number): void;
  retry(): void;
  /**
   * Shows the archived detection arcs, or stops: while on, frames carry the
   * detections published around the clock, drawn with the beam and range
   * `nodesByRefRef.current` holds for each node when the frame is built.
   */
  setArcs(enabled: boolean, nodesByRefRef: NodesByRefRef): void;
}

/** The archive starts here; no clock goes earlier. */
export const ARCHIVE_EARLIEST_MS = Date.UTC(2020, 0, 1);
/** Wall time between two playback ticks. */
export const TICK_MS = 250;
/** Seeks land at most once per this much wall time; the last one always lands. */
export const SEEK_COALESCE_MS = 200;
/** A frame further than this from the last one emitted (or behind it) resets the map's stores. */
export const RESET_GAP_MS = 10_000;
/** Within this of the live edge, play has nothing left to show. */
export const LIVE_EDGE_MS = 1_000;
/** What the play buttons say while play has nothing left to show. */
export const AT_LIVE_EDGE_TITLE = "Already at the latest archived moment. Jump to now opens the live feed.";
/**
 * How often the server flushes the archive. Playing on from the last instant a
 * window serves asks for the next one only when that gains at least this much.
 */
export const FLUSH_PERIOD_MS = 60_000;
/**
 * How far behind now the archive is complete. The server flushes it every
 * 60 s, so a row can reach it up to a flush late. The clock goes no later than
 * now less this (the live edge), and a window fetched while it reached past
 * that serves only up to its fetch less this: rows flushed after the fetch
 * never reach it.
 */
export const ARCHIVE_LAG_MS = 120_000;
/** While playing, the URL follows the clock at most this often. */
export const HASH_EVERY_MS = 3_000;
/** Playing, the next window is requested this far (in replay time) before the loaded one stops serving, */
export const PREFETCH_MIN_MS = 60_000;
/** ... or this much per unit of rate, whichever is more, but never more than half the window's radius. */
export const PREFETCH_PER_RATE_MS = 15_000;
/**
 * A window serves frames only from this long after its start. The window holds
 * the rows solved inside it, and an entry lives for its lane's expiry after its
 * solve, so before this a frame would be missing the aircraft whose last solve
 * fell just before the window. Further in, every entry is the live one.
 */
export const WINDOW_LEAD_IN_MS = Math.max(DARK_EXPIRY_MS, ADSB_EXPIRY_MS);

const MINUTE_MS = 60_000;

/**
 * A window around the clock that the server cuts short of the clock is asked
 * for again on half the radius, and so on down to this. Narrower than that, a
 * window would be mostly lead-in.
 */
export const NARROW_FLOOR_MS = 2 * MINUTE_MS;
/** What the bar says when even NARROW_FLOOR_MS around the clock is cut short of it. */
export const TOO_DENSE_MESSAGE = "Too many solves around this instant to show.";
/** What the bar says for an answer whose body the index cannot be built from. */
export const UNREADABLE_MESSAGE = "The archive sent a window the map could not read.";
const UNREADABLE: ArchiveError = {
  kind: "unexpected",
  status: null,
  message: UNREADABLE_MESSAGE,
  retryable: true,
};
/** No answer failed (no status), and asking again gets the same cut window (not retryable). */
const TOO_DENSE: ArchiveError = { kind: "invalid", status: null, message: TOO_DENSE_MESSAGE, retryable: false };

interface Range {
  readonly from: number;
  readonly to: number;
}

interface Loaded {
  index: ReplayIndex;
  window: Range;
  /** The hash's radius when it was asked for (a narrowed window keeps the span it was asked under). */
  spanMs: number;
  /** Date.now() when the answer arrived. */
  fetchedAt: number;
  /** The rows the server matched, which the bar reports while the window is in use. */
  count: number;
  /** The last instant it serves (see loadedOf). */
  end: number;
  /** Whether the server's cut is what ends it, rather than its own end or the archive's lag. */
  cut: boolean;
  /** The instants it serves, [end of its lead-in, end]: windowRange while it is in use. */
  range: Range;
}

interface InFlight {
  gen: number;
  window: Range;
  spanMs: number;
  controller: AbortController;
  /**
   * Set once the archive has turned the request away (a 429): the timer that
   * asks for the window again (see waitOut). Until it fires the request
   * stands as the one out for the window, and anything that lets go of a
   * request out lets go of it too.
   */
  waiting: ReturnType<typeof setTimeout> | null;
}

type Snapshot = Omit<
  ArchiveReplay,
  "source" | "togglePlay" | "play" | "pause" | "setRate" | "step" | "seek" | "retry" | "setArcs"
>;

/** The latest instant the archive holds in full, and the latest the clock goes to. */
function liveEdge(): number {
  return Date.now() - ARCHIVE_LAG_MS;
}

function clampInstant(ms: number): number {
  return Math.min(Math.max(ms, ARCHIVE_EARLIEST_MS), liveEdge());
}

/** What a clock set to `shown` for `asked` reports: a clamp when it is not the instant asked for. */
function clampOf(by: ArchiveClamp["by"], asked: number, shown: number): ArchiveClamp | null {
  return shown === asked ? null : { by, askedMs: asked };
}

function spanMsOf(minutes: number): number {
  const m = Number.isFinite(minutes) ? Math.round(minutes) : ARCHIVE_SPAN_DEFAULT_MIN;
  return Math.min(ARCHIVE_SPAN_MAX_MIN, Math.max(ARCHIVE_SPAN_MIN_MIN, m)) * MINUTE_MS;
}

/**
 * Whether frames at `clock` can come from `window` once it is fetched (see
 * WINDOW_LEAD_IN_MS), unless the server cuts it short: what a request out is
 * waited for, as its answer cannot be known before it lands.
 */
function serves(window: Range, clock: number): boolean {
  return clock >= window.from + WINDOW_LEAD_IN_MS && clock < window.to;
}

function sameRange(a: Range, b: Range): boolean {
  return a.from === b.from && a.to === b.to;
}

/** The radius `window` was asked for with: windows are always asked for around an instant. */
function radiusOf(window: Range): number {
  return (window.to - window.from) / 2;
}

/**
 * A window as it lands. It serves up to its servable end, the first of: its
 * own end; the live edge as it stood when it arrived, since rows flushed after
 * the fetch never reach it; and, when the server cut the response, the last
 * frame time of a row in it. The rows come sorted by frame time, and the cut
 * leaves out the rest, so past that instant the window lacks rows the archive
 * holds, and a track would show an older position than it had.
 */
function loadedOf(index: ReplayIndex, window: Range, spanMs: number, fetchedAt: number, count: number): Loaded {
  const uncut = Math.min(window.to, fetchedAt - ARCHIVE_LAG_MS);
  const end = index.truncated ? Math.min(uncut, index.lastFrameTs) : uncut;
  return {
    index,
    window,
    spanMs,
    fetchedAt,
    count,
    end,
    cut: end < uncut,
    range: { from: window.from + WINDOW_LEAD_IN_MS, to: end },
  };
}

/**
 * Whether frames at `clock` can come from the loaded index: from the end of
 * its lead-in to its servable end. That end is inclusive, so a window fetched
 * for a clock clamped to the live edge always serves it, even when it lands in
 * the same millisecond, and a cut window serves the instant of its last row.
 * The window's own end is not: the API leaves it out.
 */
function servesLoaded(loaded: Loaded, clock: number): boolean {
  return clock >= loaded.range.from && clock <= loaded.end && clock < loaded.window.to;
}

/**
 * Whether `window`, once fetched, serves an instant `held` serves: a window
 * asked for ahead of it or just before it, which a clock moving between the
 * two goes on into, rather than one asked for elsewhere.
 */
function overlaps(window: Range, held: Loaded): boolean {
  return window.from + WINDOW_LEAD_IN_MS <= held.end && held.range.from < window.to;
}

/**
 * The archive routes' own reason for turning a request down: the string
 * `detail` of a 400 or a 422. Other answers' details only restate the status.
 * Read structurally, as describeFailure reads the status.
 */
export function validationDetail(err: unknown): string | null {
  const { status, body } = (err ?? {}) as { status?: unknown; body?: unknown };
  if (status !== 400 && status !== 422) return null;
  const detail = (body as { detail?: unknown } | null)?.detail;
  return typeof detail === "string" && detail ? detail : null;
}

/**
 * A failed window request as the bar reports it; null for an abort, which is
 * no failure, and for a 429, which the session waits out instead.
 */
export function archiveErrorOf(err: unknown): ArchiveError | null {
  const detail = validationDetail(err);
  // describeFailure reads a 422 as invalid and a 400 as refused; either way the route said why.
  const failure = describeFailure(err, detail === null ? undefined : { invalid: detail, refused: detail });
  if (failure.kind === "aborted" || failure.kind === "throttled") return null;
  return { ...failure, kind: failure.kind };
}

function sameSnapshot(a: Snapshot, b: Snapshot): boolean {
  for (const key of Object.keys(b) as (keyof Snapshot)[]) if (a[key] !== b[key]) return false;
  return true;
}

class ReplaySession {
  readonly source: FeedSource;

  private snapshot: Snapshot;
  private readonly watchers = new Set<() => void>();

  private spanMs: number;
  /**
   * The radius loads use in a stretch too dense for the span: half the radius
   * of the last window the server cut short of the clock. Null while the span
   * applies, which it does again once a window asked for on this radius lands
   * whole, or the link or span changes.
   */
  private narrowMs: number | null = null;
  /** The link last opened; undefined until the first, and again once disposed. */
  private rev: number | undefined = undefined;
  /** Whether a clock has been asked for: until then a span change has nothing to reload. */
  private opened = false;
  private clock: number;
  private playing = false;
  /** The next tick is the first since play(): it may ask once for more archive, however little that gains. */
  private firstTick = false;
  private rate: ReplayRate = 1;
  private clamp: ArchiveClamp | null = null;

  /** The window asked for last (or that a drag holds back, see holdBack), or the one in use once it is. */
  private target: Range | null = null;
  /** windowRange: what `target` serves, or would once it lands. */
  private range: Range | null = null;
  private status: LoadStatus = "idle";
  private error: ArchiveError | null = null;
  private count = 0;
  private truncated = false;
  private trackCount = 0;

  /** The window in use: the one frames come from while it serves the clock. */
  private loaded: Loaded | null = null;
  /**
   * The other window held: the one in use before the last that landed, while
   * the two overlap, or one that landed not serving the clock while the window
   * in use did; neither past a window cut short of the clock (see landed). A
   * clock that moves into what only it serves makes it the one in use again
   * (see switchTo), with no request.
   */
  private spare: Loaded | null = null;
  private inflight: InFlight | null = null;
  /** Bumped whenever a request stops being the current one; an answer of another generation is ignored. */
  private gen = 0;

  private listener: ((frame: InjectedFrame) => void) | null = null;
  /** The listener has had no frame yet, so the next one resets its stores. */
  private fresh = true;
  private lastEmitted: number | null = null;

  private ticker: ReturnType<typeof setInterval> | null = null;
  private landTimer: ReturnType<typeof setTimeout> | null = null;
  private lastLandWall = -Infinity;
  /** The last seek landed within SEEK_SETTLE_MS of the one before it: a drag, until SEEK_SETTLE_MS passes with no landing. */
  private dragged = false;
  /** Does what a drag put off once it settles (see defer), at `settleDue`. */
  private settleTimer: ReturnType<typeof setTimeout> | null = null;
  private settleDue: number | null = null;
  private lastHashWall = -Infinity;
  /** Publishes when the edge moves LIVE_EDGE_MS past a paused clock (see watchEdge), due at `edgeDue`. */
  private edgeTimer: ReturnType<typeof setTimeout> | null = null;
  private edgeDue: number | null = null;

  /** The detections around the clock, while the arcs layer is on. */
  private readonly arcs = new DetectionsLoader({
    clock: () => this.clock,
    rate: () => this.rate,
    changed: (landed) => this.arcsChanged(landed),
  });
  private nodesRef: NodesByRefRef | null = null;

  constructor(t: number | null, spanMin: number) {
    this.spanMs = spanMsOf(spanMin);
    this.clock = t === null ? liveEdge() : clampInstant(t);
    this.snapshot = this.read();
    this.source = { subscribe: this.subscribeFrames };
  }

  /* ---------------- React's side ---------------- */

  subscribe = (onChange: () => void): (() => void) => {
    this.watchers.add(onChange);
    return () => {
      this.watchers.delete(onChange);
    };
  };

  getSnapshot = (): Snapshot => this.snapshot;

  /**
   * The hook's arguments. A rev not seen yet is a new link (or the session's
   * start): the clock goes to `t`, paused, on the radius given with it. The
   * same rev with another radius reloads around the clock. Either way the
   * radius given is the one loads use, until a dense stretch narrows it again.
   */
  sync = (rev: number, t: number | null, spanMin: number): void => {
    const spanMs = spanMsOf(spanMin);
    if (rev !== this.rev) {
      this.rev = rev;
      this.spanMs = spanMs;
      this.narrowMs = null;
      this.openAt(t);
    } else if (spanMs !== this.spanMs) {
      this.spanMs = spanMs;
      this.narrowMs = null;
      // Asked for on the radius left behind, it is not shown again.
      this.spare = null;
      if (!this.opened) return;
      this.load(this.windowAround(this.clock));
    }
    this.publish();
  };

  /** Leaving archive mode: nothing out, nothing scheduled, nothing kept. */
  dispose = (): void => {
    this.cancelLand();
    this.cancelSettle();
    this.stop(false);
    this.abortInflight();
    this.arcs.dispose();
    this.rev = undefined;
    this.opened = false;
    this.narrowMs = null;
    this.loaded = null;
    this.spare = null;
    this.target = null;
    this.range = null;
    this.status = "idle";
    this.error = null;
    this.count = 0;
    this.truncated = false;
    this.trackCount = 0;
    this.clamp = null;
    this.fresh = true;
    this.lastEmitted = null;
    this.lastLandWall = -Infinity;
    this.publish();
  };

  /* ---------------- controls ---------------- */

  play = (): void => {
    if (this.playing || this.atEdge()) return;
    this.playing = true;
    this.firstTick = true;
    this.clamp = null;
    this.lastHashWall = Date.now();
    this.ticker = setInterval(this.tick, TICK_MS);
    this.publish();
  };

  pause = (): void => {
    if (!this.playing) return;
    this.stop(true);
    this.publish();
  };

  togglePlay = (): void => {
    if (this.playing) this.pause();
    else this.play();
  };

  setRate = (r: ReplayRate): void => {
    if (r !== 1 && r !== 5 && r !== 20) return;
    this.rate = r;
    this.publish();
  };

  seek = (ms: number): void => {
    if (!Number.isFinite(ms)) return;
    this.clock = clampInstant(ms);
    this.clamp = clampOf("seek", ms, this.clock);
    this.queueLand();
    this.publish();
  };

  step = (deltaS: number): void => {
    if (!Number.isFinite(deltaS)) return;
    this.pause();
    this.seek(this.clock + deltaS * 1000);
  };

  retry = (): void => {
    this.opened = true;
    this.load(this.target ?? this.windowAround(this.clock));
    this.arcs.retry(this.clock, this.rate);
    this.publish();
  };

  setArcs = (enabled: boolean, nodesByRefRef: NodesByRefRef): void => {
    this.nodesRef = nodesByRefRef;
    if (enabled === this.arcs.enabled) return;
    if (enabled) {
      this.arcs.enable();
      // Before a clock has been asked for there is nothing to load around; the first jump asks.
      if (this.opened) this.arcs.place(this.clock, this.rate, true);
    } else {
      this.arcs.disable();
    }
    this.publish();
  };

  /* ---------------- the feed ---------------- */

  private subscribeFrames = (onFrame: (frame: InjectedFrame) => void): (() => void) => {
    this.listener = onFrame;
    this.fresh = true;
    // Not inside the subscriber's own effect: it has only just cleared its stores.
    queueMicrotask(() => {
      if (this.listener === onFrame && this.fresh && this.emit()) this.publish();
    });
    return () => {
      if (this.listener === onFrame) this.listener = null;
    };
  };

  /**
   * Hands the listener an empty frame at the clock that resets its stores: the
   * clock has moved to an instant nothing loaded shows, so the map holds no
   * aircraft from the instant before. The frame its window shows once it lands
   * is then no reset (the same instant), and starts from an empty map.
   */
  private clear(): void {
    const listener = this.listener;
    if (!listener) return;
    const clock = this.clock;
    this.fresh = false;
    this.lastEmitted = clock;
    this.arcs.cleared();
    listener({
      now: clock / 1000,
      aircraft: [],
      detection_arcs: [],
      detecting_nodes: {},
      anomaly_hexes: [],
      ground_truth: {},
      ground_truth_meta: {},
      reset: true,
    });
  }

  /**
   * Hands the listener the frame at the clock, when the loaded index serves
   * it, with the detection arcs it has not had yet (DetectionsLoader.arcsAt).
   */
  private emit(): boolean {
    const listener = this.listener;
    const loaded = this.loaded;
    const clock = this.clock;
    if (!listener || !loaded || !servesLoaded(loaded, clock)) return false;
    const last = this.lastEmitted;
    const reset = this.fresh || last === null || clock < last || clock - last > RESET_GAP_MS;
    this.fresh = false;
    this.lastEmitted = clock;
    const frame = synthesizeFrame(loaded.index, clock);
    const arcs = this.arcs.arcsAt(clock, reset, frame.aircraft, this.nodesRef?.current);
    listener({ ...frame, detection_arcs: arcs, reset });
    return true;
  }

  /**
   * A detections window landed, or a request for one ended without it. One
   * with rows the map would show at the clock is shown at once, paused or
   * playing, unless a seek is about to land and show its own instant.
   */
  private arcsChanged(landed: DetectionWindow | null): void {
    if (
      landed &&
      this.landTimer === null &&
      landed.from <= this.clock &&
      landed.servedEnd > this.clock - ARC_TOTAL_LIFE_MS
    ) {
      this.emit();
    }
    this.publish();
  }

  /* ---------------- the clock ---------------- */

  private openAt(t: number | null): void {
    this.cancelLand();
    // A link is a jump on its own: it decides afresh what the clock needs.
    this.cancelSettle();
    this.stop(false);
    this.clamp = null;
    if (t === null || !Number.isFinite(t)) return;
    this.opened = true;
    this.clock = clampInstant(t);
    this.clamp = clampOf("link", t, this.clock);
    // The URL names the instant shown, not the one the archive cannot show.
    if (this.clamp) this.writeHash(Date.now());
    this.arcs.place(this.clock, this.rate, true);
    this.serve(true);
  }

  private tick = (): void => {
    if (!this.playing) return;
    const first = this.firstTick;
    this.firstTick = false;
    const wall = Date.now();
    const edge = wall - ARCHIVE_LAG_MS;
    const next = Math.min(this.clock + TICK_MS * this.rate, edge);
    // Play goes on into the other window held where the one in use stops
    // short of the next instant, as it would into one that had just landed.
    if (!this.switchTo(next) && !(this.loaded && servesLoaded(this.loaded, next))) this.switchTo(this.clock);
    const loaded = this.loaded;
    // One out serves what its window does, since it lands after this tick,
    // when the edge is past `next`.
    const out = this.inflight;

    if (loaded && servesLoaded(loaded, this.clock) && !servesLoaded(loaded, next)) {
      // The clock is on the last instant the index in use serves: its window's
      // end, the edge as it stood when it was fetched, or the server's cut.
      // Hold for a window out that serves the next instant. Otherwise the
      // window around the clock is asked for at once past a cut, which a later
      // fetch of the archive does not move. At the edge it is asked for only
      // when the archive holds a flush period past the clock, or on a play's
      // first tick: nearer the edge each window would serve only the seconds
      // since the one before it, and play would ask again every few seconds.
      // Short of that, play stops here as a pause would, on an instant the map
      // shows, and before the edge below would move it past one. A drag holds
      // the clock here until it settles, which asks then (see settled).
      if (!out || !serves(out.window, next)) {
        if (first || loaded.cut || this.clock <= edge - FLUSH_PERIOD_MS) {
          if (this.dragging()) {
            this.firstTick = first;
            this.defer();
          } else {
            this.loadAround(this.clock);
          }
        } else {
          this.stop(true);
        }
      }
      this.publish();
      return;
    }

    if (next >= edge) {
      // Play has caught up with the archive with nothing loaded showing the
      // clock, or with the index in use serving the edge itself: it stops at
      // the edge as a pause would, and the edge is shown as a landed seek is,
      // the window around it loaded once if neither the one in use nor one out
      // serves it. Before the hold below, so the edge never has a request
      // asked for again.
      // A clamp stays: only a seek past the edge clamps a clock this near
      // it, and the clock is still on the live edge that seek put it on.
      const moved = this.clock !== edge;
      this.clock = edge;
      this.stop(true);
      this.arcs.place(edge, this.rate, false);
      this.serve(moved);
      this.publish();
      return;
    }

    if (!loaded || !servesLoaded(loaded, next)) {
      // Nothing loaded serves the clock or the next instant: hold until the
      // window that serves it lands, asking for one if nothing is out for it,
      // once a drag settles if one goes on.
      if (!out || !serves(out.window, next)) {
        if (this.dragging()) this.defer();
        else this.loadAround(this.clock);
      }
      this.publish();
      return;
    }

    this.clock = next;
    // Moved on from where a clamped seek put it, the clamp no longer describes it.
    this.clamp = null;
    this.settle();
    this.arcs.place(next, this.rate, false);
    this.emit();
    const ahead = this.inflight ? null : this.prefetchDue(loaded, next, edge);
    if (ahead) {
      // A drag would take the clock away from it before it lands: it is asked
      // for once the drag settles, if it is still due then.
      if (this.dragging()) this.defer();
      else this.load(ahead);
    }
    if (wall - this.lastHashWall >= HASH_EVERY_MS) this.writeHash(wall);
    this.publish();
  };

  /**
   * The window around the clock, once the clock is within the margin of where
   * the index in use stops serving, so it lands before it is needed; null
   * while it is not due, or the other window held goes on from there.
   */
  private prefetchDue(loaded: Loaded, clock: number, edge: number): Range | null {
    // At most half the radius, so a window just loaded around the clock is
    // never due again at once.
    const margin = Math.min(Math.max(PREFETCH_MIN_MS, PREFETCH_PER_RATE_MS * this.rate), this.radius() / 2);
    const end = loaded.end;
    if (clock < end - margin) return null;
    // Asked for ahead before, and kept while the clock went back from it:
    // play goes on into it where this one stops.
    const spare = this.spare;
    if (spare && servesLoaded(spare, end) && spare.end > end) return null;
    const ahead = this.windowAround(clock);
    if (sameRange(ahead, loaded.window)) return null;
    // A window asked for now serves no further than the edge. Worth a request
    // only when that carries the clock at least the margin further, or near the
    // edge each window would be due again as soon as it landed. Away from the
    // edge this always holds: `ahead` ends a radius past a whole second at or
    // after `end - margin`, and the margin is at most half that radius.
    if (Math.min(ahead.to, edge) - end < margin) return null;
    return ahead;
  }

  private stop(writeHash: boolean): void {
    if (this.ticker !== null) {
      clearInterval(this.ticker);
      this.ticker = null;
    }
    if (!this.playing) return;
    this.playing = false;
    if (writeHash) this.writeHash(Date.now());
  }

  /** Lands the latest seek now, or once SEEK_COALESCE_MS has passed since the last one landed. */
  private queueLand(): void {
    if (this.landTimer !== null) return;
    const wait = this.lastLandWall + SEEK_COALESCE_MS - Date.now();
    if (wait <= 0) {
      this.land();
      return;
    }
    this.landTimer = setTimeout(() => {
      this.landTimer = null;
      this.land();
      this.publish();
    }, wait);
  }

  private cancelLand(): void {
    if (this.landTimer === null) return;
    clearTimeout(this.landTimer);
    this.landTimer = null;
  }

  /**
   * A seek lands. One within SEEK_SETTLE_MS of the last is part of a drag of
   * the scrubber (as DetectionsLoader reads one), which goes on until
   * SEEK_SETTLE_MS passes with none: a drag lands a seek every
   * SEEK_COALESCE_MS, and a window asked for on each would spend the
   * archive's read limit in seconds. A seek on its own asks at once.
   */
  private land(): void {
    const wall = Date.now();
    this.dragged = wall - this.lastLandWall < SEEK_SETTLE_MS;
    this.lastLandWall = wall;
    // What a drag put off waits for its last seek; a seek on its own decides afresh.
    if (this.settleTimer !== null) {
      if (this.dragged) this.defer();
      else this.cancelSettle();
    }
    this.opened = true;
    this.writeHash(wall);
    this.arcs.place(this.clock, this.rate, true);
    this.serve(true);
  }

  /**
   * Whether a drag goes on: a seek waits to land (which it does within
   * SEEK_COALESCE_MS of the last, so in a drag), the last one landed within
   * SEEK_SETTLE_MS of the one before it and less than that ago, or what the
   * drag put off is not done yet.
   */
  private dragging(): boolean {
    return (
      this.landTimer !== null ||
      this.settleTimer !== null ||
      (this.dragged && Date.now() - this.lastLandWall < SEEK_SETTLE_MS)
    );
  }

  /** What a drag puts off is done once it settles: SEEK_SETTLE_MS after its last seek landed. */
  private defer(): void {
    const due = this.lastLandWall + SEEK_SETTLE_MS;
    if (this.settleTimer !== null) {
      if (due === this.settleDue) return;
      clearTimeout(this.settleTimer);
    }
    this.settleDue = due;
    this.settleTimer = setTimeout(this.settled, Math.max(0, due - Date.now()));
  }

  /**
   * The drag has settled, with the clock where it is now: the window around
   * it is asked for when nothing held or out serves it, and otherwise, while
   * playing, the window ahead when it is due, so play does not hold at the
   * end of the window in use for want of it.
   */
  private settled = (): void => {
    this.settleTimer = null;
    this.settleDue = null;
    if (this.ask()) {
      this.settle();
      const loaded = this.loaded;
      const ahead = loaded && this.playing && !this.inflight ? this.prefetchDue(loaded, this.clock, liveEdge()) : null;
      if (ahead) this.load(ahead);
    }
    this.publish();
  };

  /** No drag goes on, and nothing it put off is left to do. */
  private cancelSettle(): void {
    this.dragged = false;
    this.settleDue = null;
    if (this.settleTimer === null) return;
    clearTimeout(this.settleTimer);
    this.settleTimer = null;
  }

  private writeHash(wall: number): void {
    writeArchiveHash(this.clock);
    this.lastHashWall = wall;
  }

  /* ---------------- windows ---------------- */

  /** The radius loads use: the span, or a narrower one in a dense stretch. */
  private radius(): number {
    return this.narrowMs ?? this.spanMs;
  }

  private windowAround(clock: number, radius = this.radius()): Range {
    const c = Math.floor(clock / 1000) * 1000;
    return { from: c - radius, to: c + radius };
  }

  /**
   * Asks for the window around the clock. When that is the window loaded, and
   * the server cut it at or before the clock, it would come back cut there
   * again: the radius narrows instead.
   */
  private loadAround(clock: number): void {
    const window = this.windowAround(clock);
    const loaded = this.loaded;
    if (loaded && loaded.index.truncated && sameRange(window, loaded.window) && clock >= loaded.index.lastFrameTs) {
      this.narrow(radiusOf(window));
    } else {
      this.load(window);
    }
  }

  /**
   * The window around the clock on `radius` is cut short of it: asks for the
   * one around it on half that radius, down to NARROW_FLOOR_MS, which loads
   * then use until one lands whole. Below that there is nothing narrower to
   * ask for, and the clock is in a stretch too dense to show.
   */
  private narrow(radius: number): void {
    const next = Math.max(Math.round(radius / 2), NARROW_FLOOR_MS);
    if (next >= radius) {
      this.abortInflight();
      this.status = "error";
      this.error = TOO_DENSE;
      this.stop(true);
      return;
    }
    this.narrowMs = next;
    this.load(this.windowAround(this.clock));
  }

  /**
   * Shows the clock: a frame when a window held serves it, and a request for
   * the window around it unless the one in use is current or one out serves it.
   * When the clock has just `moved` (a new link, a landed seek, a stop at the
   * edge) to an instant nothing loaded serves, the map is cleared there until
   * that window lands.
   */
  private serve(moved: boolean): void {
    if (this.ask()) {
      this.settle();
      this.emit();
    } else if (moved) {
      this.clear();
    }
  }

  /**
   * Asks for what the clock needs, from the window held that serves it, and
   * says whether one does. The window around it is asked for unless the one
   * in use is current or one out serves it; in a drag, once the drag settles
   * (see holdBack). A request out for elsewhere is let go of once the window
   * in use serves the clock, unless it was asked for ahead of that window or
   * just before it: the clock went back from it, and a drag or play may well
   * come forward to it again.
   */
  private ask(): boolean {
    const clock = this.clock;
    this.switchTo(clock);
    const { loaded, inflight } = this;
    const usable = loaded !== null && servesLoaded(loaded, clock);
    const current = usable && loaded.spanMs === this.spanMs;
    // One out serves what its window does: the clock was clamped to the edge
    // before it was asked for, and its answer comes later still. A cut is
    // known only once it lands.
    const coming = inflight !== null && serves(inflight.window, clock);
    if (!current && !coming) {
      if (this.dragging()) this.holdBack();
      else this.loadAround(clock);
    } else if (current && inflight && !coming && !overlaps(inflight.window, loaded)) {
      this.abortInflight();
    }
    return usable;
  }

  /**
   * A drag has taken the clock where nothing held or out serves it: the
   * window around it is asked for once the drag settles (see settled), with
   * the clock where it is then, and meanwhile the bar says it loads, as if it
   * were asked for. A request on its way stays, since the drag may come back
   * to it; one waiting out a 429 is let go of, as a seek elsewhere lets go of
   * it, rather than asked for again for a clock that has left it.
   */
  private holdBack(): void {
    if (this.inflight !== null && this.inflight.waiting !== null) this.abortInflight();
    const window = this.windowAround(this.clock);
    this.target = window;
    this.range = { from: window.from + WINDOW_LEAD_IN_MS, to: Math.min(window.to, liveEdge()) };
    this.status = "loading";
    this.error = null;
    this.defer();
  }

  /**
   * Makes the other window held the one in use when it serves `at` and the
   * one in use does not, and says whether it did. The bar then reports it,
   * as it would a window that had just landed.
   */
  private switchTo(at: number): boolean {
    const { loaded, spare } = this;
    if (!spare || !servesLoaded(spare, at) || (loaded && servesLoaded(loaded, at))) return false;
    this.spare = loaded;
    this.loaded = spare;
    this.count = spare.count;
    this.truncated = spare.index.truncated;
    this.trackCount = spare.index.trackCount;
    if (this.status === "ready") {
      this.target = spare.window;
      this.range = spare.range;
    }
    return true;
  }

  /** Nothing out and the loaded index in use: what the bar shows is that window. */
  private settle(): void {
    if (this.inflight || !this.loaded || this.status === "ready") return;
    this.status = "ready";
    this.error = null;
    this.target = this.loaded.window;
    this.range = this.loaded.range;
  }

  private load(window: Range): void {
    this.abortInflight();
    const gen = ++this.gen;
    const controller = new AbortController();
    const spanMs = this.spanMs;
    this.inflight = { gen, window, spanMs, controller, waiting: null };
    this.target = window;
    // What it will serve if the server sends it whole: up to the edge as it stands now, which it lands after.
    this.range = { from: window.from + WINDOW_LEAD_IN_MS, to: Math.min(window.to, Date.now() - ARCHIVE_LAG_MS) };
    this.status = "loading";
    this.error = null;
    let answer: Promise<TracksResponse>;
    try {
      answer = api.archiveTracks({ from: window.from, to: window.to, fleet: "real" }, { signal: controller.signal });
    } catch (err) {
      answer = Promise.reject(err);
    }
    answer.then(
      (resp) => this.landed(gen, window, spanMs, resp),
      (err) => this.failed(gen, err),
    );
  }

  private abortInflight(): void {
    const out = this.inflight;
    if (!out) return;
    this.inflight = null;
    this.gen++;
    if (out.waiting !== null) clearTimeout(out.waiting);
    out.controller.abort();
  }

  private landed(gen: number, window: Range, spanMs: number, resp: TracksResponse): void {
    if (gen !== this.gen) return;
    let index: ReplayIndex;
    try {
      // The index in use hands its legacy tracks' keys on, so a track running
      // on into this window keeps its hex (and the map its selection and trail).
      index = buildReplayIndex(resp, this.loaded?.index ?? null);
    } catch {
      // The server answered, so this is not a failed request: it is a body the index cannot be built from.
      this.inflight = null;
      this.fail(UNREADABLE);
      return;
    }
    this.inflight = null;
    const count = Number.isFinite(resp.count) ? resp.count : index.rowCount;
    const loaded = loadedOf(index, window, spanMs, Date.now(), count);
    const radius = radiusOf(window);
    // Whole on a narrowed radius: the clock has left the dense stretch.
    if (!index.truncated && radius === this.narrowMs) this.narrowMs = null;
    this.switchTo(this.clock);
    const held = this.loaded;
    // Asked for around an instant the window in use serves (as play asks for
    // the window ahead of it, or a new span for its own), and cut short of the
    // clock, which that window still serves: the stretch ahead is too dense
    // for this radius.
    const cutShort =
      held !== null &&
      servesLoaded(held, this.clock) &&
      servesLoaded(held, window.from + radius) &&
      loaded.cut &&
      this.clock > loaded.end;
    if (held && servesLoaded(held, this.clock) && !servesLoaded(loaded, this.clock) && !cutShort) {
      // Asked for ahead of the window in use or just before it, and the clock
      // has since gone into the window in use: kept for a clock that goes on
      // or back to it.
      this.spare = loaded;
      this.settle();
      this.publish();
      return;
    }
    // The window it takes the place of stays while the two overlap, for a
    // clock that goes back into it, but not past a cut short of the clock:
    // the clock holds here, as it does with nothing else held, while the
    // windows around it narrow. Were it kept, play would go on in it and ask
    // again on this radius from each instant it reaches, and the window that
    // lands would never be the one around the clock, which is what narrows.
    this.spare = held && overlaps(window, held) && !cutShort ? held : null;
    this.loaded = loaded;
    this.target = window;
    this.range = loaded.range;
    this.status = "ready";
    this.error = null;
    this.count = count;
    this.truncated = index.truncated;
    this.trackCount = index.trackCount;
    if (index.truncated && sameRange(window, this.windowAround(this.clock, radius)) && !servesLoaded(loaded, this.clock)) {
      // Asked for around the clock, and cut before it: no window of this
      // radius shows it. (One asked for elsewhere is asked for again around
      // the clock first, as any window that does not serve it.)
      this.narrow(radius);
    } else {
      this.serve(false);
    }
    this.publish();
  }

  private failed(gen: number, err: unknown): void {
    if (gen !== this.gen) return;
    const out = this.inflight;
    if (out && describeFailure(err).kind === "throttled") {
      this.waitOut(out, retryDelayMs(err));
      return;
    }
    this.inflight = null;
    const error = archiveErrorOf(err);
    if (!error) {
      this.settle();
      this.publish();
      return;
    }
    this.fail(error);
  }

  /**
   * The archive turned the request for `out.window` away for asking too often
   * (a 429): it asks for the same window again on its own after `delayMs`.
   * Until then that request stays the one out, so play holds for it as it
   * would for an answer, a seek it serves waits for it, and nothing asks for
   * another window sooner. The index in use keeps serving what it serves, and
   * play goes on, as it does while a window loads. A seek elsewhere, a new
   * link or span, a retry or leaving archive mode lets go of it, timer and
   * all, as it lets go of any request out (abortInflight).
   */
  private waitOut(out: InFlight, delayMs: number): void {
    this.status = "waiting";
    this.error = null;
    out.waiting = setTimeout(() => {
      out.waiting = null;
      if (this.inflight !== out) return;
      this.inflight = null;
      this.load(out.window);
      this.publish();
    }, delayMs);
    this.publish();
  }

  private fail(error: ArchiveError): void {
    this.status = "error";
    this.error = error;
    this.stop(true);
    this.publish();
  }

  /* ---------------- state ---------------- */

  /** Whether play has nothing to show: the clock is within LIVE_EDGE_MS of the live edge. */
  private atEdge(): boolean {
    return this.clock >= liveEdge() - LIVE_EDGE_MS;
  }

  /**
   * Paused at the live edge, the snapshot says so until the edge has moved
   * LIVE_EDGE_MS past the clock, which nothing else in the session marks: a
   * timer publishes then, so the play buttons follow what play() would do.
   */
  private watchEdge(): void {
    const due =
      this.rev !== undefined && !this.playing && this.snapshot.atLiveEdge
        ? this.clock + ARCHIVE_LAG_MS + LIVE_EDGE_MS + 1
        : null;
    if (due === this.edgeDue) return;
    if (this.edgeTimer !== null) clearTimeout(this.edgeTimer);
    this.edgeTimer = null;
    this.edgeDue = due;
    if (due === null) return;
    this.edgeTimer = setTimeout(() => {
      this.edgeTimer = null;
      this.edgeDue = null;
      this.publish();
    }, Math.max(0, due - Date.now()));
  }

  /**
   * The window in use when it serves the clock and the request out does not:
   * one asked for ahead of it, or just before it, kept while the clock went
   * back into the window in use (see ask).
   */
  private aside(): Loaded | null {
    const { inflight, loaded, clock } = this;
    if (!inflight || !loaded || serves(inflight.window, clock) || !servesLoaded(loaded, clock)) return null;
    return loaded;
  }

  private read(): Snapshot {
    // A request kept aside is not what the bar describes: the window in use
    // is, and the scrubber spans what it serves. A 429 on it is still said.
    const aside = this.aside();
    return {
      clockMs: this.clock,
      frameNowMs: this.lastEmitted,
      playing: this.playing,
      atLiveEdge: this.atEdge(),
      clamp: this.clamp,
      rate: this.rate,
      windowRange: aside ? aside.range : this.range,
      status: aside && this.status === "loading" ? "ready" : this.status,
      error: this.error,
      truncated: this.truncated,
      count: this.count,
      trackCount: this.trackCount,
      arcs: this.arcs.state(this.clock),
    };
  }

  private publish(): void {
    const next = this.read();
    if (!sameSnapshot(this.snapshot, next)) {
      this.snapshot = next;
      for (const onChange of this.watchers) onChange();
    }
    this.watchEdge();
  }
}

const NOOP = () => {};
const IDLE_ACTIONS = {
  togglePlay: NOOP,
  play: NOOP,
  pause: NOOP,
  setRate: NOOP,
  step: NOOP,
  seek: NOOP,
  retry: NOOP,
  setArcs: NOOP,
};

/**
 * The archive replay engine. `t` is the instant a link (or the visitor) asked
 * for, `rev` bumps on every new one, `spanMin` is the window's radius in
 * minutes. While `active`, the returned `source` feeds the map: one frame per
 * tick while playing, one per landed seek, each from the window loaded around
 * the clock and flagged `reset` when the map's stores must start over. A jump
 * to an instant no loaded window serves yet clears the map there at once.
 */
export function useArchiveReplay({
  active,
  t,
  spanMin,
  rev,
}: {
  active: boolean;
  t: number | null;
  spanMin: number;
  rev: number;
}): ArchiveReplay {
  // One session per stay in archive mode, made when it starts and dropped when
  // it ends; its source is the feed's for exactly that long.
  const [session, snapshot] = useStoreSession(active, () => new ReplaySession(t, spanMin), IDLE_SNAPSHOT);

  // `t` moves only with `rev`, and a new rev with the same `t` is still a new link.
  useEffect(() => {
    session?.sync(rev, t, spanMin);
  }, [session, rev, t, spanMin]);

  return useMemo(() => {
    if (!session) return { ...IDLE_SNAPSHOT, clockMs: t ?? 0, source: null, ...IDLE_ACTIONS };
    return {
      ...snapshot,
      source: session.source,
      togglePlay: session.togglePlay,
      play: session.play,
      pause: session.pause,
      setRate: session.setRate,
      step: session.step,
      seek: session.seek,
      retry: session.retry,
      setArcs: session.setArcs,
    };
  }, [session, snapshot, t]);
}

const IDLE_SNAPSHOT: Snapshot = {
  clockMs: 0,
  frameNowMs: null,
  playing: false,
  atLiveEdge: false,
  clamp: null,
  rate: 1,
  windowRange: null,
  status: "idle",
  error: null,
  truncated: false,
  count: 0,
  trackCount: 0,
  arcs: ARCS_OFF,
};
