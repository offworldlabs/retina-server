import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";
import { StatCard } from "../../../components/StatCard";
import { formatAvailability, formatDuration } from "../../../utils/format";
import { useChartTheme } from "../../../utils/chartTheme";
import { useChartLayout, useTapOutTooltip, type ChartFrame } from "../../../utils/chartLayout";
import { detectionCount } from "../../../utils/nodes";

const TRUST_FRAME: ChartFrame = {
  height: 200,
  xAxis: { dataKey: "name", tick: { fontSize: 11 } },
  yAxis: { tick: { fontSize: 11 } },
};

/**
 * A node's measured figures: counts, trust, timing and detection area. The
 * owner's summary and the public one share their keys, so either is drawn
 * here; the owner's has nothing moved. Sections are read as they come, since
 * the analytics service builds each one on its own.
 */
export function NodeFigures({ analytics }: { analytics: any }) {
  const chart = useChartTheme();
  const tip = useTapOutTooltip();
  const metrics = analytics.metrics || analytics;
  const trust = analytics.trust || {};
  const reputation = analytics.reputation || {};
  const gapStats = metrics.gap_stats || {};
  const area = analytics.detection_area;

  const barData = [
    { name: "Avg SNR", value: metrics.avg_snr || 0 },
    { name: "Trust", value: (trust.trust_score || 0) * 100 },
    { name: "Reputation", value: (reputation.reputation || 0) * 100 },
  ];

  const { ref, frame } = useChartLayout(TRUST_FRAME, { kind: "bar", categories: barData.length, longestLabel: 10 });

  return (
    <>
      <div className="stats-grid">
        <StatCard label="Total Frames" value={(metrics.total_frames || 0).toLocaleString()} tone="accent" />
        <StatCard label="Total Detections" value={detectionCount(analytics).toLocaleString()} tone="success" />
        <StatCard label="Total Tracks" value={metrics.total_tracks || 0} />
        <StatCard label="Avg SNR" value={<>{(metrics.avg_snr || 0).toFixed(1)} dB</>} />
      </div>

      <div className="grid-2">
        <div className="card">
          <div className="card-header"><h3>Trust & Reputation</h3></div>
          <div className="card-body">
            <div className="chart-container" ref={ref} style={{ height: frame.height }} {...tip.wrapper}>
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={barData} layout={frame.layout} margin={frame.margin} {...tip.chart}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                  <XAxis {...frame.xAxis} stroke={chart.axis} />
                  <YAxis {...frame.yAxis} stroke={chart.axis} />
                  <Tooltip {...tip.tooltip} contentStyle={chart.tooltip} cursor={chart.cursor} />
                  <Bar
                    dataKey="value"
                    fill={chart.series[0]}
                    radius={frame.layout === "vertical" ? [0, 4, 4, 0] : [4, 4, 0, 0]}
                  />
                </BarChart>
              </ResponsiveContainer>
            </div>
            <table className="kv-table">
              <tbody>
                <tr><td>Trust Score</td><td>{((trust.trust_score || 0) * 100).toFixed(1)}%</td></tr>
                <tr><td>ADS-B Matches</td><td>{trust.adsb_matches || 0}</td></tr>
                <tr><td>ADS-B Misses</td><td>{trust.adsb_misses || 0}</td></tr>
                <tr><td>Reputation</td><td>{((reputation.reputation || 0) * 100).toFixed(1)}%</td></tr>
                <tr><td>Penalties</td><td>{reputation.n_penalties || 0}</td></tr>
                <tr><td>Blocked</td><td>{reputation.blocked ? "Yes" : "No"}</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <div className="card-header"><h3>Timing & Gaps</h3></div>
          <div className="card-body">
            <table className="kv-table">
              <tbody>
                <tr><td>Availability (7 d)</td><td>{formatAvailability(metrics.availability_7d)}</td></tr>
                <tr><td>Availability (24 h)</td><td>{formatAvailability(metrics.availability_24h)}</td></tr>
                <tr><td>Availability measured over</td><td>{formatDuration(metrics.availability_measured_s)}</td></tr>
                <tr><td>Average Gap</td><td>{(gapStats.avg_gap || 0).toFixed(2)}s</td></tr>
                <tr><td>Max Gap</td><td>{(gapStats.max_gap || 0).toFixed(2)}s</td></tr>
                <tr><td>Gap Std Dev</td><td>{(gapStats.std_gap || 0).toFixed(3)}s</td></tr>
                <tr><td>Total Gaps</td><td>{gapStats.n_gaps || 0}</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      </div>

      {area && (
        <div className="card">
          <div className="card-header"><h3>Detection Area</h3></div>
          <div className="card-body">
            <table className="kv-table">
              <tbody>
                <tr><td>Estimated Range</td><td>{(area.estimated_range_km || 0).toFixed(1)} km</td></tr>
                <tr><td>Beam Width</td><td>{(area.beam_width_deg || 0).toFixed(1)}°</td></tr>
                <tr><td>ADS-B Validated Positions</td><td>{area.validated_positions || 0}</td></tr>
              </tbody>
            </table>
          </div>
        </div>
      )}
    </>
  );
}

/** Where the figures go for an owned node the server has measured nothing of
 *  yet. The owner routes say so with a null, where the public route has
 *  nothing to publish and answers 404. */
export function NoFiguresYet() {
  return (
    <div className="card">
      <div className="card-header"><h3>Metrics</h3></div>
      <div className="card-body">
        <p className="node-detail-empty">
          Nothing measured for this node yet. Its metrics start with the first frames the server receives from it.
        </p>
      </div>
    </div>
  );
}
