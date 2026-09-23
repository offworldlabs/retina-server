import { DASH } from "../../utils/format";
import { POSITION_SOURCE_ARC_ONLY, POSITION_SOURCE_ADSB_SINGLE } from "./constants";
import { classifyHex, emergencySquawkLabel } from "./hexInfo";
import { trailToCsv, downloadCsv } from "./trailExport";
import { copyToClipboard, toast } from "./toast";
import { M_PER_FT, KNOTS_PER_MS, MS_PER_KNOT } from "./units";
import { solveUncertaintyRadiusM, solveUncertaintyRadius95M } from "./uncertainty";
import { usePalette } from "./useMapTheme";

/**
 * `nodeLabelFor` maps a node ref to the handle the map is allowed to print
 * (map/nodeSites.ts).  Refs are the join key everywhere — `ac.node_ref`,
 * `detectingNodes` — and the private node id is never published, so the
 * default keeps the panel usable on its own (in a test, say) and names an
 * unknown node rather than echoing an identifier it cannot resolve.
 */
export default function AircraftDetailPanel({ ac, onClose, groundTruth, trails, computeError, detectingNodes = [], nodeLabelFor = (_nodeRef) => "unlisted node" }) {
  const { ANOMALY, DRONE, LANE_MN_ADSB } = usePalette();
  if (!ac) return null;

  const err = computeError(ac.hex, ac);
  const gtHex = ac.ground_truth_hex || ac.hex;
  const gtTrail = groundTruth[gtHex];
  const solvedPts = (trails[ac.hex] || []).length;
  const truthPts = gtTrail?.length || 0;
  const gtLast = gtTrail?.length ? gtTrail[gtTrail.length - 1] : null;
  const altErrFt = gtLast ? Math.abs((ac.alt_baro || 0) - gtLast[2] / M_PER_FT) : null;

  // Enthusiast classification — military/govt/test hex ranges and special
  // squawk codes. Both are "always on": even a truth-only entry gets a
  // hex-range badge if it lands in a known range.
  const hexInfo = classifyHex(ac.hex);
  const emergency = emergencySquawkLabel(ac.squawk);

  // Position-uncertainty radii, from the same helpers as the map disc so the
  // panel and the circle can never quote different numbers.  The map draws
  // the 68% ring (readable at map scale); the panel shows it beside the 95%
  // figure, which is the number anyone asking "how sure are you?" expects and
  // which the ring stopped being on 2026-09-06.  Both describe the LAST
  // solve and hold until the next one, so there is no "now" distinct from
  // "at solve".
  const uncertaintyM = solveUncertaintyRadiusM(ac);
  const uncertainty95M = solveUncertaintyRadius95M(ac);

  const handleExportTrail = () => {
    // `trails` (prop) is the canonical solved-position trail buffer maintained
    // by LiveAircraftMap. Backend rows are [lat, lon, alt_ft, ts_ms]; the
    // exporter's normaliseRow handles the legacy 3-tuple form too.
    const rows = ((trails && trails[ac.hex]) || []).filter(Boolean).slice();
    if (!rows.length) {
      // No buffered points — fall back to the current single fix so the user
      // still gets a non-empty CSV.
      if (ac.lat != null && ac.lon != null) {
        rows.push([ac.lat, ac.lon, (ac.alt_baro || 0), Date.now()]);
      } else {
        toast("No trail data yet", { tone: "warn" });
        return;
      }
    }
    const csv = trailToCsv(ac.hex, ac.flight, rows);
    downloadCsv(`retina-trail-${ac.hex}-${Date.now()}.csv`, csv);
    toast(`Exported ${rows.length} points`, { tone: "success" });
  };

  const isMultinode = ac.multinode;
  const hasAdsb = ac.type !== "tisb_other" && ac.type !== "multinode_solve";
  const isAmbiguityArc = ac.position_source === POSITION_SOURCE_ARC_ONLY;
  const isSolverOnly = ac.position_source === "solver_single_node";
  const isSolverAdsbSeed = ac.position_source === "solver_adsb_seed";
  const isAdsbSingleNode = ac.position_source === POSITION_SOURCE_ADSB_SINGLE;
  const isDrone = ac.target_class === "drone";
  // The multi-node label names the LANE, not the node count — the count
  // already has its own Nodes field below, while whether the solve carried a
  // transponder tag (mn-adsb-* vs mn-dark-*) was invisible until adsb_assisted.
  const sourceLabel = isMultinode
    ? (ac.adsb_assisted ? "Multi-node (ADS-B assisted)" : "Multi-node (dark)")
    : isAmbiguityArc
      ? "Single-node ellipse arc"
      : isAdsbSingleNode
        ? "ADS-B (single node)"
        : isSolverAdsbSeed
          ? "Solver (ADS-B seeded)"
          : isSolverOnly
            ? "Single-node solver (uncertain)"
            : hasAdsb
              ? "ADS-B"
              : ac.type || "Unknown";
  const sourceBadge = isMultinode ? "multinode" : isSolverAdsbSeed || isAdsbSingleNode ? "adsb" : hasAdsb ? "adsb" : "other";
  // Authoritative flag set by applyGroundTruthFixes — the old
  // `!ac.type && !ac.flight` heuristic classified ordinary radar tracks
  // (which usually have neither) as "Ground truth only".
  const isTruthOnly = Boolean(ac._isTruth);

  return (
    <div className="detail-panel">
      <div className="detail-panel-header">
        <h3>{ac.flight?.trim() || ac.hex}</h3>
        <button className="close-btn" onClick={onClose} title="Close">
          &times;
        </button>
      </div>
      <div className="detail-panel-body">
        {emergency && (
          <div className="detail-alert"
            title="Emergency squawk code reported by aircraft transponder"
          >
            ⚠ {emergency}
          </div>
        )}
        {/* Identity */}
        <div className="detail-section">
          <div className="detail-section-title">Identity</div>
          <Field label="HEX" value={<span className="detail-hex-badge" onClick={() => copyToClipboard(ac.hex, "HEX copied")} title="Click to copy" style={{ cursor: "pointer" }}>{ac.hex}</span>} />
          {hexInfo.label && (
            <Field
              label="Registry"
              value={
                <span
                  style={{
                    color: hexInfo.color,
                    fontWeight: 600,
                    border: `1px solid ${hexInfo.color}`,
                    padding: "1px 6px",
                    borderRadius: 4,
                    fontSize: 11,
                    textTransform: "uppercase",
                    letterSpacing: 0.4,
                  }}
                >
                  {hexInfo.label}
                </span>
              }
            />
          )}
          {!isTruthOnly && (
            <>
              <Field label="Callsign" value={ac.flight?.trim() || "\u2014"} />
              <Field
                label="Source"
                value={<span className={`detail-source-badge ${sourceBadge}`}>{sourceLabel}</span>}
              />
              {ac.target_class && (
                <Field
                  label="Target class"
                  value={
                    <span style={{ color: isDrone ? DRONE : LANE_MN_ADSB, fontWeight: 600 }}>
                      {isDrone ? "\u{1F6F8} Drone" : "\u2708\uFE0F Aircraft"}
                    </span>
                  }
                />
              )}
            </>
          )}
          {isTruthOnly && (
            <Field
              label="Status"
              value={<span className="detail-source-badge other">Ground truth only</span>}
            />
          )}
        </div>

        {/* Position */}
        <div className="detail-section">
          <div className="detail-section-title">Position</div>
          <Field label={isAmbiguityArc ? "Arc midpoint lat" : "Latitude"} value={ac.lat?.toFixed(5) ?? "\u2014"} />
          <Field label={isAmbiguityArc ? "Arc midpoint lon" : "Longitude"} value={ac.lon?.toFixed(5) ?? "\u2014"} />
          <Field
            label="Altitude"
            value={
              ac.alt_baro != null
                ? `${ac.alt_baro.toLocaleString()} ft`
                : ac.alt_m != null
                  ? `${Math.round(ac.alt_m / M_PER_FT).toLocaleString()} ft`
                  : "\u2014"
            }
          />
          {!isTruthOnly && (
            <>
              <Field label="Speed" value={ac.gs != null ? `${ac.gs} kts` : "\u2014"} />
              <Field
                label="Heading"
                value={ac.track != null ? `${ac.track.toFixed(0)}\u00b0` : "\u2014"}
              />
            </>
          )}
          {isTruthOnly && (
            <>
              <Field label="Speed" value={ac.speed_ms != null && ac.speed_ms > 0 ? `${(ac.speed_ms * KNOTS_PER_MS).toFixed(0)} kts (${ac.speed_ms.toFixed(0)} m/s)` : ac.gs != null ? `${ac.gs} kts` : "\u2014"} />
              <Field label="Heading" value={ac.heading != null && ac.heading > 0 ? `${ac.heading.toFixed(0)}\u00b0` : ac.track != null ? `${ac.track.toFixed(0)}\u00b0` : "\u2014"} />
            </>
          )}
          {isAmbiguityArc && (
            <>
              <Field label="Display mode" value="Delay ellipse across detection area" />
              <Field label="Latest delay" value={ac.delay_us != null ? `${ac.delay_us} μs` : "\u2014"} />
            </>
          )}
          {isSolverOnly && (
            <Field
              label="Note"
              value={<span style={{ color: "var(--text-muted)", fontStyle: "italic" }}>Position uncertain — single node, no arc</span>}
            />
          )}
        </div>

        {/* Claimed single-node detection.  Identity/Position above already
            carry the ADS-B side of this entry (hex, callsign, altitude, speed,
            heading all come straight off the claim's adsb_fix), so this block
            adds only what is unique to the claim: who is holding it, how old
            the fix behind it is, and the raw measurement. */}
        {isAdsbSingleNode && (
          <div className="detail-section">
            <div className="detail-section-title">Claimed detection</div>
            <Field label="Claiming node" value={ac.node_ref ? nodeLabelFor(ac.node_ref) : DASH} />
            <Field
              label="ADS-B fix age"
              value={ac.adsb_fix_age_s != null ? `${ac.adsb_fix_age_s}s` : DASH}
            />
            <Field label="Latest delay" value={ac.delay_us != null ? `${ac.delay_us} μs` : DASH} />
            <Field label="Latest doppler" value={ac.doppler_hz != null ? `${ac.doppler_hz} Hz` : DASH} />
            <Field
              label="Note"
              value={
                <span style={{ color: "var(--text-muted)", fontStyle: "italic" }}>
                  Position is the ADS-B fix; the arc is the delay locus from the claiming node
                </span>
              }
            />
          </div>
        )}

        {/* Multi-node details */}
        {isMultinode && (
          <div className="detail-section">
            <div className="detail-section-title">Multi-node</div>
            <Field label="Nodes" value={ac.n_nodes} />
            <Field label="RMS Delay" value={`${ac.rms_delay ?? "\u2014"} \u03bcs`} />
            <Field label="RMS Doppler" value={`${ac.rms_doppler ?? "\u2014"} Hz`} />
            {uncertaintyM > 0 && (
              <Field
                label="Accuracy"
                value={`\u00b1${formatUncertaintyRadius(uncertaintyM)} (68%) \u00b7 \u00b1${formatUncertaintyRadius(uncertainty95M)} (95%)`}
              />
            )}
          </div>
        )}

        {/* Anomaly detection */}
        {ac.is_anomalous && (
          <div className="detail-section">
            <div className="detail-section-title" style={{ color: ANOMALY }}>
              ⚠ Anomaly Detected
            </div>
            <Field
              label="Type"
              value={
                <span style={{ color: ANOMALY, fontWeight: 600 }}>
                  {(ac.anomaly_types || []).map(t => ({
                    supersonic: "Supersonic",
                    // Not a claim about the aircraft — a claim about our own
                    // estimate.  Labelled distinctly so a weakly-observable
                    // Doppler geometry is not read as a Mach-1 target.
                    instant_acceleration: "Instant Acceleration",
                    instant_direction_change: "Instant Direction Change",
                    sustained_orbit: "Sustained Orbit",
                    position_mismatch: "GPS Spoof",
                    identity_swap: "Identity Swap",
                    altitude_jump: "Altitude Jump",
                    anomalous_acceleration: "Anomalous Acceleration (>10g)",
                    long_hover: "Long Hover",
                  }[t] || t)).join(", ") || "unknown"}
                </span>
              }
            />
            {ac.max_velocity_ms > 0 && (
              <Field
                label="Max velocity"
                value={`${ac.max_velocity_ms.toFixed(0)} m/s (Mach ${(ac.max_velocity_ms / 343).toFixed(2)})`}
              />
            )}
            {ac.gs != null && (
              <Field
                label="Current speed"
                value={`${ac.gs} kts (${(ac.gs * MS_PER_KNOT).toFixed(0)} m/s)`}
              />
            )}
          </div>
        )}

        {/* Solver residuals for single-node */}
        {!isMultinode && !isTruthOnly && (ac.rms_delay != null || ac.rms_doppler != null) && (
          <div className="detail-section">
            <div className="detail-section-title">Solver Confidence</div>
            <Field label="RMS Delay" value={ac.rms_delay != null ? `${ac.rms_delay} \u03bcs` : "\u2014"} />
            <Field label="RMS Doppler" value={ac.rms_doppler != null ? `${ac.rms_doppler} Hz` : "\u2014"} />
            {ac.delay_us != null && <Field label="Latest Delay" value={`${ac.delay_us} \u03bcs`} />}
            {ac.doppler_hz != null && <Field label="Latest Doppler" value={`${ac.doppler_hz} Hz`} />}
          </div>
        )}

        {/* Accuracy */}
        <div className="detail-section">
          <div className="detail-section-title">Accuracy</div>
          <Field label="Solved pts" value={solvedPts} />
          <Field label="Truth pts" value={truthPts} />
          {err !== null && (
            <Field
              label="Pos Error"
              value={
                <span className={`detail-value ${err < 2 ? "good" : err < 5 ? "warn" : "bad"}`}>
                  {err.toFixed(2)} km
                </span>
              }
            />
          )}
          {altErrFt !== null && <Field label="Alt Error" value={`${Math.round(altErrFt)} ft`} />}
        </div>

        {/* Simulated parameters + live detection fan-out (debug) */}
        {isTruthOnly && (
          <div className="detail-section">
            <div className="detail-section-title">Simulation (debug)</div>
            {ac.source && (
              <Field
                label="Source"
                value={ac.source === "live" ? "Live ADS-B feed (real aircraft, echoed by synthetic nodes)" : "Simulated spawn"}
              />
            )}
            <Field
              label="ADS-B"
              value={
                ac.has_adsb
                  ? <span style={{ color: "var(--success)", fontWeight: 600 }}>yes</span>
                  : <span style={{ color: "var(--text-secondary)", fontWeight: 600 }}>no — dark target</span>
              }
            />
            <Field label="Callsign" value={ac.adsb_callsign || DASH} />
            <Field label="Object type" value={ac.object_type || "aircraft"} />
            {ac.is_anomalous && (
              <Field
                label="Anomaly event"
                value={
                  <span style={{ color: ANOMALY, fontWeight: 600 }}>
                    {ac.anomaly_event || "anomalous"}
                  </span>
                }
              />
            )}
            <Field
              label="Detected by"
              value={
                detectingNodes.length
                  ? (
                    <span style={{ wordBreak: "break-word" }}>
                      {detectingNodes.map(nodeLabelFor).join(", ")}
                      <span style={{ color: "var(--text-secondary)" }}> ({detectingNodes.length})</span>
                    </span>
                  )
                  : <span style={{ color: "var(--text-secondary)" }}>no nodes right now</span>
              }
            />
          </div>
        )}

        {/* Truth-only trail count */}
        {isTruthOnly && (
          <div className="detail-section">
            <div className="detail-section-title">Trail</div>
            <Field label="Points" value={ac.points || 0} />
          </div>
        )}

        {/* Export — handy for enthusiasts pulling tracks into KML/QGIS. */}
        <div className="detail-section detail-actions">
          <button
            type="button"
            className="btn btn-secondary"
            onClick={handleExportTrail}
            style={{ width: "100%" }}
            title="Download recent positions as CSV (lat, lon, alt, timestamp)"
          >
            ⇩ Export trail (CSV)
          </button>
        </div>
      </div>
    </div>
  );
}

/** Uncertainty radius for display: rounded to 10 m, switching to km with one
 *  decimal above 2 km where the extra digits are noise. */
function formatUncertaintyRadius(m) {
  if (m > 2000) return `${(m / 1000).toFixed(1)} km`;
  return `${Math.round(m / 10) * 10} m`;
}

function Field({ label, value }) {
  return (
    <div className="detail-field">
      <span className="detail-label">{label}</span>
      <span className="detail-value">{value}</span>
    </div>
  );
}
