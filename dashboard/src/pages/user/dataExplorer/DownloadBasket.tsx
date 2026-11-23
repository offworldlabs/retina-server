import { useCallback, useLayoutEffect, useRef, useState, type RefObject } from "react";

import { Sheet } from "../../../components/Sheet";
import { useMediaQuery } from "../../../hooks/useMediaQuery";
import { PHONE } from "../../../styles/breakpoints";
import { formatBytes } from "../../../utils/format";
import {
  curlExcerpt,
  curlScript,
  manifest,
  needsSession,
  type RoutedFile,
  SESSION_COOKIE,
  SESSION_VAR,
} from "./download";
import { JSON_FACTOR } from "./keys";
import { useCopy } from "./useCopy";
import "./basket.phone.css";

interface Props {
  /** The basket, resolved and in display order, each with the archive whose
   *  route fetches it for this viewer. */
  files: RoutedFile[];
  manifestOpen: boolean;
  onToggleManifest: () => void;
  onClear: () => void;
  /** Where focus goes when a phone's basket empties while focus is in its bar
   *  or its sheet, which leave the page with it: by Clear, or by anything
   *  else that takes the last file out. */
  focusWhenEmptied?: RefObject<HTMLElement | null>;
}

/** A padlock, drawn in the text's own colour. */
function Lock() {
  return (
    <svg
      className="de-lock"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      <rect x="4" y="11" width="16" height="10" rx="2" />
      <path d="M8 11V7a4 4 0 0 1 8 0v4" />
    </svg>
  );
}

interface Contents {
  files: RoutedFile[];
  /** How many of them answer only the owner's session. */
  sessionLinks: number;
}

/** Said once the basket holds a link from the owner's own archive. */
function SessionNotice({ files, sessionLinks }: Contents) {
  return (
    <div className="de-basket-session" data-testid="de-basket-session">
      <Lock />
      <span>
        {sessionLinks === files.length
          ? sessionLinks === 1
            ? "Its link needs your signed-in session"
            : "These links need your signed-in session"
          : `${sessionLinks} of ${files.length} links need your signed-in session`}
      </span>
    </div>
  );
}

/** The download URLs, one per line, with what each answers and, for the
 *  owner's own links, how a script sends the session. */
function Manifest({ files, sessionLinks }: Contents) {
  return (
    <>
      <textarea
        readOnly
        value={manifest(files)}
        placeholder="Tick files above and their download URLs appear here."
        aria-label="Manifest of download URLs"
      />
      <div className="de-hint">
        A plain <b>.txt of URLs</b>. Each answers <code className="mono">application/json</code>:
        the archived Parquet rebuilt as the legacy per-frame JSON, roughly <b>{JSON_FACTOR}×</b>{" "}
        the stored size. There is no <code className="mono">Content-Disposition</code>, so name
        the output yourself (the curl block does).
      </div>
      {sessionLinks > 0 && (
        <div className="de-hint" data-testid="de-session-hint">
          <b>
            Links under <code className="mono">/api/auth/me/archive/</code> are your own archive
          </b>{" "}
          and answer only your signed-in session. They open in this browser as they are. A script
          has to send your <code className="mono">{SESSION_COOKIE}</code> cookie: the copied curl
          asks for its value first (<code className="mono">read -rs {SESSION_VAR}</code>) and
          forgets it once the downloads are done, so copy it from this site&apos;s cookies in your
          browser and paste it at the prompt, where it is neither shown nor kept in your shell
          history. Keep it out of anything you share: it signs in as you.
        </div>
      )}
    </>
  );
}

/** The first few curl lines of what Copy curl takes. */
function Excerpt({ files }: { files: RoutedFile[] }) {
  return (
    <>
      <div className="de-sec">Fetch</div>
      <pre className="de-codeblock" data-testid="de-curl-excerpt">
        {curlExcerpt(files) || "—"}
      </pre>
    </>
  );
}

interface CompactProps extends Contents {
  bytes: number;
  onClear: () => void;
  /** Called as it leaves the page with focus in its bar or its sheet. */
  onLeaveWithFocus: () => void;
}

/** A phone's basket: one slim row under the tree, the count and a button that
 *  opens the rest in a sheet. It is there only while the basket holds
 *  something, so emptying it, Clear included, takes the sheet away with the
 *  bar, and the next file ticked finds the sheet closed. */
