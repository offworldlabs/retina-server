import { type ReactElement, type Ref, type SyntheticEvent, useEffect, useId, useMemo, useRef, useState } from "react";

import { Sheet } from "../../../components/Sheet";
import { useMediaQuery } from "../../../hooks/useMediaQuery";
import type { Scope } from "../../../hooks/useScopeParam";
import { PHONE } from "../../../styles/breakpoints";
import { formatBytes } from "../../../utils/format";
import { cutName } from "../../../utils/nameCut";
import { type ArchiveSource, type DayEntry, filePath } from "./archive";
import { hhmm } from "./dates";
import { JSON_FACTOR, type ArchiveFile } from "./keys";
import { NodeMark } from "./NodeMark";
import { isSynthetic, type NodeMark as Mark } from "./nodes";
import { type MapTarget, showOnMap } from "./showOnMap";
import "./resultsTree.phone.css";

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
  /** The basket, by archive key. */
  selected: Set<string>;
  onSelect: (keys: string[], on: boolean) => void;
  /** The file open in the preview drawer. */
  active: string | null;
  onPreview: (key: string) => void;
  /** What each node is to the signed-in owner, drawn on its group's head. */
  marks?: ReadonlyMap<string, Mark>;
  /** The archive the tree lists, whose route serves its files: a private
   *  node's file is the owner's alone, and its key names the node. */
  source?: ArchiveSource;
  /** Whose archive is on screen, and with `privateRefs`, the owner's nodes
   *  the public map leaves out: what a file's "Show on map" may offer. */
  scope?: Scope;
  privateRefs?: ReadonlySet<string>;
  /** Whether a node is synthetic, by the scope's own registry. */
  synthetic?: (id: string) => boolean;
  /** The tree's heading, which a phone can focus from script. */
  headingRef?: Ref<HTMLHeadingElement>;
}

const NO_MARKS: ReadonlyMap<string, Mark> = new Map();
const NO_REFS: ReadonlySet<string> = new Set();
const NO_NODES = new Map();
/** With no registry, a node is synthetic by its prefix alone. */
const byPrefix = (id: string) => isSynthetic(NO_NODES, id);

const nodeKey = (day: string, node: string) => `${day}|${node}`;
const total = (files: ArchiveFile[]) => files.reduce((sum, f) => sum + f.size, 0);
const keysOf = (files: ArchiveFile[]) => files.map((f) => f.key);

/** The heads and the rows answer a click as a whole, so the controls inside
 *  them keep theirs to themselves. */
const stop = (e: SyntheticEvent) => e.stopPropagation();

/** The file on the archive map at its first frame, in a new tab, or a
 *  disabled control that says why it cannot be. */
function ShowOnMap({ name, target }: { name: string; target: MapTarget }) {
  const label = `Show on map: ${name}`;
  if ("href" in target) {
    return (
      <a
        className="btn btn-secondary btn-sm"
        href={target.href}
        target="_blank"
        rel="noreferrer"
        aria-label={label}
        onClick={stop}
      >
        Show on map
      </a>
    );
  }
  // Disabled rather than aria-disabled: the shared .btn takes an aria-disabled
  // button's pointer events, and with them the title that gives the reason.
  // React runs no handler of a disabled button's own but still hands its
  // click on to the row, so the wrapper keeps it.
  return (
    <span className="de-map-off" onClick={stop}>
      <button type="button" className="btn btn-secondary btn-sm" disabled title={target.reason} aria-label={label}>
        Show on map
      </button>
    </span>
  );
}

/** A checkbox on a phone, where its glyph alone is too small to land on: a
 *  label a fingertip's size around it. The label keeps the tap to itself as
 *  the box does, so a tap anywhere on it ticks and never toggles the group or
 *  opens the preview around it. */
function Target({ children }: { children: ReactElement }) {
  return (
    <label className="de-check" onClick={stop}>
      {children}
    </label>
  );
}

interface CardProps {
  file: ArchiveFile;
  active: boolean;
  selected: boolean;
  /** Whether this card's actions menu is the one open. */
  menuOpen: boolean;
  onSelect: (keys: string[], on: boolean) => void;
  onPreview: (key: string) => void;
  onMenu: (key: string) => void;
}

