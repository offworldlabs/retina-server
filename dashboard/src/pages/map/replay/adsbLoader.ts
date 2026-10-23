/* ------------------------------------------------------------------ */
/*  Archive replay: the /api/data/adsb windows held around the clock  */
/*  while the ground-truth layer is shown, and the ADS-B traffic each */
/*  frame carries from them.                                          */
/*                                                                    */
/*  A best-effort layer: it asks for one window at a time, never      */
/*  retries on its own, and nothing it does touches the tracks.       */
/* ------------------------------------------------------------------ */

import { api } from "../../../api/client";
import { describeFailure } from "../../../utils/requestFailure";
import {
  ADSB_FRESH_MS,
  ADSB_LEAD_MS,
  adsbTraffic,
  buildAdsbWindow,
  type AdsbWindow,
  type Traffic,
  type TrafficCache,
} from "./adsbTraffic";
import type { AdsbResponse } from "./types";

/**
 * The ADS-B layer as the bar reports it. "loading": nothing held shows the
 * clock yet. "error": a request failed, and none is asked for again until a
 * seek away from the windows held, the layer is turned off and on, or the
 * replay is retried.
 */
export interface ArchiveAdsbState {
  status: "off" | "loading" | "ready" | "error";
}

export const ADSB_OFF: ArchiveAdsbState = Object.freeze({ status: "off" }) as ArchiveAdsbState;

/** The widest window the route answers (ADSB_WINDOW_MAX_MS). A jump asks for the one centred on the clock. */
export const ADSB_WINDOW_MS = 3_600_000;
/** After a cut answer the next window is as long as the stretch it served, but never shorter than this. */
export const ADSB_NARROW_FLOOR_MS = 60_000;
/** Playing, the next window is asked for this far (replay time) before the held ones end, */
export const ADSB_PREFETCH_MIN_MS = 60_000;
/** ... or this much per unit of rate, whichever is more, */
export const ADSB_PREFETCH_PER_RATE_MS = 15_000;
/** ... but never more than half the widest window. */
export const ADSB_PREFETCH_MAX_MS = ADSB_WINDOW_MS / 2;
/**
 * The server writes its snapshots out this often (ADSB_SNAPSHOT_FLUSH_INTERVAL_S).
 * Playing on from the end of what is held asks for more only once the
 * archive holds at least this much past it, so near the archive's edge one
 * window is not asked for again as soon as it lands.
 */
export const ADSB_FLUSH_MS = 60_000;
/** At most this many windows are held. */
const HELD_MAX = 2;

const NO_TRAFFIC = (): Traffic => ({ ground_truth: {}, ground_truth_meta: {} });

/** The session the loader serves: its clock and rate, the archive's edge, and what it hears back. */
export interface AdsbHost {
  clock(): number;
  rate(): number;
  /** The latest instant the archive holds in full. */
  edge(): number;
  /** A window landed (`landed`), or a request ended without one (null). */
  changed(landed: AdsbWindow | null): void;
}

interface Out {
  gen: number;
  from: number;
  to: number;
  controller: AbortController;
}

/** The window a jump to `clock` asks for: the widest, centred on it, starting on a whole second. */
export function adsbWindowStart(clock: number): number {
  return Math.floor((clock - ADSB_WINDOW_MS / 2) / 1000) * 1000;
}

export class AdsbLoader {
  private on = false;
  /** The windows held, each going on from the one before it. */
  private held: AdsbWindow[] = [];
  private out: Out | null = null;
  /** Where the windows asked for go on from: the end of the last one to land since the last start over. */
  private next: number | null = null;
  /** Bumped whenever a request stops being the current one; an answer of another generation is ignored. */
  private gen = 0;
  /** A request failed, and none is asked for again until something asks for one. */
  private failed = false;
  /** How long the next window asked for after the last one held is. */
  private length = ADSB_WINDOW_MS;
  private readonly cache: TrafficCache = new Map();
  private shown: ArchiveAdsbState = ADSB_OFF;

  constructor(private readonly host: AdsbHost) {}

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
   * instant the windows held and the one out do not cover starts over there;
   * one they cover asks for nothing. Playback asks for the window after the
   * held ones once the clock is within the prefetch margin of where they
   * end. Either way, a window no frame from the clock on reads is let go
   * first.
   */
  place(clock: number, rate: number, jumped: boolean): void {
    if (!this.on) return;
    this.letGo(clock);
    if (jumped) {
      if (!this.covers(clock)) this.restart(clock);
      return;
    }
    if (this.failed || this.out) return;
    const next = this.next;
    if (next === null || clock < this.start() + ADSB_FRESH_MS) {
      this.restart(clock);
      return;
    }
    const margin = Math.min(
      Math.max(ADSB_PREFETCH_MIN_MS, ADSB_PREFETCH_PER_RATE_MS * rate),
      ADSB_PREFETCH_MAX_MS,
    );
    if (clock < next - margin) return;
    // A third window waits until the clock has left the first, unless the clock is past both.
    if (this.held.length >= HELD_MAX && clock < next) return;
    // Near the archive's edge, not before it holds a flush period more.
    if (this.host.edge() - next < ADSB_FLUSH_MS) return;
    this.request(next, this.length);
  }

