/* ------------------------------------------------------------------ */
/*  Archive replay: which hours of the tracks archive hold solves,    */
/*  for the availability strip. A week around the replay instant to   */
/*  start with, extended as the clock or the strip's view moves, and  */
/*  today's hours asked for again as the replay plays into hours that */
/*  were not archived yet when they were first asked for.             */
/* ------------------------------------------------------------------ */

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { HttpError } from "@retina/shared";

import { api } from "../../../api/client";
import type { AvailabilityHour, AvailabilityResponse } from "./types";
import type { LoadStatus } from "./useArchiveReplay";

export interface TrackAvailability {
  /** Non-empty hours of the held range, ascending. */
  hours: AvailabilityHour[];
  /** The UTC days the hours cover, inclusive; null before the first answer. */
  range: { from: string; to: string } | null;
  status: LoadStatus;
  error: string | null;
  /**
   * The strip's view settled on this instant, at this zoom: 0 an hour across,
   * 1 a day, 2 a month. No zoom reads as the day view.
   */
  onViewCenter(centerMs: number, zoom?: number): void;
}

const DAY_MS = 86_400_000;
const HOUR_MS = 3_600_000;
/** Days fetched either side of the clock's day, and of the centre of a view up to a day across. */
export const AVAILABILITY_RADIUS_DAYS = 3;
/** The strip's zoom from which a view is a month across. */
export const AVAILABILITY_MONTH_ZOOM = 2;
/** Days a month-wide view shows either side of its centre, rounded up. */
export const AVAILABILITY_MONTH_RADIUS_DAYS = 16;
/** A view centred this close to an edge of the held range extends it. */
export const AVAILABILITY_EDGE_DAYS = 1;
/** The most days held at once (the route's own cap on a request). */
export const AVAILABILITY_MAX_DAYS = 92;
/** Today's hours are asked for again at most this often, in wall time. */
export const AVAILABILITY_REFRESH_MS = 60_000;

/** Days are whole UTC days, counted from the epoch. */
interface DayRange {
  readonly from: number;
  readonly to: number;
}

/**
 * Days around `day` wanted on the strip. The clock's are there once its day is
 * held; a view's once its centre is also clear of the held edges, and for a
 * month-wide view once every day of it is held too.
 */
interface Need {
  day: number;
  /** Days wanted either side of `day`, never past today. */
  radius: number;
  kind: "clock" | "view" | "month";
}

interface Plan {
  /** The days to ask the route for. */
  request: DayRange;
  /** The days held once the answer lands. */
  result: DayRange;
  /** The answer replaces the held hours rather than joining them. */
  replace: boolean;
  /**
   * Today asked for again: the answer replaces the held hours of the requested
   * day, and the strip is not told it is loading.
   */
  refresh?: boolean;
}

interface InFlight {
  gen: number;
  plan: Plan;
  controller: AbortController;
}

type Snapshot = Omit<TrackAvailability, "onViewCenter">;

function dayOf(ms: number): number {
  return Math.floor(ms / DAY_MS);
}

function dayString(day: number): string {
  return new Date(day * DAY_MS).toISOString().slice(0, 10);
}

function covers(range: DayRange, day: number): boolean {
  return day >= range.from && day <= range.to;
}

/**
 * What it takes to meet `need`: nothing when `base` already does, otherwise the
 * days around it, joined to `base` when the two make one run of at most
 * AVAILABILITY_MAX_DAYS, else in its place. Never past today.
 */
function planFor({ day, radius, kind }: Need, base: DayRange | null, today: number): Plan | null {
  const to = Math.min(day + radius, today);
  const target = { from: Math.min(day - radius, to), to };
  if (base) {
    const inside = covers(base, day);
    const clearOfEdges = day - base.from > AVAILABILITY_EDGE_DAYS && base.to - day > AVAILABILITY_EDGE_DAYS;
    const holdsTarget = target.from >= base.from && target.to <= base.to;
    if (inside && (kind === "clock" || (clearOfEdges && (kind === "view" || holdsTarget)))) return null;
    const contiguous = target.from <= base.to + 1 && target.to >= base.from - 1;
    const union = { from: Math.min(base.from, target.from), to: Math.max(base.to, target.to) };
    if (contiguous && union.to - union.from + 1 <= AVAILABILITY_MAX_DAYS) {
      const left = target.from < base.from;
      const right = target.to > base.to;
      if (!left && !right) return null;
      const request = left && right ? target : left ? { from: target.from, to: base.from - 1 } : { from: base.to + 1, to: target.to };
      return { request, result: union, replace: false };
    }
  }
  return { request: target, result: target, replace: true };
}