/** A file on a phone, where the desktop's row of columns does not fit: its
 *  name over its span and size, between its checkbox and a menu that holds
 *  the row's actions. A tap on the card opens the preview, as the row's click
 *  does. */
function FileCard({ file: f, active, selected, menuOpen, onSelect, onPreview, onMenu }: CardProps) {
  const menuButton = useRef<HTMLButtonElement>(null);
  const { head, tail } = cutName(f.name);
  return (
    <div
      className={`de-filecard${active ? " active" : ""}`}
      data-testid="de-file"
      onClick={() => {
        // The preview hands focus back to what held it when it opened, and a
        // tapped card can't hold it, so the card's own actions button does.
        menuButton.current?.focus({ preventScroll: true });
        onPreview(f.key);
      }}
    >
      <Target>
        <input
          type="checkbox"
          checked={selected}
          aria-label={`Select ${f.name}`}
          onClick={stop}
          onChange={(e) => onSelect([f.key], e.target.checked)}
        />
      </Target>
      {/* Cut in the middle rather than at the end: a node's files share
          their first characters, and the end is what tells them apart. */}
      <span className="de-fname mono" title={f.name}>
        <span className="de-fname-head">{head}</span>
        <span className="de-fname-tail">{tail}</span>
      </span>
      <button
        ref={menuButton}
        type="button"
        className="de-file-menu"
        aria-label={`Actions for ${f.name}`}
        aria-haspopup="dialog"
        aria-expanded={menuOpen}
        onClick={(e) => {
          stop(e);
          // The sheet hands focus back to whatever held it when it opened,
          // and Safari leaves focus where it was on a tap: taken here, it
          // comes back to this button when the menu closes.
          e.currentTarget.focus();
          onMenu(f.key);
        }}
      >
        <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
          <circle cx="12" cy="5" r="1.75" fill="currentColor" />
          <circle cx="12" cy="12" r="1.75" fill="currentColor" />
          <circle cx="12" cy="19" r="1.75" fill="currentColor" />
        </svg>
      </button>
      {/* No JSON estimate here: beside the span it would not fit a phone's
          line. The menu gives it, against Download JSON. */}
      <span className="de-filecard-meta">
        <span>
          {hhmm(f.startMs)} → {hhmm(f.endMs)}
          <span className="de-est">est.</span>
        </span>
        <span>{formatBytes(f.size)}</span>
      </span>
    </div>
  );
}

interface ActionsProps {
  file: ArchiveFile;
  inBasket: boolean;
  /** Where the file downloads as JSON. */
  href: string;
  map: MapTarget;
  onClose: () => void;
  onPreview: (key: string) => void;
  onSelect: (keys: string[], on: boolean) => void;
}

/** A file's actions on a phone, in a sheet from the foot of the screen: what
 *  the desktop row lays out along its end, plus the basket. Every action
 *  closes the sheet, which hands focus back to the menu button. */
function FileActions({ file: f, inBasket, href, map, onClose, onPreview, onSelect }: ActionsProps) {
  const reasonId = useId();
  return (
    <Sheet
      open
      onClose={onClose}
      variant="bottom"
      className="de-file-actions"
      title={f.name}
      description={`${hhmm(f.startMs)} → ${hhmm(f.endMs)} est. · ${formatBytes(f.size)}`}
    >
      <div className="de-file-action-list">
        <button
          type="button"
          className="de-file-action"
          onClick={() => {
            // Closed in the same update as the preview opens, so focus is
            // back on the menu button by the time the preview takes it, and
            // returns there when the preview closes.
            onClose();
            onPreview(f.key);
          }}
        >
          Preview
        </button>
        <a className="de-file-action" href={href} target="_blank" rel="noreferrer" onClick={onClose}>
          <span>Download JSON</span>{" "}
          <span className="de-file-action-note">≈ {formatBytes(f.size * JSON_FACTOR)}</span>
        </a>
        <button
          type="button"
          className="de-file-action"
          onClick={() => {
            onClose();
            onSelect([f.key], !inBasket);
          }}
        >
          {inBasket ? "Remove from basket" : "Add to basket"}
        </button>
        {"href" in map ? (
          <a className="de-file-action" href={map.href} target="_blank" rel="noreferrer" onClick={onClose}>
            Show on map
          </a>
        ) : (
          // The reason in words under the control: the desktop's title
          // tooltip never shows on a touch screen.
          <>
            <button type="button" className="de-file-action" disabled aria-describedby={reasonId}>
              Show on map
            </button>
            <p id={reasonId} className="de-file-action-reason">
              {map.reason}
            </p>
          </>
        )}
      </div>
    </Sheet>
  );
}

