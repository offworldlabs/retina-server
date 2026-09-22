import type { ReactNode } from "react";
import type { Polled } from "../hooks/usePolling";

interface NoticeProps {
  children: ReactNode;
  /** `error` when the notice is all the page has to show; a warning when the
   *  page carries on beneath it. */
  tone?: "warning" | "error";
  /** Offers a Retry button that calls this. */
  onRetry?: () => void;
}

/** A problem the page carries on through, set under its header: a failed
 *  refresh, a save that did not land. The markup is ui.css's `.notice`. */
export function Notice({ children, tone = "warning", onRetry }: NoticeProps) {
  return (
    <div className={tone === "error" ? "notice error" : "notice"} role="alert">
      <div className="notice-text">{children}</div>
      {onRetry && (
        <button type="button" className="btn btn-secondary btn-sm" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

type FetchState = Pick<Polled<unknown>, "error" | "updatedAt" | "refresh">;

/** True once a fetch has failed with nothing loaded before it: the page has
 *  no answer to show, and an empty one would be a false one. */
export function nothingLoaded({ error, updatedAt }: FetchState): boolean {
  return error !== null && updatedAt === null;
}

/** What a page says about its fetch failing, and nothing while it succeeds.
 *  With nothing loaded it stands in for the page's content; with an earlier
 *  answer still on screen, it says how old that answer is. `what` names the
 *  data in the page's own words: "alerts", "the node list". */
export function FetchNotice({ polled, what }: { polled: FetchState; what: string }) {
  const { error, updatedAt, refresh } = polled;
  if (!error) return null;
  if (!updatedAt) {
    return (
      <Notice tone="error" onRetry={refresh}>
        Could not load {what}: {error.message}
      </Notice>
    );
  }
  return (
    <Notice onRetry={refresh}>
      Could not refresh {what}: {error.message}. Showing what was loaded at{" "}
      {updatedAt.toLocaleTimeString()}.
    </Notice>
  );
}
