/* ------------------------------------------------------------------ */
/*  Archive replay: the /api/data/detections windows held around the  */
/*  clock while the arcs layer is shown, and which of their rows each */
/*  frame still has to hand the map.                                  */
/*                                                                    */
/*  A best-effort layer: it asks for one window at a time, never      */
/*  retries a failure on its own, and nothing it does touches the     */
/*  tracks. A window the server says it may not have archived in      */
/*  full yet is asked for again once it can have been.                */
/* ------------------------------------------------------------------ */

import { api } from "../../../api/client";
import { describeFailure } from "../../../utils/requestFailure";
import type { NodeGeometry } from "../bistaticArc";
import { ARC_TOTAL_LIFE_MS } from "../constants";
import { buildDetectionWindow, detectionArcs, type ArcSource, type DetectionWindow } from "./detectionArcs";
import type { DetectionsResponse, ReplayAircraft, ReplayArc } from "./types";

/**
 * The arcs layer as the bar reports it. "loading": the window that serves
 * the clock is out. "ready": it is in. "pending": it is in, but the server
 * said it may not have archived all of it yet (it does so hourly), so arcs
 * may still come; it is asked for again once they can have. "error": a
 * request failed, and none is asked for again until a seek away from the
 * windows held, the layer is turned off and on, or the replay is retried.
 * `local` is false when the answer serving the clock said the server may
 * already have deleted some of its window's detections from its disk.
 */
export interface ArchiveArcsState {
  status: "off" | "loading" | "ready" | "pending" | "error";
  local: boolean;
}

export const ARCS_OFF: ArchiveArcsState = Object.freeze({ status: "off", local: true }) as ArchiveArcsState;

/** The widest window the route answers (DETECTIONS_WINDOW_MAX_MS). */
export const DETECTIONS_WINDOW_MS = 600_000;
/** After a cut answer the next window is as long as the stretch it served, but never shorter than this. */
export const DETECTIONS_NARROW_FLOOR_MS = 60_000;
/** The prefetch margin never exceeds this, half the widest window. */
export const DETECTIONS_PREFETCH_MAX_MS = 300_000;
/** Playing, the next window is asked for this far (replay time) before the held ones end, */
export const DETECTIONS_PREFETCH_MIN_MS = 60_000;
/** ... or this much per unit of rate, whichever is more. */
export const DETECTIONS_PREFETCH_PER_RATE_MS = 15_000;
/** At most this many windows are held. */
const HELD_MAX = 2;
/** The server archives detections once an hour (ARCHIVE_FLUSH_INTERVAL_S). */
export const ARCHIVE_WRITE_MS = 3_600_000;
/**
 * A window's detections are all archived this long after it ends: the hourly
 * write and the ten minutes the server allows for a node clock running behind
 * and for the write itself (DETECTIONS_PARTITION_SLACK_MS), and a minute for
 * this clock running ahead of the server's.
 */
export const DETECTIONS_ARCHIVED_AFTER_MS = 4_260_000;
/** A window answered pending is asked for again no sooner than this after the answer. */
export const PENDING_RECHECK_MIN_MS = 60_000;

/** The session the loader serves: its clock and rate, and what it hears back. */
export interface DetectionsHost {
  clock(): number;
  rate(): number;
  /** A window landed (`landed`), or a request ended without one (null). */
  changed(landed: DetectionWindow | null): void;
}

interface Held {
  window: DetectionWindow;
  /** The instant through which its rows have been handed to the map since the last reset. */
  emitted: number;
  /** The server said it may not have archived all of the window yet. */
  pending: boolean;
  /** When (Date.now()) a pending window is asked for again; Infinity for one that is not. */
  recheck: number;
}

interface Out {
  gen: number;
  from: number;
  to: number;
  controller: AbortController;
  /** It asks again for a window held. */
  again: boolean;
}

/** The first window for `clock`: it starts a whole second at or before the oldest instant whose arcs still show. */
export function firstWindowStart(clock: number): number {
  return Math.floor((clock - ARC_TOTAL_LIFE_MS) / 1000) * 1000;
}

/**
 * When a window ending at `to`, answered pending at `wall`, is asked for
 * again: once the archive holds all of it, and no sooner than a minute after
 * the answer, or after a write period when it was itself asked for again.
 */
function recheckAt(to: number, wall: number, again: boolean): number {
  return Math.max(to + DETECTIONS_ARCHIVED_AFTER_MS, wall + (again ? ARCHIVE_WRITE_MS : PENDING_RECHECK_MIN_MS));
}

