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

interface Props {
  /** The basket, resolved and in display order, each with the archive whose
   *  route fetches it for this viewer. */
  files: RoutedFile[];
  manifestOpen: boolean;
  onToggleManifest: () => void;
  onClear: () => void;
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

export function DownloadBasket({ files, manifestOpen, onToggleManifest, onClear }: Props) {
  const [copyOutcome, copy] = useCopy();
  const bytes = files.reduce((sum, f) => sum + f.size, 0);
  // Links from the owner's own archive, which answer only their session.
  const sessionLinks = files.filter(needsSession).length;

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
        {sessionLinks > 0 && (
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
        )}
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
          </div>
          <div>
            <div className="de-sec">Fetch</div>
            <pre className="de-codeblock" data-testid="de-curl-excerpt">
              {curlExcerpt(files) || "—"}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}
