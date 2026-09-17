import { useCallback, useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";

import { StatCard } from "../../components/StatCard";
import { formatBytes } from "../../utils/format";
import { entryForScope, horizon } from "./dataExplorer/archive";
import { DateRangeControls } from "./dataExplorer/DateRangeControls";
import { daysBetween, todayUTC } from "./dataExplorer/dates";
import { makePredicate } from "./dataExplorer/filters";
import { JSON_FACTOR, type ArchiveFile } from "./dataExplorer/keys";
import { ResultsTree, type SortKey } from "./dataExplorer/ResultsTree";
import { useArchiveScan } from "./dataExplorer/useArchiveScan";
import { readFilters, writeFilters } from "./dataExplorer/urlState";

import "./dataExplorer/dataExplorer.css";

export default function DataExplorerPage() {
  const [search, setSearch] = useSearchParams();
  const [sort, setSort] = useState<SortKey>("span");

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

  // What the page filters and counts by. The scan derives its own copy for
  // deciding what to fetch; this one is for display.
  const effective = useMemo(
    () => nodeSel || scan.nodeIds,
    [nodeSel, scan.nodeIds],
  );

  const entryFor = useCallback(
    (day: string) => entryForScope(scan.entries, day, effective),
    [scan.entries, effective],
  );

  const { byDay, matched } = useMemo(() => {
    const predicate = makePredicate(filters, effective);
    const out = new Map<string, ArchiveFile[]>();
    const all: ArchiveFile[] = [];
    for (const day of days) {
      const entry = entryForScope(scan.entries, day, effective);
      if (!entry || entry.status !== "done") continue;
      const files = entry.files.filter((f) => f.day === day && predicate(f));
      out.set(day, files);
      all.push(...files);
    }
    return { byDay: out, matched: all };
  }, [days, scan.entries, effective, filters]);

  const listed = days.filter((d) => entryFor(d)?.status === "done").length;
  const failed = days.filter((d) => entryFor(d)?.status === "error").length;
  const bytes = matched.reduce((sum, f) => sum + f.size, 0);
  const nodesWithData = new Set(matched.map((f) => f.node)).size;
  const coldFrom = horizon(scan.entries, days, effective);
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
        <StatCard label="Nodes with data" value={nodesWithData} tone="success" />
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
          <code className="mono" data-testid="de-share">?{shareQuery}</code>
        </div>
        <DateRangeControls filters={filters} today={today} onChange={setFilters} />
      </div>

      <div className="card">
        <div className="card-header">
          <h3>Archived detections</h3>
          <span className="mono">
            {matched.length} files · {formatBytes(bytes)}
          </span>
        </div>
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
