import { useCallback, useEffect, useMemo, useRef, useState } from "react";

/**
 * Two-way sync between a small bag of UI state and `location.hash`.
 *
 * Goals
 * -----
 *  * Shareable deep links — "give a teammate this URL and they land on
 *    the exact same view + selected aircraft."
 *  * Survives reload without trampling the user's preference cookies.
 *  * Lossy by design — only a handful of keys are encoded so the hash
 *    stays short and stable across feature additions.
 *
 * The hash format is `#k=v&k=v` (same syntax as a query string, sans `?`).
 * Unknown keys are ignored on read so old links keep working when fields
 * are removed, and a value that does not decode skips only its own key.
 *
 * Archive keys
 * ------------
 * `t` is the archive instant and `span` the replay window's radius in
 * minutes: `#lat=34.85&lon=-82.39&z=10&t=2026-10-18T22:14:00Z`. A hash with a
 * valid `t` opens the map on the archive; without one the map is live, and
 * the hash reads and writes exactly as it did before the archive existed.
 */

export interface HashState {
  /** Map centre lat (6dp) */
  lat?: number;
  /** Map centre lon (6dp) */
  lon?: number;
  /** Leaflet zoom */
  z?: number;
  /** Currently selected ICAO hex */
  hex?: string | null;
  /** Bitfield-ish toggles, encoded compactly */
  layers?: string;
  /** Archive instant, epoch ms (UTC). Absent means live; on a write, null
   *  drops it and `span` from the hash. */
  t?: number | null;
  /** Archive window radius in minutes, an integer in
   *  [ARCHIVE_SPAN_MIN_MIN, ARCHIVE_SPAN_MAX_MIN]. */
  span?: number;
}

export const ARCHIVE_SPAN_DEFAULT_MIN = 15;
export const ARCHIVE_SPAN_MIN_MIN = 1;
export const ARCHIVE_SPAN_MAX_MIN = 30;

/** No archive holds anything older, so a link to before it is a typo. */
const ARCHIVE_T_EARLIEST_MS = Date.UTC(2020, 0, 1);
/** How far past this clock a link's instant may be: a sender whose clock runs
 *  ahead, or a local time east of UTC typed with no zone and so read as UTC.
 *  Anything later is a typo. */
const ARCHIVE_T_AHEAD_MS = 24 * 60 * 60 * 1000;

/**
 * The ISO 8601 forms a hand-written or pasted `t` comes in: minutes, optional
 * seconds with an optional fraction of up to three digits, then `Z`, an
 * offset with or without its colon, or nothing, which is read as UTC. `T` and
 * `Z` in either case, as people type them.
 */
const HASH_T = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?(?:Z|([+-])(\d{2}):?(\d{2}))?$/i;

function daysInMonth(year: number, month: number): number {
  return new Date(Date.UTC(year, month, 0)).getUTCDate();
}

/** A `t` value as epoch ms, or undefined when it is not a real instant in the
 *  span a link may name. */
function parseHashT(value: string, nowMs: number): number | undefined {
  const m = HASH_T.exec(value);
  if (!m) return undefined;
  const [year, month, day, hour, minute] = m.slice(1, 6).map(Number);
  const second = Number(m[6] ?? 0);
  const millis = Number((m[7] ?? "").padEnd(3, "0"));
  const sign = m[8] === "-" ? -1 : 1;
  const offsetHours = Number(m[9] ?? 0);
  const offsetMinutes = Number(m[10] ?? 0);
  // Date.UTC would roll 2026-02-30 over into March, and 25:00 into tomorrow.
  if (month < 1 || month > 12 || day < 1 || day > daysInMonth(year, month)) return undefined;
  if (hour > 23 || minute > 59 || second > 59) return undefined;
  if (offsetHours > 23 || offsetMinutes > 59) return undefined;
  const ms = Date.UTC(year, month - 1, day, hour, minute, second, millis) - sign * (offsetHours * 60 + offsetMinutes) * 60_000;
  if (ms < ARCHIVE_T_EARLIEST_MS || ms > nowMs + ARCHIVE_T_AHEAD_MS) return undefined;
  return ms;
}

/** A span in minutes, rounded and clamped, or undefined for anything that is
 *  not a finite number. */
export function clampSpan(minutes: number | undefined): number | undefined {
  if (typeof minutes !== "number" || !Number.isFinite(minutes)) return undefined;
  return Math.min(ARCHIVE_SPAN_MAX_MIN, Math.max(ARCHIVE_SPAN_MIN_MIN, Math.round(minutes)));
}

/** The instant as the hash carries it, floored to the second:
 *  `2026-10-18T22:14:00Z`. Throws a RangeError for a non-finite `ms`, as
 *  Date#toISOString does. */
