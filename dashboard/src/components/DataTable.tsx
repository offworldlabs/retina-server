import { useId, useLayoutEffect, useRef, type ReactNode } from "react";

import { useMediaQuery } from "../hooks/useMediaQuery";
import { PHONE, TABLET } from "../styles/breakpoints";
import "./dataTable.css";

/** Where a column goes when the screen is too narrow for the whole table. At
 *  1024px and up every column keeps its place whatever its hint.
 *  - `title`: heads the row's card on a phone, first and across it, unlabelled.
 *  - `primary`: a labelled line on the card. A column with no hint is one too.
 *  - `secondary`: a labelled line a phone keeps behind the card's More button.
 *  - `hidden`: left off the card on a phone, and out of the table on a tablet. */
export type ColumnHint = "title" | "primary" | "secondary" | "hidden";

/** One order a phone can put the rows in. */
export interface SortOption {
  value: string;
  label: string;
}

/** The sort a phone offers over its cards, which have no header to click. The
 *  page keeps the choice and sorts the rows itself. */
export interface PhoneSort {
  options: readonly SortOption[];
  /** The option in force. */
  value: string;
  onChange: (value: string) => void;
  /** The select's label, "Sort by" unless given. */
  label?: string;
}

interface Props {
  /** One entry per column, in order. */
  headers: ReactNode[];
  /** How many rows `children` holds. Zero shows the empty text, if any. */
  count: number;
  /** What to say across the table when there are no rows. Omit to show
   *  nothing, for a page that says so elsewhere. */
  empty?: ReactNode;
  /** A busy row in place of the rows, for a listing keyed on a page number. */
  loading?: boolean;
  /** How each column fits a narrow screen, in the order of `headers`. Without
   *  any, every column is a labelled line on a phone and stays on a tablet.
   *  Where no column is the title, a cell marked `.cell-primary`, the one its
   *  row is known by, heads the card. */
  hints?: readonly (ColumnHint | undefined)[];
  /** A Sort by select over the cards on a phone. */
  sort?: PhoneSort;
  /** How a phone draws the table: each row as a card of labelled lines, or the
   *  table itself, scrolling sideways, for one that reads better as a grid. */
  phone?: "cards" | "table";
  /** The `<tr>` rows. */
  children?: ReactNode;
}

/** What the cells are stamped from, as of the table's last render. */
interface Layout {
  /** Each column's header text, "" for an empty header. */
  labels: string[];
  hints: readonly (ColumnHint | undefined)[];
  /** Whether the rows are drawn as cards just now. */
  cards: boolean;
}

/* The rows are the caller's, often rendered by components and fragments the
   table never holds as elements, so the cards are drawn by dataTable.css from
   attributes on the rendered cells. After every render, and whenever the body
   changes on its own, each cell gets its column's header text (data-label) and
   hint (data-hint), the column found by summing the colSpans before it; an
   attribute the caller set is left as it is. On a phone each row with a
   secondary cell also gets a More cell of the table's own, kept last. React
   never refers to that node, so rows added, removed or reordered around it are
   none the wiser, and it goes when the screen widens. */

/** The attributes the table wrote, per element, to tell them from the caller's. */
const written = new WeakMap<Element, Map<string, string>>();

/** The More cells the table added to its rows. */
const moreCells = new WeakSet<Element>();

/** Sets one of the table's own attributes, or removes it for null. One the
 *  caller set is theirs, and stays. */
function put(el: Element, name: string, value: string | null) {
  const ours = written.get(el);
  const now = el.getAttribute(name);
  if (now !== null && now !== ours?.get(name)) return;
  if (value === null) {
    if (now !== null) el.removeAttribute(name);
    ours?.delete(name);
    return;
  }
  if (now !== value) el.setAttribute(name, value);
  if (ours) ours.set(name, value);
  else written.set(el, new Map([[name, value]]));
}

/** A card's More: a button that shows the row's secondary lines, or tucks
 *  them away again. Whether they show is the row's `data-card-open`. */
function moreCell(): HTMLTableCellElement {
  const cell = document.createElement("td");
  cell.setAttribute("data-card-more", "");
  cell.setAttribute("role", "cell");
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = "More";
  button.setAttribute("aria-expanded", "false");
  button.addEventListener("click", () => {
    const row = cell.parentElement;
    if (!row) return;
    const open = button.getAttribute("aria-expanded") !== "true";
    put(row, "data-card-open", open ? "" : null);
    button.setAttribute("aria-expanded", String(open));
    button.textContent = open ? "Less" : "More";
  });
  cell.append(button);
  moreCells.add(cell);
  return cell;
}

