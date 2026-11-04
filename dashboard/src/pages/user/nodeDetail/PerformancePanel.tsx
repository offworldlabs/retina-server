import { useId, useState, type CSSProperties, type ReactNode } from "react";
import {
  PolarAngleAxis, PolarGrid, PolarRadiusAxis, Radar, RadarChart, ResponsiveContainer, Tooltip,
  type PolarAngleAxisProps, type PolarRadiusAxisProps,
} from "recharts";
import { Sheet } from "../../../components/Sheet";
import { useMediaQuery } from "../../../hooks/useMediaQuery";
import { PHONE, TABLET } from "../../../styles/breakpoints";
import type { NodePerformance } from "../../../types";
import { useTapOutTooltip } from "../../../utils/chartLayout";
import { useChartTheme } from "../../../utils/chartTheme";
import { DASH, formatPercent } from "../../../utils/format";
import { formatRange, formatSystemConstant, RANK_STATUS_TEXT, rankStatusText } from "../contribution/figures";
import "./PerformancePanel.css";

/** A floor on samples: a cell under it is greyed, however it reads. A sample
 *  is one aircraft in one 10 s step, so a few can be a single aircraft. */
const EVIDENCE_FLOOR = 30;

/** Read first when listed: the class the network sees the most of. */
const DEFAULT_CLASS = "narrowbody";

/** A Map, not a lookup on an object: the keys come from the server, and
 *  `constructor` would otherwise resolve up the prototype chain. */
const CLASS_LABELS = new Map([
  ["military_transport", "Military transport"],
  ["widebody", "Widebody"],
  ["regional_jet", "Regional jet"],
  ["narrowbody", "Narrowbody"],
  ["turboprop", "Turboprop"],
  ["bizjet", "Business jet"],
  ["light_GA", "Light aircraft"],
  ["helicopter", "Helicopter"],
  ["unknown", "Unknown type"],
]);

const classLabel = (key: string) => CLASS_LABELS.get(key) ?? key.replace(/_/g, " ");

/** A bin as the analysis keys it and as an axis prints it. */
interface Bin {
  key: string;
  label: string;
}

/** Highest first, so the table reads the way the sky does. */
const ALTITUDES: readonly Bin[] = [
  { key: "20-45kft", label: "20–45" },
  { key: "10-20kft", label: "10–20" },
  { key: "5-10kft", label: "5–10" },
  { key: "0-5kft", label: "0–5" },
];

const RANGES: readonly Bin[] = [
  { key: "0-20km", label: "0–20" },
  { key: "20-40km", label: "20–40" },
  { key: "40-60km", label: "40–60" },
  { key: "60-100km", label: "60–100" },
];

interface Reading {
  pd: number | null;
  evidence: number;
}

/** One cell of a class's table. A cell the file leaves out is one nothing was
 *  measured in. */
function readCell(performance: NodePerformance, cls: string, range: Bin, altitude: Bin): Reading {
  return {
    pd: performance.pd_by_class_range_alt[cls]?.[range.key]?.[altitude.key] ?? null,
    evidence: performance.evidence_by_class_range_alt[cls]?.[range.key]?.[altitude.key] ?? 0,
  };
}

const pdText = (pd: number | null) => (pd === null ? DASH : formatPercent(pd * 100, 0));

/** Five digits and a comma are wider than a phone's cell holds. */
const compactCount = (n: number) => (n >= 10_000 ? `${Math.round(n / 1000)}k` : n.toLocaleString());

/** The share of accent in a cell's ground. The top of the ramp is as far as
 *  the text ink keeps AA on the dark theme's card (4.9:1 at 45%), and the foot
 *  stays clear of a greyed cell's ground. */
const heat = (pd: number) => `${(12 + 33 * pd).toFixed(1)}%`;

function cellDetail(cls: string, range: Bin, altitude: Bin, { pd, evidence }: Reading): string {
  const place = `${classLabel(cls)}, ${range.label} km, ${altitude.label} kft`;
  const samples = evidence.toLocaleString();
  return pd === null ? `${place}: no figure, ${samples} samples` : `${place}: ${pdText(pd)} of ${samples} samples`;
}

function Fact({ label, value, warning, children }: { label: string; value: ReactNode; warning?: boolean; children?: ReactNode }) {
  return (
    <div className={warning ? "node-performance-fact node-performance-fact-warning" : "node-performance-fact"}>
      <dt className="reading-label">{label}</dt>
      <dd className="reading-value">{value}</dd>
      {children}
    </div>
  );
}

