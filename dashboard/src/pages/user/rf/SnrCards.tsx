import type { ReactElement } from "react";
import {
  Bar, BarChart, CartesianGrid, Cell, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
  type TooltipProps,
} from "recharts";
import type { NameType, ValueType } from "recharts/types/component/DefaultTooltipContent";

import { FetchNotice } from "../../../components/Notice";
import type { SnrSeries } from "../../../types";
import { type ChartTheme, useChartTheme } from "../../../utils/chartTheme";
import { nodeLabel } from "./choices";
import { averageSnr, type NetworkNode, type SnrSample } from "./feeds";
import {
  clock, clockSeconds, hourTicks, isolated, lastHours, type SnrBucket, seriesTitle, throughLabel, withGaps,
} from "./series";
import type { KeyedPoll } from "./useKeyedPoll";

import "./rf.css";

const HOUR_MS = 3_600_000;

/** The legend's words in the text ink; the swatch beside each carries the colour. */
const legendText = (value: string) => <span className="rf-legend-text">{value}</span>;

const plural = (n: number, one: string, many: string) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

/* ── An owned node: the server's series ─────────────────────────────────── */

type Percentile = "median_db" | "p90_db";

/** What Recharts hands a custom dot, the keys read here. */
interface DotArgs {
  key?: string;
  index?: number;
  cx?: number | null;
  cy?: number | null;
}

/** A dot for a bucket with no neighbour on either side, which a line alone
 *  cannot draw; nothing for the rest, which the line already does. */
function loneDots(buckets: SnrBucket[], line: Percentile, colour: string) {
  return function loneDot({ key, index = -1, cx, cy }: DotArgs): ReactElement<SVGElement> {
    if (cx == null || cy == null || !isolated(buckets, index, line)) return <g key={key} />;
    return <circle key={key} cx={cx} cy={cy} r={3} fill={colour} />;
  };
}

interface BucketTooltipProps extends TooltipProps<ValueType, NameType> {
  bucketMs: number;
  theme: ChartTheme;
}

/** A bucket's window, its two percentiles and how many detections they are of;
 *  for a gap, how long nothing was detected. */
export function BucketTooltip({ active, payload, bucketMs, theme }: BucketTooltipProps) {
  if (!active || !payload?.length) return null;
  const bucket = payload[0].payload as SnrBucket;
  const end = bucket.gap_until ?? bucket.t_ms + bucketMs;
  return (
    <div className="rf-tooltip" style={theme.tooltip}>
      <div className="rf-tooltip-title">
        {clock(bucket.t_ms)}–{clock(end)}
      </div>
      {bucket.median_db === null ? (
        <div className="rf-tooltip-note">No detections</div>
      ) : (
        <>
          <div className="rf-tooltip-row">
            <span className="rf-tooltip-swatch" style={{ color: theme.series[0] }} />
            Median
            <span className="rf-tooltip-value">{bucket.median_db.toFixed(1)} dB</span>
          </div>
          {bucket.p90_db !== null && (
            <div className="rf-tooltip-row">
              <span className="rf-tooltip-swatch dashed" style={{ color: theme.series[1] }} />
              90th percentile
              <span className="rf-tooltip-value">{bucket.p90_db.toFixed(1)} dB</span>
            </div>
          )}
          <div className="rf-tooltip-note">{plural(bucket.count, "detection", "detections")}</div>
        </>
      )}
    </div>
  );
}

/** The chart in words, for whoever cannot hover it: the latest bucket and the
 *  window's total. */
function SeriesSummary({ series, buckets }: { series: SnrSeries; buckets: SnrBucket[] }) {
  const latest = [...buckets].reverse().find((b) => b.median_db !== null);
  if (!latest || latest.median_db === null) return null;
  const total = series.points.reduce((sum, p) => sum + p.count, 0);
  return (
    <p className="card-note rf-chart-summary">
      Latest bucket, {clock(latest.t_ms)}–{clock(latest.t_ms + series.bucket_s * 1000)}: median{" "}
      {latest.median_db.toFixed(1)} dB
      {latest.p90_db !== null && <>, 90th percentile {latest.p90_db.toFixed(1)} dB</>}, of{" "}
      {plural(latest.count, "detection", "detections")}. {plural(total, "detection", "detections")} in{" "}
      {lastHours(series.hours)}.
    </p>
  );
}