function CompactBasket({ files, sessionLinks, bytes, onClear, onLeaveWithFocus }: CompactProps) {
  const [open, setOpen] = useState(false);
  const [copyOutcome, copy] = useCopy();
  const root = useRef<HTMLDivElement>(null);
  const count = `${files.length} ${files.length === 1 ? "file" : "files"}`;

  // Asked as it leaves, while focus is still where it was: the sheet then
  // hands focus back to Download, which leaves with it, and after that only
  // the page's body holds focus, with nothing to say where it had been.
  useLayoutEffect(() => {
    const node = root.current;
    return () => {
      if (node?.contains(document.activeElement)) onLeaveWithFocus();
    };
  }, [onLeaveWithFocus]);

  return (
    <div ref={root} className="de-basket de-basket-compact" data-testid="de-basket">
      <div className="de-basket-bar">
        <div className="de-basket-count" data-testid="de-basket-count">
          {sessionLinks > 0 && <Lock />}
          <span>
            <span className="de-basket-n">{files.length}</span> {files.length === 1 ? "file" : "files"} ·{" "}
            {formatBytes(bytes)}
          </span>
        </div>
        <button
          type="button"
          className="btn btn-primary de-basket-open"
          aria-haspopup="dialog"
          aria-expanded={open}
          onClick={(e) => {
            // The sheet hands focus back to whatever held it when it opened,
            // and Safari does not focus a tapped button: taken here, it comes
            // back to this button when the sheet closes.
            e.currentTarget.focus();
            setOpen(true);
          }}
        >
          Download
        </button>
      </div>

      <Sheet
        open={open}
        onClose={() => setOpen(false)}
        variant="bottom"
        className="de-basket-sheet"
        title="Download basket"
        description={`${count} selected · ${formatBytes(bytes)} (≈ ${formatBytes(bytes * JSON_FACTOR)} downloaded as JSON)`}
        actions={
          <>
            <button type="button" className="btn btn-primary" onClick={() => copy(curlScript(files))}>
              {copyOutcome ?? "Copy curl"}
            </button>
            <button type="button" className="btn btn-secondary" onClick={onClear}>
              Clear
            </button>
          </>
        }
      >
        {sessionLinks > 0 && <SessionNotice files={files} sessionLinks={sessionLinks} />}
        <Manifest files={files} sessionLinks={sessionLinks} />
        <Excerpt files={files} />
      </Sheet>
    </div>
  );
}

export function DownloadBasket({ files, manifestOpen, onToggleManifest, onClear, focusWhenEmptied }: Props) {
  const phone = useMediaQuery(PHONE);
  const [copyOutcome, copy] = useCopy();
  const bytes = files.reduce((sum, f) => sum + f.size, 0);
  // Links from the owner's own archive, which answer only their session.
  const sessionLinks = files.filter(needsSession).length;

  // A phone's basket that empties takes its bar and sheet with it, and with
  // them whatever had focus in either, which would fall to the page's body.
  // It goes to `focusWhenEmptied` instead, once they are gone. A basket that
  // left because the screen widened has the wider bar in its place, and focus
  // is left as that finds it.
  const leftWithFocus = useRef(false);
  const onLeaveWithFocus = useCallback(() => {
    leftWithFocus.current = true;
  }, []);
  const emptied = phone && files.length === 0;
  useLayoutEffect(() => {
    if (!leftWithFocus.current) return;
    leftWithFocus.current = false;
    if (emptied) focusWhenEmptied?.current?.focus();
  });

  // A phone has no room beside the tree for the bar's three buttons, let
  // alone the manifest, so its basket is a slim bar while something is ticked
  // and opens them in a sheet. The manifest's toggle belongs to the wider bar,
  // and its state is left for that.
  if (phone) {
    return files.length > 0 ? (
      <CompactBasket
        files={files}
        sessionLinks={sessionLinks}
        bytes={bytes}
        onClear={onClear}
        onLeaveWithFocus={onLeaveWithFocus}
      />
    ) : null;
  }

  return (
    <div className="de-basket" data-testid="de-basket">
      <div className="de-basket-bar">
        <div className="de-basket-count" data-testid="de-basket-count">
          <span className="de-basket-n">{files.length}</span> {files.length === 1 ? "file" : "files"}{" "}
          selected · {formatBytes(bytes)}
          {files.length > 0 && (
            <span className="de-muted"> (≈ {formatBytes(bytes * JSON_FACTOR)} downloaded as JSON)</span>
          )}
        </div>
        {sessionLinks > 0 && <SessionNotice files={files} sessionLinks={sessionLinks} />}
        <div className="de-basket-actions">
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            aria-expanded={manifestOpen}
            onClick={onToggleManifest}
          >
            {manifestOpen ? "Hide manifest" : "Show manifest"}
          </button>
          <button
            type="button"
            className="btn btn-secondary btn-sm"
            disabled={!files.length}
            onClick={() => copy(curlScript(files))}
          >
            {copyOutcome ?? "Copy curl"}
          </button>
          <button type="button" className="btn btn-secondary btn-sm" onClick={onClear}>
            Clear
          </button>
        </div>
      </div>

      {manifestOpen && (
        <div className="de-basket-body">
          <div>
            <Manifest files={files} sessionLinks={sessionLinks} />
          </div>
          <div>
            <Excerpt files={files} />
          </div>
        </div>
      )}
    </div>
  );
}