export function formatHashT(ms: number): string {
  return new Date(Math.floor(ms / 1000) * 1000).toISOString().replace(/\.\d{3}Z$/, "Z");
}

function encodeLayers(t: Record<string, boolean>): string {
  // Each entry becomes a single letter when truthy.  Compact, stable,
  // and trivial to extend without breaking older links.
  let out = "";
  if (t.coverage)     out += "c";
  if (t.labels)       out += "l";
  if (t.trails)       out += "t";
  if (t.groundTruth)  out += "g";
  if (t.illuminators) out += "i";
  if (t.colorByAlt)   out += "a";
  if (t.stats)        out += "s";
  if (t.rangeRings)   out += "r";
  if (t.inBeamDiag)   out += "b";
  if (t.arcs)         out += "d";
  if (t.uncertainty)  out += "u";
  return out;
}

function decodeLayers(s: string | undefined): Partial<Record<string, boolean>> {
  if (!s) return {};
  return {
    coverage:     s.includes("c"),
    labels:       s.includes("l"),
    trails:       s.includes("t"),
    groundTruth:  s.includes("g"),
    illuminators: s.includes("i"),
    colorByAlt:   s.includes("a"),
    stats:        s.includes("s"),
    rangeRings:   s.includes("r"),
    inBeamDiag:   s.includes("b"),
    arcs:         s.includes("d"),
    uncertainty:  s.includes("u"),
  };
}

/**
 * Reads a hash (the page's own by default). `nowMs` bounds how far ahead a
 * `t` may be; an invalid `t` or `span` is left out, so the map stays live.
 */
