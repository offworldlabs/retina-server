/* ------------------------------------------------------------------ */
/*  Archive replay: the /api/data/adsb windows held around the clock  */
/*  while the ground-truth layer is shown, and the ADS-B traffic each */
/*  frame carries from them.                                          */
/*                                                                    */
/*  A best-effort layer: it asks for one window at a time, and        */
/*  nothing it does touches the tracks. A drag of the scrubber away   */
/*  from the windows held asks once it settles, not on every seek it  */
/*  lands. A window the archive turned away for asking too often is   */
/*  asked for again once the wait it named is over. After any other   */
/*  failure nothing is asked for on its own.                          */
/* ------------------------------------------------------------------ */

import { api } from "../../../api/client";
import { describeFailure, retryDelayMs } from "../../../utils/requestFailure";
import {
  ADSB_FRESH_MS,
  ADSB_LEAD_MS,
  adsbTraffic,
  buildAdsbWindow,
  type AdsbWindow,
  type Traffic,
  type TrafficCache,
} from "./adsbTraffic";
import { SEEK_SETTLE_MS } from "./detectionsLoader";
import type { AdsbResponse } from "./types";

/**
 * The ADS-B layer as the bar reports it. "loading": nothing held shows the
 * clock yet. "waiting": nothing held shows the clock, and the archive turned
 * the request for it away for asking too often (a 429); it is asked for again
 * on its own once the wait it named is over. "ready": the windows held show
 * the clock, whatever became of a request for another window since. "error":
 * the traffic for the clock could not be built, or a request failed and
 * nothing held shows the clock. After a failure nothing is asked for again
 * until a seek away from the windows held, the layer is turned off and on, or
 * the replay is retried. While a drag of the scrubber holds back the start
 * over that asks for the clock's window, it is "loading", whether or not a
 * request failed before.
 */