function SeriesChart({ series, at }: { series: SnrSeries; at: number }) {
  const chart = useChartTheme();
  const buckets = withGaps(series);
  // The window the server read, ending when it answered: an hour with no
  // detection is empty space on the axis, not an axis that starts later.
  const start = at - series.hours * HOUR_MS;
  const end = Math.max(at, buckets[buckets.length - 1].t_ms);
  return (
    <>
      <div className="chart-container">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={buckets}>
            <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
            <XAxis
              dataKey="t_ms"
              type="number"
              scale="time"
              domain={[start, end]}
              ticks={hourTicks(start, end)}
              tickFormatter={clock}
              stroke={chart.axis}
              tick={{ fontSize: 11 }}
            />
            <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} width={36} />
            <Tooltip
              cursor={chart.cursor}
              content={<BucketTooltip bucketMs={series.bucket_s * 1000} theme={chart} />}
            />
            <Legend iconSize={10} wrapperStyle={{ fontSize: 12 }} formatter={legendText} />
            <Line
              type="linear"
              dataKey="median_db"
              name="Median (dB)"
              stroke={chart.series[0]}
              strokeWidth={2}
              dot={loneDots(buckets, "median_db", chart.series[0])}
            />
            {/* Dashed as well as coloured, so the two lines part without colour. */}
            <Line
              type="linear"
              dataKey="p90_db"
              name="90th percentile (dB)"
              stroke={chart.series[1]}
              strokeWidth={2}
              strokeDasharray="5 3"
              dot={loneDots(buckets, "p90_db", chart.series[1])}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>
      <SeriesSummary series={series} buckets={buckets} />
    </>
  );
}

interface OwnedSnrCardProps {
  poll: KeyedPoll<SnrSeries>;
  /** The window asked for, which titles the card until an answer names its own. */
  hours: number;
  /** The bucket the server is contracted to, likewise. */
  bucketS: number;
}

/** An owned node's detection SNR per bucket over the window, read from the
 *  archive by the server. */
export function OwnedSnrCard({ poll, hours, bucketS }: OwnedSnrCardProps) {
  const answer = poll.answer;
  const series = answer?.value ?? null;
  const window = series?.hours ?? hours;

  let body: ReactElement | null = null;
  if (answer && series && series.points.length > 0) {
    body = <SeriesChart series={series} at={answer.at} />;
  } else if (series) {
    body = (
      <div className="chart-container">
        <div className="empty-state">No detections from this node in {lastHours(window)}.</div>
      </div>
    );
  } else if (poll.loading) {
    body = (
      <div className="chart-container">
        <div className="empty-state">Loading…</div>
      </div>
    );
  }

  return (
    <div className="card rf-trend">
      <div className="card-header">
        <h3>{seriesTitle(window, series?.bucket_s ?? bucketS)}</h3>
        {answer && series?.through_ms != null && (
          <span className="card-note">{throughLabel(series.through_ms, answer.at)}</span>
        )}
      </div>
      <div className="card-body">
        <FetchNotice polled={poll} what="this node's SNR history" />
        {body}
      </div>
    </div>
  );
}

/* ── Anyone else's node: the live sampled trend ─────────────────────────── */

/** A network node's running average, read off the public analytics on every
 *  refresh since the page opened. */