export function ResultsTree({
  days,
  byDay,
  entryFor,
  sort,
  onSort,
  onRetry,
  selected,
  onSelect,
  active,
  onPreview,
  marks = NO_MARKS,
  source = "public",
  scope = "network",
  privateRefs = NO_REFS,
  synthetic = byPrefix,
  headingRef,
}: Props) {
  const [collapsed, setCollapsed] = useState<Set<string>>(new Set());
  // The node set last seen per day. The collapse default is applied when that
  // changes, so a group the reader opened survives the listings still landing.
  const signatures = useRef<Map<string, string>>(new Map());
  const isPhone = useMediaQuery(PHONE);
  const sortId = useId();
  // The file whose actions menu is open, by key: one at a time, on a phone.
  const [menuFor, setMenuFor] = useState<string | null>(null);

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

  const mapScope = { scope, privateRefs, synthetic };
  const matched = Array.from(byDay.values()).flat();
  const allSelected = (files: ArchiveFile[]) =>
    files.length > 0 && files.every((f) => selected.has(f.key));

  // The menu belongs to a phone's card. Past the phone width, or once the
  // listing no longer holds its file, it closes, and is not left behind to
  // open again when either comes back.
  const menuFile = isPhone && menuFor !== null ? matched.find((f) => f.key === menuFor) : undefined;
  if (menuFor !== null && !menuFile) setMenuFor(null);
  const closeMenu = () => setMenuFor(null);

  const check = (box: ReactElement) => (isPhone ? <Target>{box}</Target> : box);

  return (
    <div className="de-tree">
      <div className="card-header">
        {/* Where a phone's focus comes when the basket's bar, and its sheet
            with it, go as the basket empties: the files are ticked again from
            here. A desktop's bar stays, and keeps focus. */}
        <h3 ref={headingRef} tabIndex={isPhone ? -1 : undefined}>
          Archived detections
        </h3>
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
          <button
            type="button"
            className="btn btn-primary btn-sm"
            disabled={!matched.length}
            onClick={() => onSelect(keysOf(matched), true)}
          >
            Select all matching
          </button>
        </div>
      </div>

      {isPhone ? (
        // A card has no column to head, so its sorts are a select.
        <div className="de-sortbar">
          <label htmlFor={sortId}>Sort by</label>
          <select
            id={sortId}
            className="input de-sort"
            value={sort}
            onChange={(e) => {
              const picked = COLUMNS.find((c) => c.key === e.target.value);
              if (picked) onSort(picked.key);
            }}
          >
            {COLUMNS.map((c) => (
              <option key={c.key} value={c.key}>
                {c.label}
              </option>
            ))}
          </select>
        </div>
      ) : (
        <div className="de-colhead">
          <span aria-hidden="true" />
          {COLUMNS.map((c) => (
            <span key={c.key}>
              <button type="button" onClick={() => onSort(c.key)} aria-pressed={sort === c.key}>
                {c.label}
              </button>
            </span>
          ))}
          <span>Actions</span>
        </div>
      )}

      {days.length === 0 && <div className="de-day-state">Pick a date range.</div>}

      {[...days].reverse().map((day) => {
        const entry = entryFor(day);
        const byNode = grouped.get(day) || new Map<string, ArchiveFile[]>();
        const files = byDay.get(day) || [];
        const dayCollapsed = collapsed.has(day);

        return (
          <div key={day} className={`de-group${dayCollapsed ? " collapsed" : ""}`}>
            <div className="de-day-head" onClick={() => toggle(day)}>
              <span className="de-caret" aria-hidden="true">▾</span>
              {check(
                <input
                  type="checkbox"
                  checked={allSelected(files)}
                  disabled={!files.length}
                  aria-label={`Select every file on ${day}`}
                  onClick={stop}
                  onChange={(e) => onSelect(keysOf(files), e.target.checked)}
                />,
              )}
              <button type="button" className="de-toggle" aria-expanded={!dayCollapsed}>
                <span className="mono">{day}</span>
                {entry?.status === "error" && <span className="de-day-state err">failed</span>}
                {(!entry || entry.status === "loading") && (
                  // A phone's head has the day beside it already, and no room
                  // to say it twice.
                  <span className="de-day-state">{isPhone ? "Listing…" : <>Listing {day}…</>}</span>
                )}
                <span className="de-spacer">
                  {files.length} file{files.length === 1 ? "" : "s"} · {formatBytes(total(files))}
                  {byNode.size > AUTO_COLLAPSE_NODES && ` · ${byNode.size} nodes`}
                </span>
              </button>
            </div>

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
                    <div className="de-node-head" onClick={() => toggle(key)}>
                      <span className="de-caret" aria-hidden="true">▾</span>
                      {check(
                        <input
                          type="checkbox"
                          checked={allSelected(nodeFiles)}
                          aria-label={`Select every file from ${node} on ${day}`}
                          onClick={stop}
                          onChange={(e) => onSelect(keysOf(nodeFiles), e.target.checked)}
                        />,
                      )}
                      <button type="button" className="de-toggle" aria-expanded={!nodeCollapsed}>
                        <span className="mono">{node}</span>
                        <NodeMark mark={marks.get(node)} />
                        <span className="de-spacer">
                          {nodeFiles.length} · {formatBytes(total(nodeFiles))}
                        </span>
                      </button>
                    </div>

                    {!nodeCollapsed &&
                      nodeFiles.map((f) =>
                        isPhone ? (
                          <FileCard
                            key={f.key}
                            file={f}
                            active={f.key === active}
                            selected={selected.has(f.key)}
                            menuOpen={f.key === menuFor}
                            onSelect={onSelect}
                            onPreview={onPreview}
                            onMenu={setMenuFor}
                          />
                        ) : (
                          <div
                            key={f.key}
                            className={`de-file${f.key === active ? " active" : ""}`}
                            data-testid="de-file"
                            onClick={() => onPreview(f.key)}
                          >
                            <input
                              type="checkbox"
                              checked={selected.has(f.key)}
                              aria-label={`Select ${f.name}`}
                              onClick={stop}
                              onChange={(e) => onSelect([f.key], e.target.checked)}
                            />
                            <span className="mono">{f.name}</span>
                            <span>
                              {hhmm(f.startMs)} → {hhmm(f.endMs)}
                              <span className="de-est">est.</span>
                            </span>
                            <span>
                              {formatBytes(f.size)}
                              <span className="de-est">≈ {formatBytes(f.size * JSON_FACTOR)} JSON</span>
                            </span>
                            <span className="de-acts">
                              <button
                                type="button"
                                className="btn btn-secondary btn-sm"
                                aria-label={`Preview ${f.name}`}
                                onClick={(e) => {
                                  stop(e);
                                  onPreview(f.key);
                                }}
                              >
                                Preview
                              </button>
                              <a
                                className="btn btn-secondary btn-sm"
                                href={filePath(f.key, source)}
                                target="_blank"
                                rel="noreferrer"
                                onClick={stop}
                              >
                                JSON
                              </a>
                              <ShowOnMap name={f.name} target={showOnMap(f, mapScope)} />
                            </span>
                          </div>
                        ),
                      )}
                  </div>
                );
              })}
          </div>
        );
      })}

      {menuFile && (
        <FileActions
          file={menuFile}
          inBasket={selected.has(menuFile.key)}
          href={filePath(menuFile.key, source)}
          map={showOnMap(menuFile, mapScope)}
          onClose={closeMenu}
          onPreview={onPreview}
          onSelect={onSelect}
        />
      )}
    </div>
  );
}
