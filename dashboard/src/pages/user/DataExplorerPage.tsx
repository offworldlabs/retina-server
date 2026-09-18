import { useCallback, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { StatCard } from "../../components/StatCard";
import { formatBytes } from "../../utils/format";
import { entryForScope, horizon } from "./dataExplorer/archive";
import { AvailabilityTimeline } from "./dataExplorer/AvailabilityTimeline";
import { DateRangeControls } from "./dataExplorer/DateRangeControls";
import { daysBetween, todayUTC } from "./dataExplorer/dates";
import { makePredicate } from "./dataExplorer/filters";
import { JSON_FACTOR, type ArchiveFile } from "./dataExplorer/keys";
import { NearControls } from "./dataExplorer/NearControls";
import { NodeMap } from "./dataExplorer/NodeMap";
import { NodePicker } from "./dataExplorer/NodePicker";
import { anyNodeSynthetic, effectiveNodeIds, isSynthetic } from "./dataExplorer/nodes";
import { ResultsTree, type SortKey } from "./dataExplorer/ResultsTree";
import { useArchiveScan } from "./dataExplorer/useArchiveScan";
import { useNodeRegistry } from "./dataExplorer/useNodeRegistry";
import {
  DEFAULT_RADIUS_KM,
  defaultFilters,
  readFilters,
  writeFilters,
} from "./dataExplorer/urlState";

import "./dataExplorer/dataExplorer.css";

export default function DataExplorerPage() {
  const [search, setSearch] = useSearchParams();
  const [sort, setSort] = useState<SortKey>("span");
  // Off by default: the map is a second way to say what the coordinate fields
  // already say, and it is half the height of the page when it is open.
  const [mapOpen, setMapOpen] = useState(false);
  // The radius to apply to the next centre. It cannot live in the query string,
  // which spells a radius only as part of `near`, and it has to outlive the gap
  // between choosing a distance and choosing a point to measure it from.
  const [pendingKm, setPendingKm] = useState(DEFAULT_RADIUS_KM);

  const today = useMemo(() => todayUTC(), []);
  const filters = useMemo(() => readFilters(search.toString(), today), [search, today]);

  const setFilters = useCallback(
    (next) => setSearch(writeFilters(next), { replace: true }),
    [setSearch],
  );

  // The future holds no archive, so those days are never listed.
  const days = useMemo(
    () => daysBetween(filters.from, filters.to).filter((d) => d <= today),
    [filters.from, filters.to, today],
  );

  // `readFilters` builds a new Set each time the query string is touched, and
  // the scan's effect compares by identity, so the selection is memoised on
  // its contents rather than on the object.
  const nodeSelKey = filters.nodeSel ? Array.from(filters.nodeSel).sort().join(",") : "*";
  const nodeSel = useMemo(
    () => (nodeSelKey === "*" ? null : new Set(nodeSelKey.split(","))),
    [nodeSelKey],
  );

  const scan = useArchiveScan(days, nodeSel);

  const registry = useNodeRegistry();

  // Which cached listing is allowed to answer. This has to be what was asked
  // for, never what survives the filters: a radius that narrows the view to a
  // single node has not changed the request, and that node's listing cannot
  // stand in for the whole fleet's.
  const entryScope = useMemo(() => nodeSel ?? new Set<string>(), [nodeSel]);

  // What the page filters and counts by, which the radius does narrow.
  const effective = useMemo(
    () => effectiveNodeIds(registry.nodes, scan.nodeIds, nodeSel, filters.near),
    [registry.nodes, scan.nodeIds, nodeSel, filters.near],
  );

  const entryFor = useCallback(
    (day: string) => entryForScope(scan.entries, day, entryScope),
    [scan.entries, entryScope],
  );

  // Files this node has in the range, regardless of every other filter. A
  // count that fell to zero the moment you deselected the node would say
  // nothing about whether to select it again.
  const fileCountFor = useCallback(
    (id: string) => {
      let n = 0;
      for (const day of days) {
        const entry = entryForScope(scan.entries, day, entryScope);
        if (!entry || entry.status !== "done") continue;
        for (const f of entry.files) if (f.day === day && f.node === id) n += 1;
      }
      return n;
    },
    [days, scan.entries, entryScope],
  );

  const { byDay, matched } = useMemo(() => {
    const predicate = makePredicate(filters, effective);
    const out = new Map<string, ArchiveFile[]>();
    const all: ArchiveFile[] = [];
    for (const day of days) {
      const entry = entryForScope(scan.entries, day, entryScope);
      if (!entry || entry.status !== "done") continue;
      const files = entry.files.filter((f) => f.day === day && predicate(f));
      out.set(day, files);
      all.push(...files);
    }
    return { byDay: out, matched: all };
  }, [days, scan.entries, entryScope, effective, filters]);

  const effectiveIds = useMemo(() => Array.from(effective).sort(), [effective]);
  const synthetic = useCallback((id: string) => isSynthetic(registry.nodes, id), [registry.nodes]);
  const anySynthetic = useMemo(
    () => anyNodeSynthetic(registry.nodes, scan.nodeIds),
    [registry.nodes, scan.nodeIds],
  );

  const listed = days.filter((d) => entryFor(d)?.status === "done").length;
  const failed = days.filter((d) => entryFor(d)?.status === "error").length;
  const bytes = matched.reduce((sum, f) => sum + f.size, 0);
  const nodesWithData = new Set(matched.map((f) => f.node)).size;
  const coldFrom = horizon(scan.entries, days, entryScope);
  const shareQuery = writeFilters(filters).toString();

  return (
    <>
      <div className="page-header">
        <h1>Data Explorer</h1>
        <p>Browse and download the detection archive</p>
      </div>

      {coldFrom && (
        <div className="de-notice">
          Files before {coldFrom} have been moved to cold storage and are not served here.
        </div>
      )}

      {registry.error && (
        <div className="de-notice">
          The node list could not be loaded ({registry.error}), so names and positions are missing
          and the radius filter has nothing to measure against.
          <button type="button" className="btn btn-secondary btn-sm" onClick={registry.retry}>
            Retry
          </button>
        </div>
      )}

      <div className="stats-grid">
        <StatCard
          label="Files matching"
          value={<span data-testid="de-stat-files">{matched.length.toLocaleString()}</span>}
          tone="accent"
          sub={`${listed} of ${days.length} days listed${failed ? ` · ${failed} failed` : ""}`}
        />
        <StatCard
          label="Stored size"
          value={<span data-testid="de-stat-bytes">{formatBytes(bytes)}</span>}
          sub={`≈ ${formatBytes(bytes * JSON_FACTOR)} as JSON (≈ ${JSON_FACTOR}×)`}
        />
        <StatCard
          label="Nodes with data"
          value={nodesWithData}
          tone="success"
          sub={`of ${registry.nodes.size} known`}
        />
        <StatCard
          label="Range"
          value={`${days.length}d`}
          sub={`${filters.from} → ${filters.to} UTC`}
          tone="warning"
        />
      </div>

      <div className="card de-card">
        <div className="card-header">
          <h3>Filters</h3>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => setFilters(defaultFilters(today))}
          >
            Reset
          </button>
        </div>
        <div className="card-body">
          <div className="de-filters">
            <NodePicker
              filters={filters}
              nodes={registry.nodes}
              discovered={scan.nodeIds}
              fileCountFor={fileCountFor}
              onChange={setFilters}
            />
            <DateRangeControls filters={filters} today={today} onChange={setFilters} />
            <NearControls
              filters={filters}
              radiusKm={filters.near?.km ?? pendingKm}
              onRadiusChange={setPendingKm}
              mapOpen={mapOpen}
              onToggleMap={() => setMapOpen(!mapOpen)}
              onChange={setFilters}
            />
          </div>
          <div className="de-urlbar">
            <span>Shareable:</span>
            <code className="mono" data-testid="de-share">?{shareQuery}</code>
          </div>
        </div>
        {mapOpen && (
          <NodeMap
            filters={filters}
            nodes={registry.nodes}
            effective={effective}
            radiusKm={filters.near?.km ?? pendingKm}
            loading={registry.loading}
            onChange={setFilters}
          />
        )}
      </div>

      <AvailabilityTimeline
        filters={filters}
        today={today}
        ids={effectiveIds}
        files={matched}
        synthetic={synthetic}
        anySynthetic={anySynthetic}
        onChange={setFilters}
      />

      <div className="card">
        <ResultsTree
          days={days}
          byDay={byDay}
          entryFor={entryFor}
          sort={sort}
          onSort={setSort}
          onRetry={scan.retry}
        />
      </div>
    </>
  );
}
