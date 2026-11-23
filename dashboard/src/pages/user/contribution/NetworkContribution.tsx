import { useState } from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";

import { DataTable, type ColumnHint } from "../../../components/DataTable";
import { Pager, clampPage } from "../../../components/Pager";
import { StatCard } from "../../../components/StatCard";
import { useChartLayout, useTapOutTooltip, type ChartFrame } from "../../../utils/chartLayout";
import { useChartTheme } from "../../../utils/chartTheme";
import { DASH } from "../../../utils/format";
import { detectionCount, shortRef } from "../../../utils/nodes";
import {
  count,
  displayName,
  formatTrust,
  totalDetections,
  type NodeSummary,
  type OverlapPair,
  type Overlaps,
  type Standings,
} from "./figures";
import { NodeTag, Swatch } from "./NodeTag";

const PAGE_SIZE = 25;
const CHART_TOP = 20;
const RANKINGS_TOP = 10;

const DETECTIONS_FRAME: ChartFrame = {
  height: 250,
  xAxis: { dataKey: "name", tick: { fontSize: 11 } },
  yAxis: { tick: { fontSize: 11 } },
};

const OVERLAP_HINTS: readonly (ColumnHint | undefined)[] = ["title", undefined, undefined, "secondary", "secondary"];
const RANKING_HINTS: readonly (ColumnHint | undefined)[] = [undefined, "title", undefined, undefined];

/** The size of a shared coverage volume, which the column's title explains. */
export const SHARED_POINTS = (
  <span title="Points of the network's 3 km coverage grid, at six altitudes, that both nodes' beams cover">
    Shared Points
  </span>
);

interface Props {
  /** Every node once, the owner's figures over their own nodes; null until
   *  the network's analytics land. */
  nodes: ReadonlyMap<string, NodeSummary> | null;
  /** The network's first answer is still on its way. */
  loading: boolean;
  overlaps: Overlaps | null;
  standings: Standings | null;
  /** The owner's node_refs, marked wherever they are listed. */
  mine: ReadonlySet<string>;
  names: ReadonlyMap<string, string>;
}

/** The network the owner's nodes are part of: its totals, who detects the
 *  most, which beams share coverage and the top of the leaderboard. Each part
 *  shows once its own source has answered. */
export function NetworkContribution({ nodes, loading, overlaps, standings, mine, names }: Props) {
  const showsOverlaps = overlaps !== null && overlaps.pairs.length > 0;
  const showsRankings = standings !== null && standings.rows.length > 0;
  return (
    <>
      {loading ? (
        <p className="section-caption" role="status">
          Loading the network…
        </p>
      ) : (
        nodes && <NetworkFigures nodes={nodes} overlaps={overlaps} mine={mine} names={names} />
      )}
      {showsOverlaps && <OverlapsCard pairs={overlaps.pairs} mine={mine} names={names} />}
      {showsRankings && <RankingsCard standings={standings} mine={mine} names={names} />}
    </>
  );
}

interface ChartBar {
  ref: string;
  /** The axis label: a short ref, which every node has and which stays short. */
  name: string;
  /** The tooltip's: what the node is called. */
  label: string;
  detections: number;
  mine: boolean;
}

function NetworkFigures({
  nodes,
  overlaps,
  mine,
  names,
}: {
  nodes: ReadonlyMap<string, NodeSummary>;
  overlaps: Overlaps | null;
  mine: ReadonlySet<string>;
  names: ReadonlyMap<string, string>;
}) {
  const chart = useChartTheme();
  const summaries = [...nodes.values()];
  const total = totalDetections(summaries);
  const avgTrust = summaries.length
    ? summaries.reduce((s, n) => s + (n.trust?.trust_score || 0), 0) / summaries.length
    : 0;

  const bars: ChartBar[] = [...nodes]
    .map(([ref, summary]) => ({
      ref,
      name: shortRef(ref),
      label: names.get(ref) ?? shortRef(ref),
      detections: detectionCount(summary),
      mine: mine.has(ref),
    }))
    .sort((a, b) => b.detections - a.detections || a.ref.localeCompare(b.ref))
    .slice(0, CHART_TOP);
  // Two series only while one of the owner's nodes is among the bars; the
  // network alone keeps the one colour it always had.
  const highlights = bars.some((bar) => bar.mine);
  const theirs = chart.series[0];
  const yours = chart.series[1];

  const { ref, frame } = useChartLayout(DETECTIONS_FRAME, {
    kind: "bar",
    categories: bars.length,
    longestLabel: Math.max(0, ...bars.map((bar) => bar.name.length)),
  });
  const tip = useTapOutTooltip();

  return (
    <>
      <div className="stats-grid contribution-stats">
        <StatCard label="Network Detections" value={total.toLocaleString()} tone="accent" />
        <StatCard label="Avg Trust Score" value={<>{(avgTrust * 100).toFixed(1)}%</>} tone="success" />
        <StatCard label="Nodes" value={nodes.size.toLocaleString()} sub="with analytics" />
        <StatCard
          label="Coverage Overlaps"
          value={overlaps ? overlaps.pairs.length.toLocaleString() : DASH}
          tone="warning"
          sub="pairs of nodes"
        />
      </div>

      {bars.length > 0 && (
        <div className="card">
          <div className="card-header">
            <h3>Detections per Node — Top {CHART_TOP}</h3>
            <div className="card-aside contribution-aside">
              {highlights && (
                <ul className="contribution-legend" aria-label="Key">
                  <li>
                    <Swatch colour={yours} />
                    Your nodes
                  </li>
                  <li>
                    <Swatch colour={theirs} />
                    Other nodes
                  </li>
                </ul>
              )}
              {mine.size > 0 && !highlights && (
                <span className="card-note">None of your nodes is in the top {CHART_TOP}</span>
              )}
              <span className="card-note">{nodes.size.toLocaleString()} total nodes</span>
            </div>
          </div>
          <div className="card-body">
            <div
              className="chart-container"
              ref={ref}
              style={frame === DETECTIONS_FRAME ? undefined : { height: frame.height }}
              {...tip.wrapper}
              role="img"
              aria-label={`Detections of the ${bars.length} nodes that detect the most${
                highlights ? ", your nodes highlighted" : ""
              }`}
            >
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={bars} layout={frame.layout} margin={frame.margin} {...tip.chart}>
                  <CartesianGrid strokeDasharray="3 3" stroke={chart.grid} />
                  <XAxis {...frame.xAxis} stroke={chart.axis} />
                  <YAxis {...frame.yAxis} stroke={chart.axis} />
                  <Tooltip
                    {...tip.tooltip}
                    contentStyle={chart.tooltip}
                    cursor={chart.cursor}
                    labelFormatter={(label, payload) => {
                      const bar = payload?.[0]?.payload as ChartBar | undefined;
                      if (!bar) return label;
                      return bar.mine ? `${bar.label} (yours)` : bar.label;
                    }}
                    formatter={(value) => [Number(value).toLocaleString(), "Detections"]}
                  />
                  <Bar dataKey="detections" fill={theirs} radius={frame.layout === "vertical" ? [0, 4, 4, 0] : [4, 4, 0, 0]}>
                    {bars.map((bar) => (
                      <Cell key={bar.ref} fill={highlights && bar.mine ? yours : theirs} />
                    ))}
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      )}
    </>
  );
}

