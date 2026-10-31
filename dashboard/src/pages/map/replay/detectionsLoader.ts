/* ------------------------------------------------------------------ */
/*  Archive replay: the /api/data/detections windows held around the  */
/*  clock while the arcs layer is shown, and which of their rows each */
/*  frame still has to hand the map.                                  */
/*                                                                    */
/*  A best-effort layer: it asks for one window at a time, and        */
/*  nothing it does touches the tracks. The windows held go on from   */
/*  one another: play asks for the one after them, and a seek back    */
/*  from them for the one before, so a scrub either way asks only for */
/*  the stretches it enters. A drag of the scrubber anywhere else     */
/*  asks once it settles, not on every seek it lands. A window the    */
/*  server says it may not have archived in full yet is asked for     */
/*  again once it can have been, and one it turned away for asking    */
/*  too often once the wait it named is over. After a failure nothing */
/*  is asked for on its own, neither the failed window nor a pending  */
/*  one.                                                              */
/* ------------------------------------------------------------------ */

import { api } from "../../../api/client";
import { describeFailure, retryDelayMs } from "../../../utils/requestFailure";
import type { NodeGeometry } from "../bistaticArc";
import { ARC_TOTAL_LIFE_MS } from "../constants";
import { buildDetectionWindow, detectionArcs, type ArcSource, type DetectionWindow } from "./detectionArcs";
import type { DetectionsResponse, ReplayAircraft, ReplayArc } from "./types";

/**
 * The arcs layer as the bar reports it. "loading": the window that serves
 * the clock is out. "waiting": no window held serves the clock, and the
 * archive turned the request for one away for asking too often (a 429); it
 * is asked for again on its own once the wait it named is over. "ready": it
 * is in, all of it, whatever became of a request for another window since.
 * "pending": it is in, but the server said it may not have archived all of
 * it yet (it does so hourly), so arcs may still come; it is asked for again
 * once they can have (a 429 only puts that off), and this is said only while
 * no request has failed. "error": the arcs for the clock could not be built,
 * or a request failed and no window held serves the clock in full: none
 * does, or the one that does is pending and, after a failure, is not asked
 * for again either. Nothing is asked for again until a seek away from the
 * windows held, the layer is turned off and on, or the replay is retried.
 * `local` is false when the answer serving the clock said the server may
 * already have deleted some of its window's detections from its disk.
 * While a drag of the scrubber holds back the start over that asks for the
 * clock's window, it is "loading", whether or not a request failed before.
 */
export interface ArchiveArcsState {
  status: "off" | "loading" | "waiting" | "ready" | "pending" | "error";
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
/**
 * At most this many windows are held: past it, the one the clock has left
 * goes, or on a seek back from them the one furthest ahead.
 */
const HELD_MAX = 2;
/**
 * A jump this soon after the last one is part of a drag of the scrubber, and
 * a drag has settled once this long passes with no jump. The session lands a
 * dragged seek every SEEK_COALESCE_MS (200 ms), so a drag keeps landing them
 * this close, with 100 ms to spare for a timer that fires late. The session
 * reads a drag by the same measure for the tracks.
 */
export const SEEK_SETTLE_MS = 300;
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
  /** A window landed (`landed`), or the layer's state changed without one (null). */
  changed(landed: DetectionWindow | null): void;
}

interface Held {
  window: DetectionWindow;
  /** The instant through which its rows have been handed to the map since the last reset. */
  emitted: number;
  /** The server said it may not have archived all of the window yet. */
  pending: boolean;
  /**
   * When (Date.now()) a pending window is asked for again, unless a request
   * has failed by then; Infinity for one that is not.
   */
  recheck: number;
}

interface Out {
  gen: number;
  from: number;
  to: number;
  controller: AbortController;
  /** It asks again for a window held. */
  again: boolean;
  /**
   * Set once the archive has turned the request away (a 429): the timer that
   * asks for the same window again (see waitOut). Until it fires the request
   * stands as the one out, and whatever lets go of a request out lets go of it.
   */
  waiting: ReturnType<typeof setTimeout> | null;
}

/** The first window for `clock`: it starts a whole second at or before the oldest instant whose arcs still show. */
export function firstWindowStart(clock: number): number {
  return Math.floor((clock - ARC_TOTAL_LIFE_MS) / 1000) * 1000;
}