export class DetectionsLoader {
  private on = false;
  private held: Held[] = [];
  private out: Out | null = null;
  /** Bumped whenever a request stops being the current one; an answer of another generation is ignored. */
  private gen = 0;
  /** A request failed, and none is asked for again until something asks for one. */
  private failed = false;
  /** How long the next window asked for after the last one held is. */
  private length = DETECTIONS_WINDOW_MS;
  private shown: ArchiveArcsState = ARCS_OFF;
  /** Wakes the loader when a pending window held is due to be asked for again, at `wakeAt`. */
  private wake: ReturnType<typeof setTimeout> | null = null;
  private wakeAt = Infinity;

  constructor(private readonly host: DetectionsHost) {}

  get enabled(): boolean {
    return this.on;
  }

  /** Turns the layer on; the session then places the clock (a jump) once it has one. */
  enable(): void {
    this.on = true;
  }

  /** Turns the layer off: nothing out, nothing held, nothing failed. */
  disable(): void {
    this.on = false;
    this.drop();
  }

  /**
   * Leaving archive mode: nothing out and nothing held. The layer stays on,
   * so a session that resumes (React.StrictMode) asks again on its next jump.
   */
  dispose(): void {
    this.drop();
  }

  /**
   * The clock is at `clock`. A jump (a link, a landed seek or step) to an
   * instant outside every window held and the one out starts over there;
   * one inside them asks for nothing. Otherwise a pending window held that
   * is due is asked for again, and playback asks for the next window once
   * the clock is within the prefetch margin of where the held ones end.
   * Either way, a window whose rows have all aged out of the afterglow is
   * let go first.
   */
  place(clock: number, rate: number, jumped: boolean): void {
    this.placed(clock, rate, jumped);
    this.schedule();
  }

  private placed(clock: number, rate: number, jumped: boolean): void {
    if (!this.on) return;
    this.held = this.held.filter((h) => h.window.servedEnd > clock - ARC_TOTAL_LIFE_MS);
    if (jumped) {
      if (!this.covers(clock)) this.restart(clock);
      return;
    }
    if (this.failed || this.out) return;
    const held = this.held;
    if (held.length === 0 || clock < held[0].window.from) {
      this.restart(clock);
      return;
    }
    const due = held.find((h) => h.recheck <= Date.now());
    if (due) {
      this.request(due.window.from, due.window.to - due.window.from, true);
      return;
    }
    const last = held[held.length - 1].window;
    const margin = Math.min(
      Math.max(DETECTIONS_PREFETCH_MIN_MS, DETECTIONS_PREFETCH_PER_RATE_MS * rate),
      DETECTIONS_PREFETCH_MAX_MS,
    );
    if (clock < last.servedEnd - margin) return;
    // A third window waits until the clock has left the first, unless the clock is past both.
    if (held.length >= HELD_MAX && clock < last.servedEnd) return;
    this.request(last.servedEnd, this.length);
  }

  /** After a failure, asks again for what the clock needs. */
  retry(clock: number, rate: number): void {
    if (!this.on || !this.failed) return;
    this.failed = false;
    this.place(clock, rate, false);
  }

  /** The layer's state with the clock at `clock`; the same object while it is unchanged. */
  state(clock: number): ArchiveArcsState {
    let next: ArchiveArcsState;
    if (!this.on) {
      next = ARCS_OFF;
    } else {
      const serving = this.held.find((h) => h.window.from <= clock && clock < h.window.servedEnd);
      const local = serving ? serving.window.local : true;
      const status = this.failed ? "error" : !serving ? "loading" : serving.pending ? "pending" : "ready";
      next = { status, local };
    }
    if (next.status !== this.shown.status || next.local !== this.shown.local) this.shown = next;
    return this.shown;
  }

  /**
   * The detection_arcs of the frame at `clock`: the rows held with
   * lo < frame_ts_ms <= clock, where lo is the oldest instant whose arc still
   * shows, or on a frame that is no reset, the instant through which that
   * window's rows were already handed over. So no row goes out twice between
   * two resets, and a window that lands after the clock passed its rows
   * still hands over those in the afterglow.
   */
  arcsAt(
    clock: number,
    reset: boolean,
    aircraft: readonly ReplayAircraft[],
    nodesByRef: Record<string, NodeGeometry> | null | undefined,
  ): ReplayArc[] {
    const oldest = clock - ARC_TOTAL_LIFE_MS;
    const sources: ArcSource[] = [];
    for (const h of this.held) {
      const lo = reset ? oldest : Math.max(oldest, h.emitted);
      if (lo < clock) sources.push({ window: h.window, lo });
      h.emitted = reset ? clock : Math.max(h.emitted, clock);
    }
    if (!sources.length) return [];
    try {
      return detectionArcs(sources, clock, aircraft, nodesByRef);
    } catch {
      // Whatever the registry or a row held, the frame's tracks still go out.
      this.failed = true;
      return [];
    }
  }