export function LiveSnrCard({ samples, every }: { samples: SnrSample[]; every: number }) {
  const chart = useChartTheme();
  return (
    <div className="card rf-trend">
      <div className="card-header">
        <h3>Average SNR since you opened this page · sampled every {every / 1000} s</h3>
        <span className="card-note">A running average over all its detections</span>
      </div>
      <div className="card-body">
        <div className="chart-container">
          {samples.length === 0 ? (
            <div className="empty-state">This node has reported no average SNR yet.</div>
          ) : (
            <ResponsiveContainer width="100%" height="100%">
              <LineChart data={samples}>
                <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                <XAxis dataKey="t" tickFormatter={clockSeconds} stroke={chart.axis} tick={{ fontSize: 10 }} />
                <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} width={36} />
                <Tooltip
                  contentStyle={chart.tooltip}
                  cursor={chart.cursor}
                  labelFormatter={(t) => (typeof t === "number" ? clockSeconds(t) : String(t))}
                  formatter={(value) => [typeof value === "number" ? `${value.toFixed(2)} dB` : String(value), "Average SNR"]}
                />
                {/* One sample is a point and not yet a line. */}
                <Line
                  type="monotone"
                  dataKey="snr"
                  stroke={chart.series[0]}
                  strokeWidth={2}
                  dot={samples.length === 1}
                  name="Average SNR (dB)"
                />
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      </div>
    </div>
  );
}

/* ── The network ─────────────────────────────────────────────────────────── */

const TOP = 20;

interface NetworkBar {
  name: string;
  snr: number;
  mine: boolean;
}

interface NetworkSnrCardProps {
  nodes: NetworkNode[];
  /** The owner's node_refs, whose bars are marked. */
  mine: ReadonlySet<string>;
  /** No answer from the network yet. */
  loading: boolean;
  /** The network's first answer failed, so there is nothing to draw. */
  failed: boolean;
}

/** The public listing's nodes by average SNR, the owner's own marked. */
export function NetworkSnrCard({ nodes, mine, loading, failed }: NetworkSnrCardProps) {
  const chart = useChartTheme();
  const reporting = nodes.flatMap((node): NetworkBar[] => {
    const snr = averageSnr(node.summary);
    return snr === null ? [] : [{ name: nodeLabel(node.name, node.ref), snr, mine: mine.has(node.ref) }];
  });
  const bars = [...reporting].sort((a, b) => b.snr - a.snr).slice(0, TOP);
  const anyMine = bars.some((b) => b.mine);
  const fill = (bar: NetworkBar) => (bar.mine ? chart.series[0] : chart.series[1]);

  let body: ReactElement;
  if (loading) body = <div className="empty-state">Loading…</div>;
  else if (failed) body = <div className="empty-state">The network&rsquo;s figures could not be loaded.</div>;
  else if (bars.length === 0) body = <div className="empty-state">No node on the network reports SNR yet.</div>;
  else {
    body = (
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={bars}>
          <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
          <XAxis dataKey="name" stroke={chart.axis} tick={{ fontSize: 10 }} />
          <YAxis stroke={chart.axis} tick={{ fontSize: 11 }} width={36} />
          <Tooltip
            contentStyle={chart.tooltip}
            cursor={chart.cursor}
            labelFormatter={(label, payload) => {
              const bar = payload?.[0]?.payload as NetworkBar | undefined;
              return bar?.mine ? `${label} · yours` : label;
            }}
            formatter={(value) => [typeof value === "number" ? `${value.toFixed(1)} dB` : String(value), "Average SNR"]}
          />
          {anyMine && (
            <Legend
              iconSize={10}
              wrapperStyle={{ fontSize: 12 }}
              formatter={legendText}
              payload={[
                { value: "Your nodes", type: "square", id: "yours", color: chart.series[0] },
                { value: "Others", type: "square", id: "others", color: chart.series[1] },
              ]}
            />
          )}
          <Bar dataKey="snr" name="Average SNR (dB)" radius={[4, 4, 0, 0]}>
            {bars.map((bar, i) => (
              <Cell key={i} fill={fill(bar)} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    );
  }

  return (
    <div className="card">
      <div className="card-header">
        <h3>{reporting.length > TOP ? `Network average SNR — top ${TOP}` : "Network average SNR"}</h3>
        <span className="card-note">{plural(nodes.length, "public node", "public nodes")}</span>
      </div>
      <div className="card-body">
        <div className="chart-container">{body}</div>
      </div>
    </div>
  );
}