/** How long a window asked for next to a cut one is: as long as the stretch it served, in whole seconds, but no shorter than the floor. */
function narrowed(window: DetectionWindow): number {
  return Math.max(DETECTIONS_NARROW_FLOOR_MS, Math.floor((window.servedEnd - window.from) / 1000) * 1000);
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
  /** The arcs for a frame could not be built from the windows held: an error whatever they serve. */
  private unbuilt = false;
  /** How long the next window asked for after the last one held is. */
  private length = DETECTIONS_WINDOW_MS;
  /**
   * How long the window asked for before the first one held is: after one
   * came back cut, as long as the stretch it served, until the next start
   * over. A cut leaves out a window's last rows, the ones nearest the windows
   * held, so a window too long for the stretch before them would leave a gap
   * to ask for on every seek back.
   */
  private backLength = DETECTIONS_WINDOW_MS;
  private shown: ArchiveArcsState = ARCS_OFF;
  /** Wakes the loader when a pending window held is due to be asked for again, at `wakeAt`. */
  private wake: ReturnType<typeof setTimeout> | null = null;
  private wakeAt = Infinity;
  /** The clock as last placed: an answer is weighed against it, not against a seek still to land. */
  private placedClock = Number.NaN;
  /** When (Date.now()) the last jump came, while the layer was on. */
  private jumpedAt = -Infinity;
  /** The last jump came within SEEK_SETTLE_MS of the one before it: a drag, until SEEK_SETTLE_MS passes with no jump. */
  private dragged = false;
  /** The last jump, in a drag, asks to start over once the drag settles. */
  private heldBack = false;
  /** Does, once the drag settles, what it put off (see `defer`). */
  private settle: ReturnType<typeof setTimeout> | null = null;

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
   * instant inside a window held or the one out asks for nothing. One just
   * before the windows held, within a window of where they start, asks for
   * the window that ends there and keeps them (see `before`); one just
   * before a window still on its way back from them waits for it, and the
   * window before it is asked for once it lands. A jump anywhere else
   * starts over there, as does any jump after a failure. Otherwise a
   * pending window held that is due is asked for again, and playback asks
   * for the next window once the clock is within the prefetch margin of
   * where the held ones end. Either way, a window whose rows have all aged
   * out of the afterglow is let go first.
   *
   * A jump within SEEK_SETTLE_MS of the one before it is a drag of the
   * scrubber, which goes on until SEEK_SETTLE_MS passes with no jump: the
   * session lands a dragged seek every 200 ms, and starting over on each
   * would spend the archive's read limit in seconds. A drag asks at once only
   * for the window before the windows held. Its start over, and whatever
   * play or an answer would ask for meanwhile, wait until it settles, and it
   * lets go of nothing it passes, since it may come back.
   */
  place(clock: number, rate: number, jumped: boolean): void {
    this.placed(clock, rate, jumped);
    this.schedule();
  }

  private placed(clock: number, rate: number, jumped: boolean): void {
    if (!this.on) return;
    this.placedClock = clock;
    if (jumped) {
      const wall = Date.now();
      this.dragged = wall - this.jumpedAt < SEEK_SETTLE_MS;
      this.jumpedAt = wall;
      // Each jump decides afresh: what the drag put off was for where the clock was before it.
      this.unhold();
      this.jump(clock, this.dragged);
      return;
    }
    if (this.dragging()) {
      this.defer();
      return;
    }
    this.age(clock);
    // After a failure nothing is asked for on its own, a pending window held
    // included: the windows held hand over the rows they have until the clock
    // leaves them, and state says why no more come. One out, or waiting to go
    // out again, is the one request at a time.
    if (this.failed || this.out) return;
    const held = this.held;
    if (held.length === 0 || clock < held[0].window.from) {
      this.reach(clock);
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
    this.unbuilt = false;
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
      let status: ArchiveArcsState["status"];
      if (this.unbuilt) status = "error";
      // All its arcs are in, whatever became of a request since.
      else if (serving && !serving.pending) status = "ready";
      // A drag holds back the start over that asks for them.
      else if (this.heldBack && !serving) status = "loading";
      // Nothing held serves the clock in full, and after a failure nothing is
      // asked for on its own, a pending window's next ask included.
      else if (this.failed) status = "error";
      // Arcs may still come: it is asked for again once they can have.
      else if (serving) status = "pending";
      // Nothing held serves the clock: why not.
      else if (this.out !== null && this.out.waiting !== null) status = "waiting";
      else status = "loading";
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
      this.unbuilt = true;
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
    this.unhold();
    this.held = [];
    this.failed = false;
    this.unbuilt = false;
    this.length = DETECTIONS_WINDOW_MS;
    this.backLength = DETECTIONS_WINDOW_MS;
    this.schedule();
  }

  /**
   * Sets the wake for the first pending window held to fall due. None with
   * the layer off, a request out (its end sets it again), a drag's settling
   * due (which sets it again) or after a failure (a seek, a retry or a toggle
   * asks then).
   */
  private schedule(): void {
    let due = Infinity;
    if (this.on && !this.out && !this.failed && this.settle === null) {
      for (const h of this.held) due = Math.min(due, h.recheck);
    }
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

  /**
   * Where the window for a clock before the windows held starts: it ends
   * where they start, is `backLength` long, and serves the clock when it
   * reaches back as far as the first window for the clock would
   * (firstWindowStart). Null when the clock is not before them, or further
   * back than that.
   */
  private before(clock: number): number | null {
    const first = this.held[0];
    if (!first || clock >= first.window.from) return null;
    const from = first.window.from - this.backLength;
    return from <= firstWindowStart(clock) ? from : null;
  }

  /**
   * Whether a jump to `clock`, which nothing held or out serves, waits for
   * the one out: no request has failed, it is on its way (not waiting out a
   * 429), it is the window before the windows held or there are none, and
   * the window that would go before it serves the clock. Asking for the
   * clock's own window instead would let go of it, and a scrub back that
   * outruns the answers would then start over on every seek.
   */
  private awaits(clock: number): boolean {
    const out = this.out;
    if (this.failed || out === null || out.waiting !== null || clock >= out.from) return false;
    const first = this.held[0];
    if (first && out.to !== first.window.from) return false;
    return out.from - this.backLength <= firstWindowStart(clock);
  }

  /** A jump to `clock`, in a drag or not (see `place`). */
  private jump(clock: number, dragged: boolean): void {
    if (!dragged) this.age(clock);
    if (!this.covers(clock) && !this.awaits(clock)) this.reach(clock, dragged);
  }

  /** Lets go of the windows held whose rows have all aged out of the afterglow at `clock`. */
  private age(clock: number): void {
    this.held = this.held.filter((h) => h.window.servedEnd > clock - ARC_TOTAL_LIFE_MS);
  }

  /**
   * Asks for a window that serves the clock, which nothing held or out
   * serves: the one before the windows held when it does (see `before`),
   * keeping them; otherwise the first window for the clock, in place of all
   * of them, which a jump in a drag holds back until the drag settles. After
   * a failure it is always the latter: a seek away then starts over, which
   * is what has anything asked for again.
   */
  private reach(clock: number, dragged = false): void {
    const from = this.failed ? null : this.before(clock);
    if (from !== null) this.request(from, this.backLength);
    else if (dragged) this.holdBack();
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
   * would; otherwise what play or an answer would have asked for meanwhile.
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
          this.placed(clock, this.host.rate(), false);
        }
        this.schedule();
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

  /** No drag goes on: the next jump is one on its own. */
  private undrag(): void {
    this.jumpedAt = -Infinity;
    this.dragged = false;
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
    if (out.waiting !== null) clearTimeout(out.waiting);
    out.controller.abort();
  }

  private request(from: number, length: number, again = false): void {
    this.abort();
    const gen = ++this.gen;
    const controller = new AbortController();
    const to = from + Math.min(length, DETECTIONS_WINDOW_MS);
    this.out = { gen, from, to, controller, again, waiting: null };
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
    // Weighed against the clock as last placed: a dragged seek still to land
    // is weighed once it lands. A drag lets go of nothing it passes.
    const clock = this.placedClock;
    const dragging = this.dragging();
    if (!dragging) this.age(clock);
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
    } else if (this.held.length > 0 && to <= this.held[0].window.from) {
      // Asked for on a seek back from the windows held.
      const start = this.held[0].window.from;
      if (window.truncated) this.backLength = narrowed(window);
      if (window.servedEnd >= start) {
        // Whole: they go on from it, and past HELD_MAX the one furthest
        // ahead is let go. A drag keeps it though the clock has passed it.
        if (dragging || window.servedEnd > clock - ARC_TOTAL_LIFE_MS) {
          this.held.unshift(entry);
          this.held.splice(HELD_MAX);
        }
      } else if (clock < start) {
        if (window.servedEnd > clock - ARC_TOTAL_LIFE_MS) {
          // Cut short of them, with rows the clock shows: they no longer go
          // on from it and are let go, as the windows after one asked for
          // again that serves less, and play goes on from its last row.
          this.held = [entry];
        } else {
          // Cut short of the clock: nothing of it would show, and the
          // stretch from its last row to where they start serves the clock.
          this.request(window.servedEnd, start - window.servedEnd);
        }
      }
      // Otherwise the clock has moved on into them while it was out, and they serve it.
    } else {
      this.held.push(entry);
      if (this.held.length > HELD_MAX) this.held.splice(0, this.held.length - HELD_MAX);
    }
    // A cut answer: the next window starts at its last row and is no longer
    // than what this one served. A whole one: back to the widest.
    if (this.held[this.held.length - 1] === entry) {
      this.length = window.truncated ? narrowed(window) : DETECTIONS_WINDOW_MS;
    }
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
    this.schedule();
  }

  /**
   * The archive turned the request for `out` away for asking too often (a
   * 429): it asks for the same window again on its own after `delayMs`. Until
   * then it stays the one out, so playback asks for nothing else, a jump it
   * covers waits for it, and no pending window held is asked for again
   * sooner; the windows held go on serving. A jump away, the layer turned off
   * or leaving archive mode lets go of it (abort).
   */
  private waitOut(out: Out, delayMs: number): void {
    out.waiting = setTimeout(() => {
      out.waiting = null;
      if (this.out !== out) return;
      this.out = null;
      this.request(out.from, out.to - out.from, out.again);
      this.host.changed(null);
    }, delayMs);
    this.host.changed(null);
  }
}