/**
 * Rank, sensitivity and configuration. A rank status's sentence prints once,
 * in the fact it explains: `tower_unknown` under Sensitivity and
 * `config_suspect` under Configuration while those show the state, and under
 * Rank otherwise. The server decides what is suspect; this only reads
 * `rank_status`.
 */
function HeadlineFacts({ performance }: { performance: NodePerformance }) {
  const { rank, rank_status: status, tower, tag_delay_residual_mad_us: residual } = performance;
  const towerUnknown = performance.system_constant_db == null && tower.status !== "matched";
  const suspect = residual != null && status === "config_suspect";
  const explainedElsewhere = suspect || (towerUnknown && status === "tower_unknown");
  const residualFrom = performance.delay_residual_source === "claims" ? "claim" : "tag";
  const residualText = residual != null && `${residual.toFixed(2)} µs ${residualFrom} residual`;

  return (
    <dl className="node-performance-facts">
      <Fact label="Rank" value={typeof rank === "number" ? `#${rank}` : "Not ranked"}>
        {typeof rank !== "number" && status && !explainedElsewhere && (
          <dd className="node-performance-reason">{rankStatusText(status)}</dd>
        )}
      </Fact>
      <Fact
        label="Sensitivity"
        value={formatSystemConstant(
          performance.system_constant_db,
          performance.system_constant_sigma_db,
          performance.system_constant_bound,
        )}
      >
        {tower.callsign && <dd className="card-note">Tower {tower.callsign}</dd>}
        {towerUnknown && <dd className="node-performance-reason">{RANK_STATUS_TEXT.tower_unknown}</dd>}
      </Fact>
      <Fact
        label="Configuration"
        warning={suspect}
        value={
          !residualText ? "Not enough tags or claims to check yet" : suspect ? residualText : `${residualText}, in line with its site`
        }
      >
        {performance.delay_residual_source === "claims" && (
          <dd className="card-note">Read from its claims, as it has too few tags</dd>
        )}
        {suspect && <dd className="node-performance-reason">{RANK_STATUS_TEXT.config_suspect}</dd>}
      </Fact>
    </dl>
  );
}

interface ClassSwitchProps {
  classes: readonly string[];
  chosen: string;
  onChoose: (cls: string) => void;
}

function ClassSwitch({ classes, chosen, onChoose }: ClassSwitchProps) {
  const isPhone = useMediaQuery(PHONE);
  const pickerId = useId();

  if (isPhone) {
    return (
      <div className="node-performance-picker">
        <label htmlFor={pickerId}>Aircraft class</label>
        <select id={pickerId} className="input" value={chosen} onChange={(e) => onChoose(e.target.value)}>
          {classes.map((cls) => <option key={cls} value={cls}>{classLabel(cls)}</option>)}
        </select>
      </div>
    );
  }

  return (
    <div className="node-performance-chips" role="group" aria-label="Aircraft class">
      {classes.map((cls) => (
        <button
          key={cls}
          type="button"
          className={`btn ${cls === chosen ? "btn-primary" : "btn-secondary"} btn-sm`}
          aria-pressed={cls === chosen}
          onClick={() => onChoose(cls)}
        >
          {classLabel(cls)}
        </button>
      ))}
    </div>
  );
}

interface GridCellProps {
  reading: Reading;
  detail: string;
  /** Set where there is no hover to read a title by. */
  onOpen?: () => void;
}

function GridCell({ reading: { pd, evidence }, detail, onOpen }: GridCellProps) {
  const heated = pd !== null && evidence >= EVIDENCE_FLOOR;
  const figures = (
    <>
      <span className="node-performance-pd">{pdText(pd)}</span>
      <span className="node-performance-count">{compactCount(evidence)}</span>
    </>
  );
  return (
    <div
      role="cell"
      className={heated ? "node-performance-cell" : "node-performance-cell node-performance-cell-greyed"}
      style={heated ? ({ "--node-performance-heat": heat(pd) } as CSSProperties) : undefined}
      title={onOpen ? undefined : detail}
    >
      {onOpen ? (
        <button type="button" className="node-performance-cell-open" aria-label={detail} onClick={onOpen}>
          {figures}
        </button>
      ) : figures}
    </div>
  );
}

/** Where a cell's detail is open, re-read from each poll while it stays open. */
interface Spot {
  range: Bin;
  altitude: Bin;
}

