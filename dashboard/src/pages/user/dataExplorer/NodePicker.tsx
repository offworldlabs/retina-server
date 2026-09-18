/**
 * Which nodes to look at. The list is the registry plus anything an archive
 * key has named, so a node the fleet no longer lists is still reachable
 * through its own files.
 */

import { useEffect, useMemo, useRef, useState } from "react";

import { isSyntheticNode } from "../../../utils/nodeKind";
import { knownNodeIds, type RegistryNode } from "./nodes";
import type { ExplorerFilters } from "./urlState";

interface Props {
  filters: ExplorerFilters;
  nodes: Map<string, RegistryNode>;
  /** Ids seen in archive keys, which may not be in the registry. */
  discovered: Set<string>;
  /** Files this node has under the current range, ignoring the node filter
   *  itself, so a count does not fall to zero the moment you deselect it. */
  fileCountFor: (id: string) => number;
  onChange: (next: ExplorerFilters) => void;
}

export function NodePicker({ filters, nodes, discovered, fileCountFor, onChange }: Props) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const root = useRef<HTMLDivElement>(null);

  // The panel floats over the map below it, so it has to be dismissable
  // without going back to the button that opened it.
  useEffect(() => {
    if (!open) return;
    const close = (event: Event) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const ids = useMemo(() => knownNodeIds(nodes, discovered), [nodes, discovered]);
  const { nodeSel } = filters;
  const select = (next: Set<string> | null) => onChange({ ...filters, nodeSel: next });

  const nameOf = (id: string) => nodes.get(id)?.name || id;

  // A node seen only in an archive key has no registry entry and so no server
  // flag; its prefix is the only thing left to classify it by.
  const synthetic = (id: string) => {
    const known = nodes.get(id);
    return known ? known.synthetic : isSyntheticNode({}, id);
  };
  const anySynthetic = ids.some(synthetic);

  const needle = search.trim().toLowerCase();
  const visible = needle
    ? ids.filter((id) => `${id} ${nameOf(id)}`.toLowerCase().includes(needle))
    : ids;

  const toggle = (id: string) => {
    // A tick on an all-nodes view has to name every other node: null means
    // "and whatever turns up later", which cannot survive an exclusion.
    const next = new Set(nodeSel ?? ids);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    select(next);
  };

  return (
    <div className="de-fgroup de-nodes" ref={root}>
      {/* Deliberately unassociated: a `for` on a button makes the label its
          accessible name, which would hide the selection the button reports. */}
      <label>Nodes</label>
      <button
        type="button"
        className="de-select"
        aria-haspopup="true"
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <span>
          {nodeSel === null ? "All nodes" : `${nodeSel.size} of ${ids.length} nodes`}
        </span>
        <span className="de-muted" aria-hidden="true">▾</span>
      </button>

      {open && (
        <div className="de-node-panel">
          <input
            id="de-node-search"
            type="search"
            placeholder="Search node id…"
            aria-label="Search node id"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />

          <ul className="de-node-list">
            {visible.map((id) => (
              <li key={id}>
                <label>
                  <input
                    type="checkbox"
                    checked={nodeSel === null || nodeSel.has(id)}
                    onChange={() => toggle(id)}
                  />
                  <span className="de-node-name mono">{nameOf(id)}</span>
                  <span className="de-node-count">{fileCountFor(id) || ""}</span>
                </label>
              </li>
            ))}
            {visible.length === 0 && (
              <li className="de-node-empty">
                {ids.length ? "No node id matches that." : "No nodes yet."}
              </li>
            )}
          </ul>

          <div className="de-node-actions">
            <button type="button" className="btn btn-secondary btn-sm" onClick={() => select(null)}>
              All
            </button>
            {anySynthetic && (
              <button
                type="button"
                className="btn btn-secondary btn-sm"
                onClick={() => select(new Set(ids.filter((id) => !synthetic(id))))}
              >
                Real only
              </button>
            )}
            <button
              type="button"
              className="btn btn-secondary btn-sm"
              onClick={() => select(new Set())}
            >
              None
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
