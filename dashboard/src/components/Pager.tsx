import type { ReactNode } from "react";

import { useMediaQuery } from "../hooks/useMediaQuery";
import { PHONE } from "../styles/breakpoints";
import "./dataTable.css";

interface Props {
  /** Zero-based. */
  page: number;
  totalPages: number;
  onPage: (page: number) => void;
  /** Appended in brackets, such as how many rows the pages hold. */
  note?: ReactNode;
}

/** The page to show for a list that may have shrunk under the pager: one left
 *  past the end snaps back to the last, rather than showing nothing beside a
 *  note that still quotes the count. */
export function clampPage(page: number, totalPages: number): number {
  return Math.min(page, Math.max(0, totalPages - 1));
}

/** Prev and Next around "Page X of Y". Renders nothing when one page holds
 *  everything, so a caller need not guard it. */
export function Pager({ page, totalPages, onPage, note }: Props) {
  const isPhone = useMediaQuery(PHONE);
  if (totalPages <= 1) return null;
  return (
    <div className="pager">
      <button className="btn btn-secondary btn-sm" disabled={page === 0} onClick={() => onPage(page - 1)}>
        ← Prev
      </button>
      <span className="pager-note">
        {isPhone ? (
          // Two pieces, so a phone can move the note under the page whole.
          <>
            <span className="pager-page">
              Page {page + 1} of {totalPages}
            </span>
            {note != null && (
              <>
                {" "}
                <span className="pager-count">({note})</span>
              </>
            )}
          </>
        ) : (
          <>
            Page {page + 1} of {totalPages}
            {note != null && <> ({note})</>}
          </>
        )}
      </span>
      <button
        className="btn btn-secondary btn-sm"
        disabled={page >= totalPages - 1}
        onClick={() => onPage(page + 1)}
      >
        Next →
      </button>
    </div>
  );
}