function DetectionGrid({ performance, cls }: { performance: NodePerformance; cls: string }) {
  const narrow = useMediaQuery(TABLET);
  const [spot, setSpot] = useState<Spot | null>(null);
  // Past the tablet width the Sheet is gone, and must not come back open.
  if (!narrow && spot) setSpot(null);
  const opened = spot && readCell(performance, cls, spot.range, spot.altitude);

  return (
    <div>
      <div className="node-performance-range-axis">Range (km)</div>
      <div className="node-performance-grid" role="table" aria-label={`${classLabel(cls)}: share detected by altitude and range`}>
        <div className="node-performance-row" role="row">
          <div className="node-performance-axis" role="columnheader">Altitude (kft)</div>
          {RANGES.map((range) => (
            <div key={range.key} className="node-performance-axis" role="columnheader">{range.label}</div>
          ))}
        </div>
        {ALTITUDES.map((altitude) => (
          <div key={altitude.key} className="node-performance-row" role="row">
            <div className="node-performance-axis" role="rowheader">{altitude.label}</div>
            {RANGES.map((range) => {
              const reading = readCell(performance, cls, range, altitude);
              return (
                <GridCell
                  key={range.key}
                  reading={reading}
                  detail={cellDetail(cls, range, altitude, reading)}
                  onOpen={narrow ? () => setSpot({ range, altitude }) : undefined}
                />
              );
            })}
          </div>
        ))}
      </div>
      <p className="card-note node-performance-key">
        Each cell: the share detected, and how many samples that rests on. Greyed under {EVIDENCE_FLOOR} samples.
        A sample is one aircraft in one 10 s step.
      </p>
      {narrow && (
        <Sheet open={spot !== null} onClose={() => setSpot(null)} variant="bottom" title={classLabel(cls)}>
          {spot && opened && (
            <>
              <table className="kv-table">
                <tbody>
                  <tr><td>Range</td><td>{spot.range.label} km</td></tr>
                  <tr><td>Altitude</td><td>{spot.altitude.label} kft</td></tr>
                  <tr><td>Detected</td><td>{pdText(opened.pd)}</td></tr>
                  <tr><td>Samples</td><td>{opened.evidence.toLocaleString()}</td></tr>
                </tbody>
              </table>
              {opened.evidence < EVIDENCE_FLOOR && (
                <p className="node-performance-reason">
                  {`Fewer than ${EVIDENCE_FLOOR} samples here, so read this as a rough guide`}
                </p>
              )}
            </>
          )}
        </Sheet>
      )}
    </div>
  );
}

