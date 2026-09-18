import { useEffect, useMemo, useRef, useState } from "react";

import { formatBytes } from "../../../utils/format";
import type { DayEntry } from "./archive";
import { hhmm } from "./dates";
import { JSON_FACTOR, type ArchiveFile } from "./keys";

/** Above this, a day opens collapsed: the alternative is a wall of rows. */
export const AUTO_COLLAPSE_NODES = 3;

export type SortKey = "span" | "size" | "name";

const SORTS: Record<SortKey, (a: ArchiveFile, b: ArchiveFile) => number> = {
  span: (a, b) => b.endMs - a.endMs,
  size: (a, b) => b.size - a.size,
  name: (a, b) => a.name.localeCompare(b.name),
};

const COLUMNS: { key: SortKey; label: string }[] = [
  { key: "name", label: "File" },
  { key: "span", label: "Coverage" },
  { key: "size", label: "Size" },
];

interface Props {
  /** Ascending; rendered newest first. */
  days: string[];
  byDay: Map<string, ArchiveFile[]>;
  entryFor: (day: string) => DayEntry | null;
  sort: SortKey;
  onSort: (key: SortKey) => void;
  onRetry: (day: string) => void;
}

const nodeKey = (day: string, node: string) => `${day}|${node}`;
const total = (files: ArchiveFile[]) => files.reduce((sum, f) => sum + f.size, 0);

export function ResultsTree({ days, byDay, entryFor, sort, onSort, onRetry }: Props) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  // The node set last seen per day. The collapse default is applied when that
  // changes, so a group the reader opened survives the listings still landing.
  const signatures = useRef<Map<string, string>>(new Map());

  const grouped = useMemo(() => {
    const out = new Map<string, Map<string, ArchiveFile[]>>();
    for (const [day, files] of byDay) {
      const byNode = new Map<string, ArchiveFile[]>();
      for (const f of files) {
        if (!byNode.has(f.node)) byNode.set(f.node, []);
        byNode.get(f.node).push(f);
      }
      for (const list of byNode.values()) list.sort(SORTS[sort]);
      out.set(day, byNode);
    }
    return out;
  }, [byDay, sort]);

  useEffect(() => {
    const changed: { day: string; nodes: string[] }[] = [];
    for (const [day, byNode] of grouped) {
      const nodes = Array.from(byNode.keys()).sort();
      // A day with nothing to show under the current filters does not
      // re-decide the layout: recording its (empty) signature would make the
      // filters matching something again read as a change and re-collapse
      // whatever the reader opened.
      if (nodes.length === 0) continue;
      const signature = nodes.join(",");
      if (signatures.current.get(day) === signature) continue;
      signatures.current.set(day, signature);
      changed.push({ day, nodes });
    }
    if (!changed.length) return;

    setCollapsed((prev) => {
      const next = new Set(prev);
      for (const { day, nodes } of changed) {
        const many = nodes.length > AUTO_COLLAPSE_NODES;
        for (const node of nodes) {
          if (many) next.add(nodeKey(day, node));
          else next.delete(nodeKey(day, node));
        }
      }
      return next;
    });
  }, [grouped]);

  const toggle = (key: string) =>
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (!next.delete(key)) next.add(key);
      return next;
    });

  // A day collapses as a whole; its nodes keep whatever they had, so opening
  // the day again does not undo a reader's choices inside it.
  const collapseAll = () => setCollapsed((prev) => new Set([...prev, ...days]));

  const matched = Array.from(byDay.values()).flat();

  return (
    <div className="de-tree">
      <div className="card-header">
        <h3>Archived detections</h3>
        <div className="de-frow">
          <span className="mono de-muted">
            {matched.length} files · {formatBytes(total(matched))}
          </span>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={() => setCollapsed(new Set())}
          >
            Expand all
          </button>
          <button type="button" className="btn btn-secondary btn-sm" onClick={collapseAll}>
            Collapse all
          </button>
        </div>
      </div>

      <div className="de-colhead">
        {COLUMNS.map((c) => (
          <span key={c.key}>
            <button type="button" onClick={() => onSort(c.key)} aria-pressed={sort === c.key}>
              {c.label}
            </button>
          </span>
        ))}
        <span>Download</span>
      </div>

      {days.length === 0 && <div className="de-day-state">Pick a date range.</div>}

      {[...days].reverse().map((day) => {
        const entry = entryFor(day);
        const byNode = grouped.get(day) || new Map<string, ArchiveFile[]>();
        const files = byDay.get(day) || [];
        const dayCollapsed = collapsed.has(day);

        return (
          <div key={day} className={`de-group${dayCollapsed ? " collapsed" : ""}`}>
            <button
              type="button"
              className="de-day-head"
              aria-expanded={!dayCollapsed}
              onClick={() => toggle(day)}
            >
              <span className="de-caret" aria-hidden="true">▾</span>
              <span className="mono">{day}</span>
              {entry?.status === "error" && <span className="de-day-state err">failed</span>}
              {(!entry || entry.status === "loading") && (
                <span className="de-day-state">Listing {day}…</span>
              )}
              <span className="de-spacer">
                {files.length} file{files.length === 1 ? "" : "s"} · {formatBytes(total(files))}
                {byNode.size > AUTO_COLLAPSE_NODES && ` · ${byNode.size} nodes`}
              </span>
            </button>

            {!dayCollapsed && entry?.status === "error" && (
              <div className="de-day-state err" style={{ padding: "8px 32px" }}>
                Could not list this day: {entry.error}{" "}
                <button type="button" className="btn btn-secondary btn-sm" onClick={() => onRetry(day)}>
                  Retry
                </button>
              </div>
            )}

            {!dayCollapsed && entry?.status === "done" && files.length === 0 && (
              <div className="de-day-state" style={{ padding: "8px 32px" }}>
                No files for this day under the current filters.
              </div>
            )}

            {!dayCollapsed &&
              Array.from(byNode.entries()).map(([node, nodeFiles]) => {
                const key = nodeKey(day, node);
                const nodeCollapsed = collapsed.has(key);
                return (
                  <div key={key} className={`de-group${nodeCollapsed ? " collapsed" : ""}`}>
                    <button
                      type="button"
                      className="de-node-head"
                      aria-expanded={!nodeCollapsed}
                      onClick={() => toggle(key)}
                    >
                      <span className="de-caret" aria-hidden="true">▾</span>
                      <span className="mono">{node}</span>
                      <span className="de-spacer">
                        {nodeFiles.length} · {formatBytes(total(nodeFiles))}
                      </span>
                    </button>

                    {!nodeCollapsed &&
                      nodeFiles.map((f) => (
                        <div key={f.key} className="de-file" data-testid="de-file">
                          <span className="mono">{f.name}</span>
                          <span>
                            {hhmm(f.startMs)} → {hhmm(f.endMs)}
                            <span className="de-est">est.</span>
                          </span>
                          <span>
                            {formatBytes(f.size)}
                            <span className="de-est">≈ {formatBytes(f.size * JSON_FACTOR)} JSON</span>
                          </span>
                          <a
                            className="btn btn-outline btn-sm"
                            href={`/api/data/archive/${f.key}`}
                            target="_blank"
                            rel="noreferrer"
                          >
                            JSON
                          </a>
                        </div>
                      ))}
                  </div>
                );
              })}
          </div>
        );
      })}
    </div>
  );
}