export interface ArchiveAdsbState {
  status: "off" | "loading" | "waiting" | "ready" | "error";
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
  /**
   * Set once the archive has turned the request away (a 429): the timer that
   * asks for the same window again (see waitOut). Until it fires the request
   * stands as the one out, and whatever lets go of a request out lets go of it.
   */
  waiting: ReturnType<typeof setTimeout> | null;
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
  /**
   * A request failed, and none is asked for again until something asks for
   * one. Never a 429, which is waited out (waitOut).
   */
  private failed = false;
  /** The traffic for a frame could not be built from the windows held: an error whatever they serve. */
  private unbuilt = false;
  /** How long the next window asked for after the last one held is. */
  private length = ADSB_WINDOW_MS;
  private readonly cache: TrafficCache = new Map();
  private shown: ArchiveAdsbState = ADSB_OFF;
  /** The clock as last placed: an answer is weighed against it, not against a seek still to land. */
  private placedClock = Number.NaN;
  /** When (Date.now()) the last jump came, while the layer was on. */
  private jumpedAt = -Infinity;
  /**
   * The clock has not been placed since the layer was turned on or archive
   * mode was left: the jump that places it is no seek of the scrubber, so a
   * seek right after it is one on its own.
   */
  private unplaced = true;
  /** The last jump came within SEEK_SETTLE_MS of the one before it: a drag, until SEEK_SETTLE_MS passes with no jump. */
  private dragged = false;
  /** The last jump, in a drag, asks to start over once the drag settles. */
  private heldBack = false;
  /** Does, once the drag settles, what it put off (see `defer`). */
  private settle: ReturnType<typeof setTimeout> | null = null;

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
    this.undrag();
    this.drop();
  }

  /**
   * Leaving archive mode: nothing out and nothing held. The layer stays on,
   * so a session that resumes (React.StrictMode) asks again on its next jump.
   */
  dispose(): void {
    this.undrag();
    this.drop();
  }

  /**
   * The clock is at `clock`. A jump (a link, a landed seek or step) to an
   * instant the windows held and the one out do not cover starts over there;
   * one they cover asks for nothing. Playback asks for the window after the
   * held ones once the clock is within the prefetch margin of where they
   * end. Either way, a window no frame from the clock on reads is let go
   * first.
   *
   * A jump within SEEK_SETTLE_MS of the one before it is a drag of the
   * scrubber, which goes on until SEEK_SETTLE_MS passes with no jump: the
   * session lands a dragged seek every 200 ms, and on a scrubber wider than
   * the hour held, starting over on each would spend the archive's read
   * limit in seconds. A drag's start over, and whatever play or an answer
   * would ask for meanwhile, wait until it settles, and it lets go of nothing
   * it passes, since it may come back. The jump that places the clock once
   * the layer is on, or once archive mode is entered again, is no seek and
   * starts no drag.
   */
  place(clock: number, rate: number, jumped: boolean): void {
    if (!this.on) return;
    this.placedClock = clock;
    if (jumped) {
      const wall = Date.now();
      this.dragged = wall - this.jumpedAt < SEEK_SETTLE_MS;
      if (!this.unplaced) this.jumpedAt = wall;
      this.unplaced = false;
      // Each jump decides afresh: what the drag put off was for where the clock was before it.
      this.unhold();
      this.jump(clock, this.dragged);
      return;
    }
    if (this.dragging()) {
      this.defer();
      return;
    }
    this.letGo(clock);
    // After a failure nothing is asked for on its own: the windows held show
    // what they hold until the clock leaves them, and state says why no more
    // comes. One out, or waiting to go out again, is the one request at a time.
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
    this.unbuilt = false;
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
    let status: ArchiveAdsbState["status"];
    if (!this.on) status = "off";
    else if (this.unbuilt) status = "error";
    // What the clock needs is in, whatever became of a request since.
    else if (this.serves(clock)) status = "ready";
    // A drag holds back the start over that asks for it.
    else if (this.heldBack) status = "loading";
    // Nothing held shows the clock, and after a failure nothing is asked for on its own.
    else if (this.failed) status = "error";
    // Nothing held shows the clock: why not.
    else if (this.out !== null && this.out.waiting !== null) status = "waiting";
    else status = "loading";
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
      this.unbuilt = true;
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

  /** A jump to `clock`, in a drag or not (see `place`). */
  private jump(clock: number, dragged: boolean): void {
    if (!dragged) this.letGo(clock);
    if (this.covers(clock)) return;
    if (dragged) this.holdBack();
    else this.restart(clock);
  }

  /**
   * The start over a jump in a drag asks for waits until the drag settles.
   * Meanwhile the windows held stay, and so does a request on its way, since
   * the drag may come back to either; one waiting out a 429 is let go of, as
   * a jump away lets go of it.
   */
  private holdBack(): void {
    this.heldBack = true;
    if (this.out !== null && this.out.waiting !== null) this.abort();
    this.defer();
  }

  /**
   * Whether a drag goes on: its last jump came within SEEK_SETTLE_MS of the
   * one before, and less than that ago, or what it put off is not done yet.
   */
  private dragging(): boolean {
    return this.settle !== null || (this.dragged && Date.now() - this.jumpedAt < SEEK_SETTLE_MS);
  }

  /**
   * Once the drag settles, SEEK_SETTLE_MS after its last jump, with the clock
   * where it is then: the start over held back, as a jump on its own there
   * would; otherwise what play or an answer would have asked for meanwhile,
   * the window after the held ones included, so play goes on into it.
   */
  private defer(): void {
    if (this.settle !== null) return;
    this.settle = setTimeout(
      () => {
        this.settle = null;
        const clock = this.host.clock();
        if (this.heldBack) {
          this.heldBack = false;
          this.placedClock = clock;
          this.jump(clock, false);
        } else {
          this.place(clock, this.host.rate(), false);
        }
        this.host.changed(null);
      },
      Math.max(0, this.jumpedAt + SEEK_SETTLE_MS - Date.now()),
    );
  }

  /** What a drag put off goes: a jump decides afresh, and a restart or the layer turned off asks for all of it again. */
  private unhold(): void {
    this.heldBack = false;
    if (this.settle === null) return;
    clearTimeout(this.settle);
    this.settle = null;
  }

  /** No drag goes on: the next jump, and a seek right after it, are each one on its own. */
  private undrag(): void {
    this.jumpedAt = -Infinity;
    this.dragged = false;
    this.unplaced = true;
  }

  private drop(): void {
    this.abort();
    this.unhold();
    this.held = [];
    this.next = null;
    this.failed = false;
    this.unbuilt = false;
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
    if (out.waiting !== null) clearTimeout(out.waiting);
    out.controller.abort();
  }

  private request(from: number, length: number): void {
    this.abort();
    const gen = ++this.gen;
    const controller = new AbortController();
    const to = from + Math.min(length, ADSB_WINDOW_MS);
    this.out = { gen, from, to, controller, waiting: null };
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
    // Weighed against the clock as last placed: a dragged seek still to land
    // is weighed once it lands. A drag lets go of nothing it passes.
    const clock = this.placedClock;
    const last = this.held[this.held.length - 1];
    // Every window asked for goes on from the last one held, or starts over.
    if (last && last.servedEnd !== window.from) this.held = [];
    this.held.push(window);
    this.next = window.servedEnd;
    if (!this.dragging()) this.letGo(clock);
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
    const out = this.out;
    const { kind } = describeFailure(err);
    if (out && kind === "throttled" && !this.heldBack) {
      this.waitOut(out, retryDelayMs(err));
      return;
    }
    this.out = null;
    // An abort only means the request was let go of: no failure. Nor does a
    // 429 while a drag holds back a start over: the clock has left what it
    // asked for, so it is let go of, as a jump away lets go of one waiting.
    if (kind !== "aborted" && kind !== "throttled") this.failed = true;
    this.host.changed(null);
  }

  /**
   * The archive turned the request for `out` away for asking too often (a
   * 429): it asks for the same window again on its own after `delayMs`. Until
   * then it stays the one out, so playback asks for nothing else and a jump it
   * covers waits for it; the windows held go on serving. A jump away, the
   * layer turned off or leaving archive mode lets go of it (abort).
   */
  private waitOut(out: Out, delayMs: number): void {
    out.waiting = setTimeout(() => {
      out.waiting = null;
      if (this.out !== out) return;
      this.out = null;
      this.request(out.from, out.to - out.from);
      this.host.changed(null);
    }, delayMs);
    this.host.changed(null);
  }
}