export function parseHash(
  hash: string = typeof window === "undefined" ? "" : window.location.hash,
  nowMs: number = Date.now(),
): HashState {
  const h = hash.replace(/^#/, "");
  if (!h) return {};
  const out: HashState = {};
  for (const part of h.split("&")) {
    const eq = part.indexOf("=");
    if (eq <= 0) continue;
    const k = part.slice(0, eq);
    let v: string;
    try {
      v = decodeURIComponent(part.slice(eq + 1));
    } catch {
      // A malformed escape, typed or truncated in a paste: that key alone.
      continue;
    }
    if (k === "lat" || k === "lon" || k === "z") {
      const n = Number(v);
      if (Number.isFinite(n)) out[k] = n;
    } else if (k === "hex") {
      out.hex = v || null;
    } else if (k === "layers") {
      out.layers = v;
    } else if (k === "t") {
      const t = parseHashT(v, nowMs);
      if (t !== undefined) out.t = t;
    } else if (k === "span") {
      // Not Number's 0 for a blank value: `span=` names no span.
      const span = clampSpan(v.trim() === "" ? NaN : Number(v));
      if (span !== undefined) out.span = span;
    }
  }
  return out;
}

function replaceHash(next: string) {
  // Use replaceState so each pan/zoom doesn't fill the browser back stack.
  if (window.location.hash !== next) {
    const url = window.location.pathname + window.location.search + next;
    // Carry the entry's state over: React Router keys POP navigation off it.
    window.history.replaceState(window.history.state, "", url);
  }
}

function writeHash(state: HashState) {
  if (typeof window === "undefined") return;
  const parts: string[] = [];
  if (state.lat != null && state.lon != null) {
    parts.push(`lat=${state.lat.toFixed(4)}`);
    parts.push(`lon=${state.lon.toFixed(4)}`);
  }
  if (state.z != null) parts.push(`z=${state.z}`);
  if (state.hex) parts.push(`hex=${encodeURIComponent(state.hex)}`);
  if (state.layers) parts.push(`layers=${state.layers}`);
  // The archive keys belong to useArchiveHash, which writes them straight to
  // the hash, so a view write that does not name them keeps what the hash
  // holds when it lands, not what it held when the write was queued.
  const ownT = state.t === null || Number.isFinite(state.t);
  const ownSpan = clampSpan(state.span);
  const current = ownT && ownSpan !== undefined ? {} : parseHash(window.location.hash);
  const t = ownT ? state.t : current.t;
  if (t != null) {
    // Unencoded: `:` is legal in a fragment, and the link stays readable.
    parts.push(`t=${formatHashT(t)}`);
    const span = ownSpan ?? current.span;
    if (span !== undefined) parts.push(`span=${span}`);
  }
  replaceHash(parts.length ? `#${parts.join("&")}` : "");
}

/** The key of a raw `k=v` pair, or the whole pair when it has no `=`. */
function keyOf(pair: string): string {
  const eq = pair.indexOf("=");
  return eq < 0 ? pair : pair.slice(0, eq);
}

/**
 * Sets the archive keys in the page's hash (`t` a number), or removes them
 * (`t` null, which removes `span` too), without touching any other pair: each
 * keeps its raw text and its place. A key already there is replaced where it
 * stands, a new one is appended. `span` is set only when given.
 *
 * replaceState, so no hashchange follows: the caller already knows.
 */
export function writeArchiveHash(t: number | null, span?: number): void {
  if (typeof window === "undefined") return;
  if (t !== null && !Number.isFinite(t)) return;
  const tPair = t === null ? null : `t=${formatHashT(t)}`;
  const spanMin = t === null ? undefined : clampSpan(span);
  const spanPair = spanMin === undefined ? null : `span=${spanMin}`;
  const pairs: string[] = [];
  let tSet = false;
  let spanSet = false;
  for (const pair of window.location.hash.replace(/^#/, "").split("&")) {
    if (!pair) continue;
    const key = keyOf(pair);
    if (key === "t") {
      // In place of the first; any repeat would only shadow it on read.
      if (tPair && !tSet) pairs.push(tPair);
      tSet = true;
    } else if (key === "span" && (t === null || spanPair)) {
      if (spanPair && !spanSet) pairs.push(spanPair);
      spanSet = true;
    } else {
      pairs.push(pair);
    }
  }
  if (tPair && !tSet) pairs.push(tPair);
  if (spanPair && !spanSet) pairs.push(spanPair);
  replaceHash(pairs.length ? `#${pairs.join("&")}` : "");
}

export interface ArchiveHash {
  /** The archive instant, epoch ms, or null for live. */
  t: number | null;
  /** The window radius in minutes. */
  span: number;
  /** Bumps whenever t or span change from outside (a hashchange) or through
   *  setArchive, so a consumer can tell a new request from a re-render. */
  rev: number;
  setArchive(t: number | null, span?: number): void;
}

interface ArchiveView {
  t: number | null;
  span: number;
}

function archiveOf(parsed: HashState): ArchiveView {
  return { t: parsed.t ?? null, span: parsed.span ?? ARCHIVE_SPAN_DEFAULT_MIN };
}

/** The fragment of a full URL, `#` included, or "" when it has none. */
function hashOfUrl(url: string): string {
  const i = url.indexOf("#");
  return i < 0 ? "" : url.slice(i);
}

/**
 * The archive instant and window the hash asks for, kept in step with it.
 *
 * A hashchange is the user (or a link) changing the URL: when it moves t or
 * span, rev bumps. One that only moves the view does not, so an edit of
 * lat/lon in the address bar leaves the replay alone. The map's own writes,
 * and setArchive's, use replaceState, which fires no hashchange; setArchive
 * bumps rev itself.
 */
export function useArchiveHash(): ArchiveHash {
  const [state, setState] = useState(() => ({ ...archiveOf(parseHash()), rev: 0 }));

  useEffect(() => {
    const onHashChange = (event: HashChangeEvent) => {
      const now = Date.now();
      // The URL the navigation went to, not location.hash: by the time the
      // queued event runs, a replaceState may have moved the hash on.
      const next = archiveOf(parseHash(event.newURL ? hashOfUrl(event.newURL) : window.location.hash, now));
      const prev = event.oldURL ? archiveOf(parseHash(hashOfUrl(event.oldURL), now)) : null;
      setState((s) => {
        const was = prev ?? s;
        if (was.t === next.t && was.span === next.span) return s;
        return { ...next, rev: s.rev + 1 };
      });
    };
    window.addEventListener("hashchange", onHashChange);
    return () => window.removeEventListener("hashchange", onHashChange);
  }, []);

  const setArchive = useCallback((t: number | null, span?: number) => {
    if (t !== null && !Number.isFinite(t)) return;
    writeArchiveHash(t, span);
    setState((s) => ({ t, span: clampSpan(span) ?? s.span, rev: s.rev + 1 }));
  }, []);

  return useMemo(() => ({ ...state, setArchive }), [state, setArchive]);
}

/**
 * Throttled writer — call this from any UI event without worrying about
 * spamming the history API.
 */
export function useHashWriter() {
  const pendingRef = useRef<HashState | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, []);

  return (state: HashState) => {
    pendingRef.current = { ...(pendingRef.current ?? {}), ...state };
    if (timerRef.current) return;
    timerRef.current = setTimeout(() => {
      timerRef.current = null;
      const next = pendingRef.current;
      pendingRef.current = null;
      if (next) writeHash(next);
    }, 250);
  };
}

export { encodeLayers, decodeLayers };