/** A short reason for the strip: "Couldn't load availability: <this>". */
export function availabilityErrorOf(err: unknown): string {
  if (err instanceof HttpError) {
    const detail = (err.body as { detail?: unknown } | null)?.detail;
    return typeof detail === "string" && detail ? detail : `HTTP ${err.status}`;
  }
  if ((err as { name?: unknown } | null)?.name === "TimeoutError") return "timed out";
  return "network error";
}

function isAbort(err: unknown): boolean {
  return (err as { name?: unknown } | null)?.name === "AbortError";
}

class AvailabilityStore {
  private held: DayRange | null = null;
  private hours: AvailabilityHour[] = [];
  private status: LoadStatus = "idle";
  private error: string | null = null;
  private inflight: InFlight | null = null;
  private gen = 0;
  /** When the last answer covering today landed, wall ms; null before one has. */
  private todayAnsweredAt: number | null = null;
  /** When today was last asked for, wall ms. */
  private todayAskedAt = Number.NEGATIVE_INFINITY;

  private snapshot: Snapshot = { hours: this.hours, range: null, status: "idle", error: null };
  private readonly watchers = new Set<() => void>();

  subscribe = (onChange: () => void): (() => void) => {
    this.watchers.add(onChange);
    return () => {
      this.watchers.delete(onChange);
    };
  };

  getSnapshot = (): Snapshot => this.snapshot;

  /** The replay clock is on `day`: hold the days around it. */
  showDay = (day: number): void => {
    this.need({ day, radius: AVAILABILITY_RADIUS_DAYS, kind: "clock" });
  };

  viewCenter = (centerMs: number, zoom?: number): void => {
    if (!Number.isFinite(centerMs)) return;
    const month = typeof zoom === "number" && zoom >= AVAILABILITY_MONTH_ZOOM;
    this.need({
      day: dayOf(centerMs),
      radius: month ? AVAILABILITY_MONTH_RADIUS_DAYS : AVAILABILITY_RADIUS_DAYS,
      kind: month ? "month" : "view",
    });
  };

  /**
   * The replay clock is at `clockMs`. Today's hours were complete only up to
   * when they were fetched: once the clock plays into that hour, ask for today
   * again, at most once a minute, and only while nothing else is out.
   */
  clockAt = (clockMs: number): void => {
    const held = this.held;
    const answeredAt = this.todayAnsweredAt;
    if (this.inflight || !held || answeredAt === null || !Number.isFinite(clockMs)) return;
    const now = Date.now();
    const today = dayOf(now);
    if (!covers(held, today)) return;
    if (clockMs < Math.floor(answeredAt / HOUR_MS) * HOUR_MS) return;
    if (now - this.todayAskedAt < AVAILABILITY_REFRESH_MS) return;
    this.request({ request: { from: today, to: today }, result: held, replace: false, refresh: true });
  };

  dispose = (): void => {
    this.abort();
    this.held = null;
    this.hours = [];
    this.status = "idle";
    this.error = null;
    this.todayAnsweredAt = null;
    this.todayAskedAt = Number.NEGATIVE_INFINITY;
    this.publish();
  };

  /**
   * The latest need wins: a request out that will not cover it is dropped,
   * and its answer with it.
   */
  private need(need: Need): void {
    const today = dayOf(Date.now());
    const out = this.inflight;
    if (out && planFor(need, out.plan.result, today) === null) return;
    const plan = planFor(need, this.held, today);
    if (plan) {
      this.request(plan);
    } else if (out && out.plan.replace) {
      // The held days answer it, and the answer out would have replaced them.
      this.abort();
      this.status = this.held ? "ready" : "idle";
    }
    this.publish();
  }

