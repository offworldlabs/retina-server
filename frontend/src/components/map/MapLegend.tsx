import { ALTITUDE_LEGEND } from "./icons";
import {
  ILLUMINATOR,
  LANE_MN_ADSB,
  LANE_MN_DARK,
  LANE_SOLVER_SEED,
  NODE,
  TRUTH,
} from "./mapPalette";
import { usePersistedState } from "./usePersistedState";

/**
 * The key to the track colours, pinned to the bottom-left of the map.
 *
 * It used to ride at the end of the toolbar, where it shared a row with the
 * controls and moved every time that row rewrapped. A legend is part of the
 * picture, not part of the chrome, so it now sits on the map and collapses to
 * its header for anyone who has learnt the colours.
 */
export default function MapLegend({ colorByAlt, showGroundTruth, showIlluminators, hasPlayback }) {
  const [open, setOpen] = usePersistedState("tf.legendOpen", true);

  return (
    <div className={`map-legend${hasPlayback ? " with-playback" : ""}`}>
      <button
        className="map-legend-header"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={open ? "Collapse legend" : "Show legend"}
      >
        {colorByAlt ? "Altitude" : "Track source"}
        <span aria-hidden="true">{open ? "▾" : "▸"}</span>
      </button>

      {open && (
        <div className="map-legend-body">
          {colorByAlt ? (
            ALTITUDE_LEGEND.map(([c, lbl]) => <LegendItem key={lbl} color={c} label={lbl} />)
          ) : (
            <>
              <LegendItem color={LANE_SOLVER_SEED} label="Solver + ADS-B" />
              {/* Multi-node splits by lane, same shades getAircraftColor gives
                  the icons: a solve that carried a transponder tag vs a dark
                  one. */}
              <LegendItem color={LANE_MN_ADSB} label="MLAT + ADS-B" />
              <LegendItem color={LANE_MN_DARK} label="MLAT dark" />
              {showGroundTruth && <LegendItem color={TRUTH} label="Ground truth" />}
            </>
          )}
          <LegendItem color={NODE} label="Node" />
          {showIlluminators && <LegendItem color={ILLUMINATOR} label="Illuminator" />}
        </div>
      )}
    </div>
  );
}

function LegendItem({ color, label }) {
  return (
    <span className="legend-item">
      <span className="legend-dot" style={{ background: color }} /> {label}
    </span>
  );
}
