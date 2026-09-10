import { useMemo } from "react";
import { POSITION_SOURCE_ADSB_SINGLE } from "./constants";
import { drIconState } from "./icons";
import { usePalette } from "./useMapTheme";
import { M_PER_FT } from "./units";

interface StatsOverlayProps {
  aircraft: any[];
  truth: any[];
  anomalyCount: number;
  visible: boolean;
  onToggle: () => void;
}

/**
 * Compact panel pinned to the top-right of the map.  Aggregates the
 * current radar snapshot into the handful of counts an aircraft watcher
 * actually scans for: total tracks, source mix, mean altitude, fastest
 * mover, current anomaly count.
 *
 * The panel is collapsible — collapsed, it shrinks to a single chip so
 * it never hides a target the user is trying to click.
 */
export default function StatsOverlay({ aircraft, truth, anomalyCount, visible, onToggle }: StatsOverlayProps) {
  const { ANOMALY, DRONE, LANE_ADSB_SINGLE, LANE_MN_ADSB, LANE_MN_DARK, LANE_SOLVER_SEED } =
    usePalette();
  const stats = useMemo(() => {
    const now = Date.now();
    const total = aircraft.length;
    // Split by lane, matching the icon colours: an mn-adsb-* solve knew the
    // transponder, an mn-dark-* one did not, and "how many of our solves are
    // actually dark" is the number this panel exists to surface.
    // ...and by what the map actually draws for them.  The panel used to count
    // the raw feed only, so it could report "MLAT dark 9" with nine violet
    // icons hidden by the drift gate — a number that silently disagreed with
    // the map it sits on.  drIconState is the same call the icon layer makes:
    // "hidden" is an icon the user cannot see, "stale" is one drawn degraded.
    // (Viewport culling is not modelled here — these are feed-wide counts, as
    // every other row in this panel is.)
    let mnAssisted = 0;
    let mnAssistedHidden = 0;
    let mnDark = 0;
    let mnDarkStale = 0;
    let arcOnly = 0;
    let adsbSeed = 0;
    let adsbSingle = 0;
    let solverOnly = 0;
    let drones = 0;
    let altSum = 0;
    let altCount = 0;
    let maxGs = 0;
    let maxGsCallsign = "";
    for (const ac of aircraft) {
      if (ac.multinode) {
        const drState = drIconState(ac, now);
        if (ac.adsb_assisted) {
          mnAssisted++;
          if (drState === "hidden") mnAssistedHidden++;
        } else {
          mnDark++;
          if (drState !== "normal") mnDarkStale++;
        }
      }
      else if (ac.position_source === "single_node_ellipse_arc") arcOnly++;
      else if (ac.position_source === "solver_adsb_seed") adsbSeed++;
      else if (ac.position_source === POSITION_SOURCE_ADSB_SINGLE) adsbSingle++;
      else if (ac.position_source === "solver_single_node") solverOnly++;
      if (ac.target_class === "drone") drones++;
      const alt = ac.alt_baro ?? (ac.alt_m ? ac.alt_m / M_PER_FT : null);
      if (alt != null && alt > 0) { altSum += alt; altCount++; }
      const gs = ac.gs ?? 0;
      if (gs > maxGs) { maxGs = gs; maxGsCallsign = (ac.flight || ac.hex || "").trim(); }
    }
    return {
      total,
      truth: truth.length,
      mnAssisted,
      mnAssistedHidden,
      mnDark,
      mnDarkStale,
      arcOnly,
      adsbSeed,
      adsbSingle,
      solverOnly,
      drones,
      meanAltFt: altCount ? Math.round(altSum / altCount) : null,
      maxGs: Math.round(maxGs),
      maxGsCallsign,
    };
  }, [aircraft, truth]);

  return (
    <div className={`stats-panel${visible ? "" : " collapsed"}`} role="region" aria-label="Live stats">
      <button
        className="stats-panel-header"
        onClick={onToggle}
        aria-expanded={visible}
        title={visible ? "Collapse stats" : "Show stats"}
      >
        <span>{visible ? "Live stats" : `${stats.total} tracks`}</span>
        <span aria-hidden="true">{visible ? "▾" : "▸"}</span>
      </button>

      {visible && (
        <div className="stats-panel-body">
          <Row label="Total">
            <strong>{stats.total}</strong>
            {stats.truth ? <span className="stats-note">+{stats.truth} truth</span> : null}
          </Row>

          <Row label="MLAT+ADS‑B">
            <strong style={{ color: LANE_MN_ADSB }}>{stats.mnAssisted}</strong>
            {stats.mnAssistedHidden > 0 && (
              <span className="stats-note" title="Dead-reckoned past the drift budget — no icon drawn">
                {stats.mnAssistedHidden} hidden
              </span>
            )}
          </Row>

          <Row label="MLAT dark">
            <strong style={{ color: LANE_MN_DARK }}>{stats.mnDark}</strong>
            {stats.mnDarkStale > 0 && (
              <span
                className="stats-note"
                title="Drawn in the degraded stale-solve style — solved, but past the drift budget"
              >
                {stats.mnDarkStale} stale
              </span>
            )}
          </Row>

          <Row label="Solver+ADS‑B">
            <strong style={{ color: LANE_SOLVER_SEED }}>{stats.adsbSeed}</strong>
          </Row>

          <Row label="ADS‑B·1N">
            <strong style={{ color: LANE_ADSB_SINGLE }}>{stats.adsbSingle}</strong>
          </Row>

          <Row label="Arc·1N">{stats.arcOnly}</Row>
          <Row label="Solver·1N">{stats.solverOnly}</Row>

          {stats.drones > 0 && (
            <Row label="Drones">
              <strong style={{ color: DRONE }}>{stats.drones}</strong>
            </Row>
          )}

          {anomalyCount > 0 && (
            <Row label="Anomalies">
              <strong style={{ color: ANOMALY }}>⚠ {anomalyCount}</strong>
            </Row>
          )}

          {stats.meanAltFt != null && (
            <Row label="Mean alt">FL{Math.round(stats.meanAltFt / 100)}</Row>
          )}

          {stats.maxGs > 0 && (
            <Row label="Fastest">
              <span title={stats.maxGsCallsign}>{stats.maxGs} kt</span>
              {stats.maxGsCallsign && (
                <span className="stats-note">{stats.maxGsCallsign.slice(0, 8)}</span>
              )}
            </Row>
          )}
        </div>
      )}
    </div>
  );
}

/** One label/value pair. The grid lives on the body, so each row is just its
 *  two cells — a fragment, not a wrapper that would break the column tracks. */
function Row({ label, children }) {
  return (
    <>
      <span className="stats-row-label">{label}</span>
      <span className="stats-row-value">{children}</span>
    </>
  );
}