  private request(plan: Plan): void {
    this.abort();
    const gen = ++this.gen;
    const controller = new AbortController();
    this.inflight = { gen, plan, controller };
    const now = Date.now();
    if (covers(plan.request, dayOf(now))) this.todayAskedAt = now;
    if (!plan.refresh) {
      this.status = "loading";
      this.error = null;
    }
    let answer: Promise<AvailabilityResponse>;
    try {
      answer = api.trackAvailability(
        { from: dayString(plan.request.from), to: dayString(plan.request.to), fleet: "real" },
        { signal: controller.signal },
      );
    } catch (err) {
      answer = Promise.reject(err);
    }
    answer.then(
      (resp) => this.landed(gen, plan, resp),
      (err) => this.failed(gen, plan, err),
    );
  }

  private abort(): void {
    const out = this.inflight;
    if (!out) return;
    this.inflight = null;
    this.gen++;
    out.controller.abort();
  }

  private landed(gen: number, plan: Plan, resp: AvailabilityResponse): void {
    if (gen !== this.gen) return;
    this.inflight = null;
    const byStart = new Map<number, AvailabilityHour>();
    if (!plan.replace) {
      for (const hour of this.hours) {
        if (!(plan.refresh && covers(plan.request, dayOf(hour.start_ms)))) byStart.set(hour.start_ms, hour);
      }
    }
    for (const hour of resp?.hours ?? []) byStart.set(hour.start_ms, hour);
    this.hours = [...byStart.values()].sort((a, b) => a.start_ms - b.start_ms);
    this.held = plan.result;
    const now = Date.now();
    if (covers(plan.request, dayOf(now))) this.todayAnsweredAt = now;
    this.status = "ready";
    this.error = null;
    this.publish();
  }

  private failed(gen: number, plan: Plan, err: unknown): void {
    if (gen !== this.gen) return;
    this.inflight = null;
    if (isAbort(err)) {
      // A refresh never said it was loading, so there is nothing to undo.
      if (!plan.refresh) this.status = this.held ? "ready" : "idle";
    } else {
      this.status = "error";
      this.error = availabilityErrorOf(err);
    }
    this.publish();
  }

  private publish(): void {
    const s = this.snapshot;
    const held = this.held;
    const rangeSame =
      held === null ? s.range === null : s.range !== null && s.range.from === dayString(held.from) && s.range.to === dayString(held.to);
    if (rangeSame && s.hours === this.hours && s.status === this.status && s.error === this.error) return;
    this.snapshot = {
      hours: this.hours,
      range: rangeSame ? s.range : held && { from: dayString(held.from), to: dayString(held.to) },
      status: this.status,
      error: this.error,
    };
    for (const onChange of this.watchers) onChange();
  }
}

const noWatch = () => () => {};
const NOOP = () => {};
const IDLE: Snapshot = { hours: [], range: null, status: "idle", error: null };

function idleSnapshot(): Snapshot {
  return IDLE;
}

/**
 * The availability strip's data. While `active`, holds the UTC days around the
 * replay clock (a week, never past today), follows the clock to another day,
 * extends or moves the held days when the strip's view settles near or past
 * their edge or, zoomed out to a month, on days they leave out, and asks for
 * today's hours again as the clock plays into the hour they were fetched in.
 */
export function useTrackAvailability({ active, clockMs }: { active: boolean; clockMs: number }): TrackAvailability {
  const [store, setStore] = useState<AvailabilityStore | null>(() => (active ? new AvailabilityStore() : null));
  if (active !== (store !== null)) setStore(active ? new AvailabilityStore() : null);

  const snapshot = useSyncExternalStore(store?.subscribe ?? noWatch, store?.getSnapshot ?? idleSnapshot);
  const clockDay = dayOf(clockMs);

  useEffect(() => (store ? store.dispose : undefined), [store]);
  useEffect(() => {
    if (Number.isFinite(clockDay)) store?.showDay(clockDay);
  }, [store, clockDay]);
  useEffect(() => {
    store?.clockAt(clockMs);
  }, [store, clockMs]);

  return useMemo(() => ({ ...snapshot, onViewCenter: store ? store.viewCenter : NOOP }), [snapshot, store]);
}
