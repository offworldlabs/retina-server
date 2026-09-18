/**
 * What leaves the page: the manifest of URLs and the curl lines that fetch
 * them. Both are pasted into a terminal or a script, so every byte here is
 * part of the interface, and the URLs are absolute because the page's origin
 * does not travel with the text.
 */

import type { ArchiveFile } from "./keys";

/** Lines shown in the basket before the excerpt is cut short. */
export const CURL_EXCERPT_LINES = 3;

export function downloadUrl(file: ArchiveFile, origin: string = window.location.origin): string {
  return `${origin}/api/data/archive/${file.key}`;
}

/** The output is named explicitly. The route sets no Content-Disposition, so
 *  `-O` would write a JSON body as `part-000001.parquet` and `-J` has no
 *  header to read. */
export function curlLine(file: ArchiveFile, origin?: string): string {
  const stem = file.name.replace(/\.[^.]+$/, "");
  return `curl -sS -o ${file.node}-${stem}.json "${downloadUrl(file, origin)}"`;
}

export function manifest(files: ArchiveFile[], origin?: string): string {
  return files.map((f) => downloadUrl(f, origin)).join("\n");
}

export function curlScript(files: ArchiveFile[], origin?: string): string {
  return files.map((f) => curlLine(f, origin)).join("\n");
}

/** The first few curl lines, then a comment saying how many the copy takes:
 *  the bar shows the shape of the script, not all of it. */
export function curlExcerpt(files: ArchiveFile[], origin?: string): string {
  const lines = files.slice(0, CURL_EXCERPT_LINES).map((f) => curlLine(f, origin));
  const rest = files.length - lines.length;
  if (rest > 0) lines.push(`# … ${rest} more; Copy curl takes all ${files.length}`);
  return lines.join("\n");
}
