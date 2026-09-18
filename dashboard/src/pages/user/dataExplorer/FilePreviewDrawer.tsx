import { useCallback, useEffect, useRef, useState } from "react";

import { request } from "@retina/shared";

import { DASH, fmt, formatBytes } from "../../../utils/format";
import { hhmm } from "./dates";
import { curlLine, downloadUrl } from "./download";
import { JSON_FACTOR, type ArchiveFile } from "./keys";
import type { RegistryNode } from "./nodes";
import { isoUTC, PREVIEW_ROWS, type PreviewSummary, summarisePreview } from "./preview";

/** The preview is the whole file as JSON, which runs to tens of megabytes:
 *  the shared client's ten seconds would cut most of them off. */
export const PREVIEW_TIMEOUT_MS = 300_000;

type Preview =
  | { status: "loading" }
  | { status: "done"; summary: PreviewSummary | null }
  | { status: "error"; error: string };

interface Props {
  /** The open file, or null for a closed drawer. */
  file: ArchiveFile | null;
  /** The registry's entry for the file's node; undefined when it has none,
   *  which the archive outlives. */
  node: RegistryNode | undefined;
  onClose: () => void;
  onAddToBasket: (key: string) => void;
}

const mhz = (hz: number | null) => (hz === null ? DASH : `${(hz / 1e6).toFixed(3)} MHz`);

function FetchBlock({ file }: { file: ArchiveFile }) {
  return (
    <>
      <div className="de-sec">Fetch</div>
      <pre className="de-codeblock">{curlLine(file)}</pre>
      <div className="de-hint">
        The download route answers <code className="mono">application/json</code> and sets no{" "}
        <code className="mono">Content-Disposition</code>, so it opens inline and{" "}
        <code className="mono">-O</code> or <code className="mono">-J</code> would misname it.
        Parquet, CSV and zip bundles are not served yet.
      </div>
    </>
  );
}

/** The mode, then the verdict: "signature valid" only when every signed frame
 *  verified, the count when some did not, nothing when none did. */
function signing({ signingMode, framesSigned, signaturesValid }: PreviewSummary) {
  if (!signingMode) return <span className="de-muted">not recorded</span>;
  if (signaturesValid === 0) return signingMode;
  if (signaturesValid === framesSigned) return `${signingMode} · signature valid`;
  return `${signingMode} · ${signaturesValid.toLocaleString()} of ${framesSigned.toLocaleString()} signatures valid`;
}