  /** The map's stores were emptied by a frame that carried no arcs: every row held is due again. */
  cleared(): void {
    for (const h of this.held) h.emitted = -Infinity;
  }

  /* ---------------- requests ---------------- */

  /** Whether a window held, or the one out, serves `clock`. */
  private covers(clock: number): boolean {
    if (this.held.some((h) => h.window.from <= clock && clock < h.window.servedEnd)) return true;
    const out = this.out;
    return out !== null && out.from <= clock && clock < out.to;
  }

  private drop(): void {
    this.abort();
    this.held = [];
    this.failed = false;
    this.length = DETECTIONS_WINDOW_MS;
    this.schedule();
  }

  /**
   * Sets the wake for the first pending window held to fall due. None with
   * the layer off, a request out (its end sets it again) or after a failure
   * (a seek, a retry or a toggle asks then).
   */
  private schedule(): void {
    let due = Infinity;
    if (this.on && !this.out && !this.failed) for (const h of this.held) due = Math.min(due, h.recheck);
    if (due === this.wakeAt) return;
    if (this.wake !== null) clearTimeout(this.wake);
    this.wake = null;
    this.wakeAt = due;
    if (due === Infinity) return;
    this.wake = setTimeout(
      () => {
        this.wake = null;
        this.wakeAt = Infinity;
        this.place(this.host.clock(), this.host.rate(), false);
      },
      Math.max(0, due - Date.now()),
    );
  }

  private restart(clock: number): void {
    this.drop();
    this.request(firstWindowStart(clock), DETECTIONS_WINDOW_MS);
  }

  private abort(): void {
    const out = this.out;
    if (!out) return;
    this.out = null;
    this.gen++;
    out.controller.abort();
  }

  private request(from: number, length: number, again = false): void {
    this.abort();
    const gen = ++this.gen;
    const controller = new AbortController();
    const to = from + Math.min(length, DETECTIONS_WINDOW_MS);
    this.out = { gen, from, to, controller, again };
    let answer: Promise<DetectionsResponse>;
    try {
      // The explorer is the real fleet's, and no bbox: one window covers the world.
      answer = api.archiveDetections({ from, to, fleet: "real" }, { signal: controller.signal });
    } catch (err) {
      answer = Promise.reject(err);
    }
    answer.then(
      (resp) => this.landed(gen, from, to, again, resp),
      (err) => this.ended(gen, err),
    );
  }

  private landed(gen: number, from: number, to: number, again: boolean, resp: DetectionsResponse): void {
    if (gen !== this.gen) return;
    this.out = null;
    let window: DetectionWindow;
    try {
      window = buildDetectionWindow(resp, from, to);
    } catch {
      // The server answered, with a body that cannot be read: a failure all the same.
      this.failed = true;
      this.host.changed(null);
      this.schedule();
      return;
    }
    const clock = this.host.clock();
    this.held = this.held.filter((h) => h.window.servedEnd > clock - ARC_TOTAL_LIFE_MS);
    const pending = resp.pending === true;
    const entry: Held = {
      window,
      emitted: -Infinity,
      pending,
      recheck: pending ? recheckAt(to, Date.now(), again) : Infinity,
    };
    if (again) {
      const at = this.held.findIndex((h) => h.window.from === from && h.window.to === to);
      if (at < 0) {
        // Let go of while it was out: the clock has moved on without it.
        this.place(clock, this.host.rate(), false);
        this.host.changed(null);
        return;
      }
      // It takes the place of the one held. The next frame hands over its rows
      // in the afterglow, the ones already shown too, which the map takes
      // once. Serving less than before, the windows after it no longer go on
      // from where it ends, and are let go.
      if (window.servedEnd < this.held[at].window.servedEnd) this.held.splice(at + 1);
      this.held[at] = entry;
    } else {
      this.held.push(entry);
      if (this.held.length > HELD_MAX) this.held.splice(0, this.held.length - HELD_MAX);
    }
    // A cut answer: the next window starts at its last row and is no longer
    // than what this one served. A whole one: back to the widest.
    if (this.held[this.held.length - 1] === entry) {
      this.length = window.truncated
        ? Math.max(DETECTIONS_NARROW_FLOOR_MS, Math.floor((window.servedEnd - window.from) / 1000) * 1000)
        : DETECTIONS_WINDOW_MS;
    }
    // A cut short of the clock asks for the rest at once; so does a margin already reached.
    this.place(clock, this.host.rate(), false);
    this.host.changed(window);
  }

  private ended(gen: number, err: unknown): void {
    if (gen !== this.gen) return;
    this.out = null;
    // An abort only means the request was let go of: no failure.
    if (describeFailure(err).kind !== "aborted") this.failed = true;
    this.host.changed(null);
    this.schedule();
  }
}