/** Brings every body row's attributes, and its More cell, up to `layout`. */
function stamp(table: HTMLTableElement, { labels, hints, cards }: Layout) {
  const titled = hints.includes("title");
  for (const row of Array.from(table.tBodies[0]?.rows ?? [])) {
    let column = 0;
    let folds = false;
    let more: Element | null = null;
    for (const cell of Array.from(row.cells)) {
      if (moreCells.has(cell)) {
        more = cell;
        continue;
      }
      // A cell across several columns, and the busy or empty note, span the
      // card with no label and no column's hint.
      const single = cell.colSpan === 1 && !cell.classList.contains("table-note");
      const hint = single
        ? (hints[column] ?? (!titled && cell.classList.contains("cell-primary") ? "title" : undefined))
        : undefined;
      put(cell, "data-label", single && labels[column] ? labels[column] : null);
      put(cell, "data-hint", hint ?? null);
      // A table drawn as blocks loses its table semantics in some browsers;
      // the roles keep each value read out under its header.
      put(cell, "role", cards ? (cell.tagName === "TH" ? "rowheader" : "cell") : null);
      if (hint === "secondary") folds = true;
      column += cell.colSpan;
    }
    put(row, "role", cards ? "row" : null);
    if (cards && folds) {
      if (!more) row.append(moreCell());
      else if (row.lastElementChild !== more) row.append(more);
    } else {
      more?.remove();
      put(row, "data-card-open", null);
    }
  }
}

/** How many columns a full-width row spans at the current width, for a page's
 *  own rows in a table's body, as the table's busy and empty notes use it.
 *  Below 1024px a column hinted `hidden` is gone, so a row spanning all of
 *  `columns` would add empty ones beside the header row; this counts only
 *  those left, and at 1024px and up all of them. Pass the same `hints` as the
 *  table. A row that spans only some of the columns subtracts the cells it
 *  draws itself from the result, `useColumnSpan(n, hints) - 2`, and keeps what
 *  is left at 1 or more, as the hidden columns may have taken those cells'. */
export function useColumnSpan(columns: number, hints?: readonly (ColumnHint | undefined)[]): number {
  const narrow = useMediaQuery(TABLET);
  if (!narrow) return columns;
  let shown = 0;
  for (let i = 0; i < columns; i++) if (hints?.[i] !== "hidden") shown++;
  return Math.max(1, shown);
}

/** A listing in the stylesheet's `.table-wrapper` markup, owning the header
 *  row and the two rows every page used to write by hand: the busy one and
 *  the empty one. On a phone each row becomes a card of labelled lines. */
export function DataTable({ headers, count, empty, loading, hints, sort, phone = "cards", children }: Props) {
  const isPhone = useMediaQuery(PHONE);
  const cards = isPhone && phone === "cards";
  const role = (name: string) => (cards ? name : undefined);
  const span = useColumnSpan(headers.length, hints);
  const sortId = useId();
  const table = useRef<HTMLTableElement>(null);
  const layout = useRef<Layout | null>(null);
  const watcher = useRef<MutationObserver | null>(null);

  // Every render: the headers, the hints or the width may have changed.
  useLayoutEffect(() => {
    const el = table.current;
    if (!el) return;
    const labels = Array.from(el.tHead?.rows[0]?.cells ?? [], (th) => (th.textContent ?? "").replace(/\s+/g, " ").trim());
    layout.current = { labels, hints: hints ?? [], cards };
    stamp(el, layout.current);
    // What the stamp itself changed needs no second pass.
    watcher.current?.takeRecords();
  });

  // Rows that change without the table rendering: a row component's own
  // update, or one that adds a cell.
  useLayoutEffect(() => {
    const el = table.current;
    const body = el?.tBodies[0];
    if (!el || !body || typeof MutationObserver !== "function") return;
    const watch = new MutationObserver((records) => {
      const rows = records.some(
        (r) =>
          r.target === body ||
          r.target.parentNode === body ||
          (r.type === "attributes" && r.target.parentNode?.parentNode === body),
      );
      if (rows && layout.current) stamp(el, layout.current);
      watch.takeRecords();
    });
    watch.observe(body, {
      childList: true,
      subtree: true,
      attributes: true,
      attributeFilter: ["class", "colspan", "data-label"],
    });
    watcher.current = watch;
    return () => {
      watch.disconnect();
      watcher.current = null;
    };
  }, []);

  return (
    <>
      {isPhone && sort && (
        <div className="table-sort">
          <label htmlFor={sortId}>{sort.label ?? "Sort by"}</label>
          <select id={sortId} className="input" value={sort.value} onChange={(e) => sort.onChange(e.target.value)}>
            {sort.options.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </div>
      )}
      <div className="table-wrapper" data-phone={phone}>
        <table ref={table} role={role("table")}>
          <thead role={role("rowgroup")}>
            <tr role={role("row")}>
              {headers.map((h, i) => (
                <th key={i} role={role("columnheader")} data-hint={hints?.[i]}>
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody role={role("rowgroup")}>
            {loading ? (
              <tr>
                <td colSpan={span} className="table-note">Loading…</td>
              </tr>
            ) : count === 0 && empty != null ? (
              <tr>
                <td colSpan={span} className="table-note">{empty}</td>
              </tr>
            ) : (
              children
            )}
          </tbody>
        </table>
      </div>
    </>
  );
}