function Summary({ summary }: { summary: PreviewSummary }) {
  const { spanStartMs, spanEndMs, adsb } = summary;
  return (
    <>
      <dl className="de-kv">
        <dt>Reported node</dt>
        <dd>{summary.reportedNode ?? DASH}</dd>
        <dt>Frame span</dt>
        <dd>
          {spanStartMs !== null && spanEndMs !== null ? (
            <>
              {isoUTC(spanStartMs)} → {isoUTC(spanEndMs)}{" "}
              <span className="de-muted">
                ({((spanEndMs - spanStartMs) / 60_000).toFixed(1)} min, from the frames themselves)
              </span>
            </>
          ) : (
            <span className="de-muted">no frame carries a timestamp</span>
          )}
        </dd>
        <dt>Frames</dt>
        <dd>{summary.frames.toLocaleString()}</dd>
        <dt>Detections</dt>
        <dd>
          {summary.detections.toLocaleString()} ({(summary.detections / summary.frames).toFixed(2)}{" "}
          per frame)
        </dd>
        <dt>ADS-B match</dt>
        <dd>
          {adsb === null ? (
            <span className="de-muted">no adsb column in this file</span>
          ) : adsb.slots === 0 ? (
            <span className="de-muted">no detections to match</span>
          ) : (
            `${((100 * adsb.hits) / adsb.slots).toFixed(1)}% of ${adsb.slots.toLocaleString()} detections carry an ADS-B match`
          )}
        </dd>
        <dt>Signing</dt>
        <dd>{signing(summary)}</dd>
        <dt>rx (published)</dt>
        <dd>
          {fmt(summary.rx.lat, 4)}, {fmt(summary.rx.lon, 4)} · {fmt(summary.rx.altFt, 1)} ft
        </dd>
        <dt>tx</dt>
        <dd>
          {fmt(summary.tx.lat, 4)}, {fmt(summary.tx.lon, 4)} · {fmt(summary.tx.altFt, 1)} ft
        </dd>
        <dt>fc / fs</dt>
        <dd>
          {mhz(summary.fcHz)} / {mhz(summary.fsHz)}
        </dd>
      </dl>
      <div className="de-warnline">
        ⚠ <b>rx_lat / rx_lon are the published (fuzzed) receiver position</b>, not the true site.
        tx_* are true: transmitters are licensed towers.
      </div>
      <div className="de-sec">First detection of the first {PREVIEW_ROWS} frames</div>
      <div className="de-prev-wrap">
        <table className="de-prev">
          <thead>
            <tr>
              <th>timestamp</th>
              <th>delay (µs)</th>
              <th>doppler (Hz)</th>
              <th>snr (dB)</th>
            </tr>
          </thead>
          <tbody>
            {summary.rows.map((row, i) => (
              <tr key={i}>
                <td>{row.timestampMs === null ? DASH : isoUTC(row.timestampMs)}</td>
                {row.delay === null ? (
                  <td colSpan={3} className="de-muted">
                    no detections
                  </td>
                ) : (
                  <>
                    <td>{fmt(row.delay, 3)}</td>
                    <td>{fmt(row.doppler, 2)}</td>
                    <td>{fmt(row.snr, 2)}</td>
                  </>
                )}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  );
}

export function FilePreviewDrawer({ file, node, onClose, onAddToBasket }: Props) {
  // Kept across closes: a preview is a whole file's download, so reopening a
  // file must not fetch it again.
  const [previews, setPreviews] = useState<Map<string, Preview>>(new Map());
  const aborts = useRef<Set<AbortController>>(new Set());
  const closeRef = useRef<HTMLButtonElement>(null);

  // Read by load() without re-creating it, so a second click while a
  // download is in flight finds the claim and does not start another.
  const previewsRef = useRef(previews);
  previewsRef.current = previews;

  const load = useCallback(async (f: ArchiveFile) => {
    if (previewsRef.current.get(f.key)?.status === "loading") return;
    setPreviews((prev) => new Map(prev).set(f.key, { status: "loading" }));
    const controller = new AbortController();
    aborts.current.add(controller);
    try {
      const data = await request<unknown>(`/api/data/archive/${f.key}`, {
        signal: controller.signal,
        timeoutMs: PREVIEW_TIMEOUT_MS,
      });
      setPreviews((prev) =>
        new Map(prev).set(f.key, { status: "done", summary: summarisePreview(data) }),
      );
    } catch (e) {
      if (controller.signal.aborted) return;
      setPreviews((prev) =>
        new Map(prev).set(f.key, {
          status: "error",
          error: e instanceof Error ? e.message : String(e),
        }),
      );
    } finally {
      aborts.current.delete(controller);
    }
  }, []);

  useEffect(() => {
    const controllers = aborts.current;
    return () => {
      for (const c of controllers) c.abort();
      controllers.clear();
    };
  }, []);

  const open = file !== null;
  const key = file?.key;

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  useEffect(() => {
    if (key) closeRef.current?.focus();
  }, [key]);

  if (!file) return null;

  const preview = previews.get(file.key);
  const jsonSize = formatBytes(file.size * JSON_FACTOR);
  const placed = node && node.lat !== null && node.lon !== null;

  return (
    <>
      <div className="de-scrim" data-testid="de-scrim" onClick={onClose} />
      <aside
        className="de-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="de-drawer-title"
        data-testid="de-drawer"
      >
        <div className="de-drawer-head">
          <div>
            <h3 id="de-drawer-title">{file.name}</h3>
            <div className="mono de-muted de-drawer-key">{file.key}</div>
          </div>
          <button
            ref={closeRef}
            type="button"
            className="btn btn-secondary btn-sm"
            onClick={onClose}
          >
            Close
          </button>
        </div>

        <div className="de-drawer-body">
          <dl className="de-kv">
            <dt>Node</dt>
            <dd>
              {file.node}
              {!node && (
                <span className="de-muted"> (not in the node registry: offline or retired)</span>
              )}
            </dd>
            <dt>Day</dt>
            <dd>{file.day}</dd>
            <dt>Coverage</dt>
            <dd>
              {hhmm(file.startMs)} → {hhmm(file.endMs)} UTC{" "}
              <span className="de-muted">
                est.: the listing gives only the write time; files cover the hour ending there
              </span>
            </dd>
            <dt>Stored size</dt>
            <dd>
              {formatBytes(file.size)} Parquet · ≈ {jsonSize} as JSON (≈ {JSON_FACTOR}×)
            </dd>
            <dt>Published rx</dt>
            <dd>
              {placed ? (
                <>
                  {node.lat.toFixed(4)}, {node.lon.toFixed(4)}{" "}
                  <span className="de-muted">published, ±{node.uncertaintyKm ?? "?"} km</span>
                </>
              ) : (
                <span className="de-muted">no published position</span>
              )}
            </dd>
          </dl>

          {!preview && (
            <>
              <button type="button" className="btn btn-primary btn-sm" onClick={() => load(file)}>
                Load preview · ≈ {jsonSize}
              </button>
              <div className="de-hint">
                The preview downloads the whole file, since there is no range or head endpoint,
                so it is on demand rather than automatic.
              </div>
            </>
          )}
          {preview?.status === "loading" && (
            <div className="de-muted">Downloading ≈ {jsonSize}…</div>
          )}
          {preview?.status === "error" && (
            <>
              <div className="de-errline">Preview failed: {preview.error}</div>
              <button type="button" className="btn btn-secondary btn-sm" onClick={() => load(file)}>
                Retry
              </button>
            </>
          )}
          {preview?.status === "done" &&
            (preview.summary ? (
              <Summary summary={preview.summary} />
            ) : (
              <div className="de-errline">The file decoded to zero frames.</div>
            ))}

          <FetchBlock file={file} />
        </div>

        <div className="de-drawer-foot">
          <a className="btn btn-primary btn-sm" href={downloadUrl(file)} target="_blank" rel="noreferrer">
            Open JSON
          </a>
          <button
            type="button"
            className="btn btn-secondary btn-sm de-drawer-add"
            onClick={() => onAddToBasket(file.key)}
          >
            Add to basket
          </button>
        </div>
      </aside>
    </>
  );
}