/** A gate as the association service sets it, or a dash for one it did not. */
function gate(value: number | null, decimals: number, unit: string): string {
  return value === null ? DASH : `${value.toFixed(decimals)} ${unit}`;
}

function OverlapsCard({
  pairs,
  mine,
  names,
}: {
  pairs: readonly OverlapPair[];
  mine: ReadonlySet<string>;
  names: ReadonlyMap<string, string>;
}) {
  const [page, setPage] = useState(0);
  const totalPages = Math.ceil(pairs.length / PAGE_SIZE);
  const current = clampPage(page, totalPages);
  const paged = pairs.slice(current * PAGE_SIZE, (current + 1) * PAGE_SIZE);
  const name = (ref: string) => names.get(ref) ?? shortRef(ref);
  return (
    <div className="card contribution-listing">
      <div className="card-header">
        <h3>Coverage Overlaps</h3>
        <span className="card-note">{count(pairs.length, "pair")}</span>
      </div>
      <DataTable
        headers={["Node A", "Node B", SHARED_POINTS, "Delay Gate", "Doppler Gate"]}
        hints={OVERLAP_HINTS}
        count={paged.length}
      >
        {paged.map((pair) => {
          const a = mine.has(pair.node_a);
          const b = mine.has(pair.node_b);
          return (
            <tr key={`${pair.node_a}|${pair.node_b}`} className={a || b ? "mine" : undefined}>
              <td>
                <NodeTag name={name(pair.node_a)} mine={a} />
              </td>
              <td>
                <NodeTag name={name(pair.node_b)} mine={b} />
              </td>
              <td>{pair.grid_points.toLocaleString()}</td>
              <td>{gate(pair.delay_gate_us, 1, "µs")}</td>
              <td>{gate(pair.doppler_gate_hz, 0, "Hz")}</td>
            </tr>
          );
        })}
      </DataTable>
      <Pager page={current} totalPages={totalPages} onPage={setPage} />
    </div>
  );
}

function RankingsCard({
  standings,
  mine,
  names,
}: {
  standings: Standings;
  mine: ReadonlySet<string>;
  names: ReadonlyMap<string, string>;
}) {
  const top = [...standings.rows].sort((a, b) => a.rank - b.rank).slice(0, RANKINGS_TOP);
  return (
    <div className="card contribution-listing contribution-rankings">
      <div className="card-header">
        <h3>Network Rankings</h3>
        <div className="card-aside contribution-aside">
          <span className="card-note">
            Top {top.length} of {standings.of.toLocaleString()}
          </span>
          <Link className="contribution-more" to="/leaderboard">
            Full leaderboard
          </Link>
        </div>
      </div>
      <DataTable headers={["#", "Node", "Detections", "Trust"]} hints={RANKING_HINTS} count={top.length}>
        {top.map((row) => {
          const own = mine.has(row.node_ref);
          return (
            <tr key={row.node_ref} className={own ? "mine" : undefined}>
              <td className="contribution-rank" data-label="">{row.rank}</td>
              <td>
                <NodeTag name={names.get(row.node_ref) ?? displayName(row)} mine={own} />
              </td>
              <td>{(row.detections ?? 0).toLocaleString()}</td>
              <td>{formatTrust(row.trust_score)}</td>
            </tr>
          );
        })}
      </DataTable>
    </div>
  );
}