function RangeList({ performance, classes, chosen }: { performance: NodePerformance; classes: readonly string[]; chosen: string }) {
  return (
    <section>
      <h4 className="node-performance-title">50% detection range</h4>
      <p className="card-note">Aircraft at 5,000–20,000 ft</p>
      <dl className="node-performance-range-list">
        {classes.map((cls) => (
          <div
            key={cls}
            className={cls === chosen ? "node-performance-range node-performance-range-chosen" : "node-performance-range"}
            aria-current={cls === chosen ? "true" : undefined}
          >
            <dt>{classLabel(cls)}</dt>
            <dd>{formatRange(performance.range_50pct_km[cls], performance.range_50pct_bound[cls])}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

const COMPASS = new Map([[0, "N"], [90, "E"], [180, "S"], [270, "W"]]);

// Recharts types `ticks` as the items it draws, and reads what a chart passes
// as plain axis values.
const COMPASS_TICKS = [...COMPASS.keys()] as unknown as PolarAngleAxisProps["ticks"];
const PD_TICKS = [0.25, 0.5, 0.75, 1] as unknown as PolarRadiusAxisProps["ticks"];

interface BearingPoint {
  x: number;
  y: number;
}

/** A bin with no figure has no coordinates. */
const drawn = (point: BearingPoint | undefined) => point !== undefined && Number.isFinite(point.x);

/** The line through the measured bins, for Radar's `shape`. Recharts' own
 *  polygon closes a lone run of bins, drawing a chord across the bearings
 *  between its ends where nothing was measured. */
function bearingLine({ points, stroke }: { points: readonly BearingPoint[]; stroke: string }) {
  const steps = points.flatMap((point, i) =>
    drawn(point) ? [`${drawn(points[i - 1]) ? "L" : "M"}${point.x},${point.y}`] : [],
  );
  // 355° and 0° are neighbours.
  const [first, last] = [points[0], points[points.length - 1]];
  if (drawn(first) && drawn(last)) steps.push(`L${first.x},${first.y}`);
  return <path d={steps.join("")} fill="none" stroke={stroke} strokeWidth={2} />;
}

function BearingChart({ bins }: { bins: readonly (number | null)[] }) {
  const chart = useChartTheme();
  const tip = useTapOutTooltip();
  const data = bins.map((pd, i) => ({ bearing: i * 5, pd }));

  return (
    <section className="node-performance-bearing">
      <h4 className="node-performance-title">Detection by bearing</h4>
      <p className="card-note">Share detected in each 5° of bearing. A gap means nothing was measured there.</p>
      <div className="chart-container node-performance-compass" {...tip.wrapper}>
        <ResponsiveContainer width="100%" height="100%">
          <RadarChart data={data} outerRadius="82%" {...tip.chart}>
            <PolarGrid gridType="circle" stroke={chart.grid} />
            <PolarAngleAxis
              dataKey="bearing"
              type="number"
              domain={[0, 360]}
              ticks={COMPASS_TICKS}
              tickFormatter={(bearing: number) => COMPASS.get(bearing) ?? ""}
              tick={{ fontSize: 12 }}
              tickLine={false}
              axisLineType="circle"
              stroke={chart.axis}
            />
            <PolarRadiusAxis
              domain={[0, 1]}
              ticks={PD_TICKS}
              tickFormatter={(pd: number) => pdText(pd)}
              angle={45}
              axisLine={false}
              tick={{ fontSize: 10 }}
              stroke={chart.axis}
            />
            <Tooltip
              {...tip.tooltip}
              contentStyle={chart.tooltip}
              cursor={chart.cursor}
              filterNull={false}
              labelFormatter={(bearing) => `${bearing}°`}
              formatter={(pd) => [pdText(pd as number | null), "Detected"]}
            />
            <Radar
              dataKey="pd"
              stroke={chart.series[0]}
              connectNulls={false}
              shape={bearingLine}
              dot={{ r: 3, fill: chart.series[0] }}
              isAnimationActive={false}
            />
          </RadarChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}

/** The credit the Mictronics licence asks for, under every page that publishes
 *  figures derived from the aircraft classes. */
export function DataLicenceNotice() {
  return (
    <p className="node-performance-notice">
      Contains information from the{" "}
      <a href="https://github.com/Mictronics/aircraft-database" target="_blank" rel="noopener noreferrer">
        Mictronics aircraft database
      </a>
      , made available under the{" "}
      <a href="https://opendatacommons.org/licenses/by/1-0/" target="_blank" rel="noopener noreferrer">
        ODC Attribution License
      </a>
    </p>
  );
}

function Measured({ performance }: { performance: NodePerformance }) {
  const classes = performance.classes_with_evidence;
  const [picked, setPicked] = useState<string | null>(null);
  // A later reading can stop listing the class being looked at. That ends the
  // pick, so the view does not jump back if the class returns.
  const listed = picked !== null && classes.includes(picked);
  if (picked !== null && !listed) setPicked(null);
  const chosen = listed ? picked : classes.includes(DEFAULT_CLASS) ? DEFAULT_CLASS : classes[0];
  const [from, to] = performance.window;

  return (
    <>
      <div className="card">
        <div className="card-header">
          <h3>What this node detects</h3>
          <span className="card-note">{from} to {to}, {performance.opportunities.toLocaleString()} ADS-B samples</span>
        </div>
        <div className="card-body node-performance-body">
          <HeadlineFacts performance={performance} />
          <p className="card-note">
            {performance.numerator === "tags"
              ? "These figures count the node's own ADS-B tags."
              : "These figures count claims that pass the quality gate."}{" "}
            A node that tags few aircraft can&apos;t be scored yet: few of its detections count here, so its figures say
            little about what it sees.
          </p>
          {classes.length === 0 ? (
            <p className="node-detail-empty">No aircraft class has enough evidence for a table yet.</p>
          ) : (
            <div className="node-performance-classes">
              <ClassSwitch classes={classes} chosen={chosen} onChoose={setPicked} />
              <div className="node-performance-split">
                <DetectionGrid performance={performance} cls={chosen} />
                <RangeList performance={performance} classes={classes} chosen={chosen} />
              </div>
            </div>
          )}
          {/* The keyed route never sends it, and its absence says nothing. */}
          {Array.isArray(performance.pd_by_bearing) && <BearingChart bins={performance.pd_by_bearing} />}
        </div>
      </div>
      <DataLicenceNotice />
    </>
  );
}

/**
 * What a node detects, per aircraft class: how far and how high it sees, how
 * sensitive it is, whether its configuration looks right, and which way it
 * sees. `performance` is the daily analysis's reading of the node, top-level on
 * both the owner's body and the keyed one, and null or absent until it has one.
 */
export function PerformancePanel({ performance }: { performance: NodePerformance | null | undefined }) {
  return (
    <div className="node-performance">
      {performance ? (
        <Measured performance={performance} />
      ) : (
        <div className="card">
          <div className="card-header"><h3>What this node detects</h3></div>
          <div className="card-body">
            <p className="node-detail-empty">
              No figures yet. They appear once the daily analysis has measured this node against passing ADS-B aircraft.
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
