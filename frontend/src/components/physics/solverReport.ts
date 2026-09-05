/**
 * Pure formatting/shaping helpers for the Physics tab's Solver Report panel.
 * Kept dependency-free from React so they're trivial to unit test — all the
 * JSX lives in PhysicsSettings.tsx, this file only turns
 * GET /api/test/solver-stats payloads into render-ready shapes.
 */

export interface SolverStats {
  window_minutes: number;
  /** How much of window_minutes the backend's solve-history stores actually
   * hold.  Lower than window_minutes means the answer is truncated.  Absent
   * from an older cached payload. */
  window_effective_minutes?: number;
  /** Records in the window per solver lane — the denominator the dark funnel
   * below excludes.  Absent from an older cached payload. */
  lane_split?: { dark: number; adsb: number; known: number };
  // attempts / published / rejects / position_error_km / fragmentation are
  // the DARK lane only (the multinode lane with no transponder), which is
  // the lane a publication funnel describes.  The known lane has its own
  // block server-side and never published through this funnel; it used to be
  // counted in it anyway, which put its success label at the top of the
  // reject table and made 94% of "attempts" belong to another lane.
  attempts: number;
  published: { total: number; n2: number; n3plus: number };
  rejects: { total: number; by_reason: Record<string, number> };
  position_error_km: { median: number | null; p90: number | null; n: number };
  ghosts: {
    /** Always "dark" — precision is scored over dark tracks only. */
    scope?: string;
    live_tracks: number;
    /** Informational; excluded from the precision denominator. */
    adsb_associated: number;
    /** The precision denominator: dark tracks with a position. */
    dark_tracks?: number;
    gt_matched: number;
    /** Dark tracks rescued by nearby real ADS-B traffic rather than GT. */
    adsb_near?: number;
    ghost_tracks: number;
    /** null when there are no dark tracks to score — formatPct renders "—".
     * It used to read a flat 100 in that case, which is the shape a healthy
     * dark lane and a completely dead one share. */
    precision_pct: number | null;
  };
  consensus: {
    mode: string;
    selected: number;
    filtered: number;
    fallback: number;
    shadow: number;
  };
  // Top-down claiming (ASSOC_CLAIM_MODE), since boot — same "cumulative
  // regardless of mode" convention as consensus above.  May be absent from
  // an older cached payload; render null-safely (see formatKm/formatPct's
  // "—" idiom) rather than assuming presence.
  claiming?: {
    mode: string;
    rounds: number;
    matched: number;
    conflicts: number;
    anchored_inputs: number;
    tracklets_excluded: number;
    anchor_hits: number;
    anchor_fallbacks: number;
    anchored_published: number;
  };
  // Windowed, from published records — distinct published keys is the
  // acceptance metric claiming exists to move toward O(targets).
  fragmentation?: {
    distinct_keys: number;
    published: number;
    solves_per_key: { median: number | null; p90: number | null };
    anchored_pct: number;
  };
  counters: {
    successes: number;
    failures: number;
    n2_unconfirmed: number;
    solver_trimmed: number;
    stale_drops: number;
    queue_drops: number;
  };
}

/** "0.42 km" / "—" for null (no published solves inside the error gate). */
export function formatKm(v: number | null | undefined): string {
  return v != null ? `${v.toFixed(2)} km` : "—";
}

/** "91.7%" / "—" for null. */
export function formatPct(v: number | null | undefined, digits = 1): string {
  return v != null ? `${v.toFixed(digits)}%` : "—";
}

export interface FunnelSegment {
  key: "n2" | "n3plus" | "rejected";
  label: string;
  count: number;
  /** Share of (published + rejected) attempts, 0–100. 0 when there's nothing yet. */
  pct: number;
  color: string;
}

const FUNNEL_COLORS: Record<FunnelSegment["key"], string> = {
  n2: "#38bdf8",
  n3plus: "#a78bfa",
  rejected: "#f43f5e",
};

/**
 * Three-way funnel split for the stacked bar: published n=2, published
 * n>=3, rejected. Order matches the legend and the color spec.
 */
export function funnelSegments(
  stats: Pick<SolverStats, "published" | "rejects">,
): FunnelSegment[] {
  const total = stats.published.n2 + stats.published.n3plus + stats.rejects.total;
  const seg = (key: FunnelSegment["key"], label: string, count: number): FunnelSegment => ({
    key,
    label,
    count,
    pct: total > 0 ? (count / total) * 100 : 0,
    color: FUNNEL_COLORS[key],
  });
  return [
    seg("n2", "Published n=2", stats.published.n2),
    seg("n3plus", "Published n≥3", stats.published.n3plus),
    seg("rejected", "Rejected", stats.rejects.total),
  ];
}

export interface RejectReasonBar {
  reason: string;
  count: number;
  /** Width of the mini-bar relative to the largest reason, 0–100. */
  pct: number;
}

/** Reject reasons sorted desc by count, scaled for a proportional mini-bar row each. */
export function rejectReasonBars(byReason: Record<string, number>): RejectReasonBar[] {
  const entries = Object.entries(byReason).sort((a, b) => b[1] - a[1]);
  const max = entries.length ? entries[0][1] : 0;
  return entries.map(([reason, count]) => ({
    reason,
    count,
    pct: max > 0 ? (count / max) * 100 : 0,
  }));
}

/** Display label for the consensus mode badge. */
export function consensusModeLabel(mode: string): string {
  if (mode === "active") return "Active";
  if (mode === "shadow") return "Shadow";
  return "Off";
}

/** Display label for the claiming mode badge — same off/shadow/active set
 * as consensus, so the same fallback-to-"Off" rule applies. */
export function claimModeLabel(mode: string): string {
  if (mode === "active") return "Active";
  if (mode === "shadow") return "Shadow";
  return "Off";
}
