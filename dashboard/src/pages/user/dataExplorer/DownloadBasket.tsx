import { formatBytes } from "../../../utils/format";
import { curlExcerpt, curlScript, manifest } from "./download";
import { JSON_FACTOR, type ArchiveFile } from "./keys";
import { useCopy } from "./useCopy";

interface Props {
  /** The basket, resolved and in display order. */
  files: ArchiveFile[];
  manifestOpen: boolean;
  onToggleManifest: () => void;
  onClear: () => void;
}

export function DownloadBasket({ files, manifestOpen, onToggleManifest, onClear }: Props) {
  const [copyOutcome, copy] = useCopy();
  const bytes = files.reduce((sum, f) => sum + f.size, 0);

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
