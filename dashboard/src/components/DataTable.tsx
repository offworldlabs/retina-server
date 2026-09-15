import type { ReactNode } from "react";

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
  /** The `<tr>` rows. */
  children?: ReactNode;
}

/** A listing in the stylesheet's `.table-wrapper` markup, owning the header
 *  row and the two rows every page used to write by hand: the busy one and
 *  the empty one. */
export function DataTable({ headers, count, empty, loading, children }: Props) {
  const span = headers.length;
  return (
    <div className="table-wrapper">
      <table>
        <thead>
          <tr>
            {headers.map((h, i) => (
              <th key={i}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
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
  );
}