  /** After a failure, asks again for what the clock needs. */
  retry(clock: number, rate: number): void {
    if (!this.on || !this.failed) return;
    this.failed = false;
    this.place(clock, rate, false);
  }

  /**
   * Whether the windows held show `clock`: they hold every row a frame at it
   * needs to say which aircraft are there, from ADSB_FRESH_MS before it.
   * Trails reach back further (ADSB_LEAD_MS), and a frame near the start of
   * the first window hands over only the points it holds.
   */
  serves(clock: number): boolean {
    const held = this.held;
    return held.length > 0 && held[0].from + ADSB_FRESH_MS <= clock && clock < held[held.length - 1].servedEnd;
  }

  /** The layer's state with the clock at `clock`; the same object while it is unchanged. */
  state(clock: number): ArchiveAdsbState {
    const status = !this.on ? "off" : this.failed ? "error" : this.serves(clock) ? "ready" : "loading";
    if (status !== this.shown.status) this.shown = status === "off" ? ADSB_OFF : { status };
    return this.shown;
  }

  /**
   * The ground truth of the frame at `clock` (adsbTraffic): the aircraft the
   * archive held then, when the windows held show it; none otherwise, as
   * the layer cannot say which were there.
   */
  trafficAt(clock: number): Traffic {
    if (!this.on || !this.serves(clock)) return NO_TRAFFIC();
    try {
      return adsbTraffic(this.held, clock, this.cache);
    } catch {
      // Whatever a row held, the frame's tracks still go out.
      this.failed = true;
      return NO_TRAFFIC();
    }
  }

  /* ---------------- requests ---------------- */

  /** Where the windows held, or the one out when none is, start. */
  private start(): number {
    return this.held.length > 0 ? this.held[0].from : (this.out?.from ?? this.next ?? Infinity);
  }

  /** Whether the windows held and the one out, together, cover `clock`. */
  private covers(clock: number): boolean {
    const end = this.out ? this.out.to : this.held.length > 0 ? this.held[this.held.length - 1].servedEnd : -Infinity;
    return this.start() + ADSB_FRESH_MS <= clock && clock < end;
  }

  /** Lets go of the windows that end before any trail at `clock` reaches back to. */
  private letGo(clock: number): void {
    const before = this.held.length;
    this.held = this.held.filter((w) => w.servedEnd > clock - ADSB_LEAD_MS);
    if (this.held.length === before) return;
    for (const [hex, entry] of this.cache) if (!this.held.includes(entry.window)) this.cache.delete(hex);
  }

  private drop(): void {
    this.abort();
    this.held = [];
    this.next = null;
    this.failed = false;
    this.length = ADSB_WINDOW_MS;
    this.cache.clear();
  }

  private restart(clock: number): void {
    this.drop();
    this.request(adsbWindowStart(clock), ADSB_WINDOW_MS);
  }

  private abort(): void {
    const out = this.out;
    if (!out) return;
    this.out = null;
    this.gen++;
    out.controller.abort();
  }

  private request(from: number, length: number): void {
    this.abort();
    const gen = ++this.gen;
    const controller = new AbortController();
    const to = from + Math.min(length, ADSB_WINDOW_MS);
    this.out = { gen, from, to, controller };
    let answer: Promise<AdsbResponse>;
    try {
      // The explorer is the real fleet's, and no bbox: one window covers the world.
      answer = api.archiveAdsb({ from, to, fleet: "real" }, { signal: controller.signal });
    } catch (err) {
      answer = Promise.reject(err);
    }
    answer.then(
      (resp) => this.landed(gen, from, to, resp),
      (err) => this.ended(gen, err),
    );
  }

  private landed(gen: number, from: number, to: number, resp: AdsbResponse): void {
    if (gen !== this.gen) return;
    this.out = null;
    let window: AdsbWindow;
    try {
      window = buildAdsbWindow(resp, from, to, this.host.edge());
    } catch {
      // The server answered, with a body that cannot be read: a failure all the same.
      this.failed = true;
      this.host.changed(null);
      return;
    }
    const clock = this.host.clock();
    const last = this.held[this.held.length - 1];
    // Every window asked for goes on from the last one held, or starts over.
    if (last && last.servedEnd !== window.from) this.held = [];
    this.held.push(window);
    this.next = window.servedEnd;
    this.letGo(clock);
    if (this.held.length > HELD_MAX) this.held.splice(0, this.held.length - HELD_MAX);
    // A cut answer: the next window starts at its last snapshot and is no
    // longer than what this one served. A whole one: back to the widest.
    this.length = window.truncated
      ? Math.max(ADSB_NARROW_FLOOR_MS, Math.floor((window.servedEnd - window.from) / 1000) * 1000)
      : ADSB_WINDOW_MS;
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
  }
}
