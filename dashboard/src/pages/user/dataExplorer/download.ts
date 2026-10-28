/**
 * What leaves the page: the manifest of URLs and the curl lines that fetch
 * them. Both are pasted into a terminal or a script, so every byte here is
 * part of the interface, and the URLs are absolute because the page's origin
 * does not travel with the text.
 *
 * A file from the owner's own archive answers only the owner's session, which
 * is a cookie the page can neither read nor print. Its curl line sends the
 * cookie from a shell variable, and the text holding one asks for the value
 * first, unechoed, so it is never written into anything that gets pasted,
 * saved or shared, the shell's own history included.
 */

import { type ArchiveSource, filePath } from "./archive";
import type { ArchiveFile } from "./keys";

/** Lines shown in the basket before the excerpt is cut short. */
export const CURL_EXCERPT_LINES = 3;

/** The session cookie the owner's routes read. */
export const SESSION_COOKIE = "auth_token";
/** The shell variable a curl line takes the cookie's value from. */
export const SESSION_VAR = "RETINA_SESSION";

/** Asks for the cookie's value: into a variable rather than onto a command
 *  line, so the shell's history never holds it, and unechoed. The prompt is
 *  printed apart because bash and zsh spell `read`'s own differently, and it
 *  says what to paste by itself: a `#` note above it would be run as a
 *  command by zsh as macOS ships it, with interactive comments off. */
export const SESSION_READ = `printf 'Paste your ${SESSION_COOKIE} cookie (it is not shown): ' >&2; read -rs ${SESSION_VAR}; echo >&2`;

/** The last line of such a text, so the value goes when the downloads are
 *  done rather than staying in the shell it was pasted into. */
export const SESSION_UNSET = `unset ${SESSION_VAR}`;

/** A file and the archive whose route serves it; the public one when unsaid. */
export type RoutedFile = ArchiveFile & { source?: ArchiveSource };

/** Whether fetching the file takes the owner's signed-in session. */
export const needsSession = (file: RoutedFile): boolean => file.source === "owner";

export function downloadUrl(file: RoutedFile, origin: string = window.location.origin): string {
  return `${origin}${filePath(file.key, file.source ?? "public")}`;
}

/** The output is named explicitly. The route sets no Content-Disposition, so
 *  `-O` would write a JSON body as `part-000001.parquet` and `-J` has no
 *  header to read. */
export function curlLine(file: RoutedFile, origin?: string): string {
  const stem = file.name.replace(/\.[^.]+$/, "");
  // Inside double quotes, so the shell expands the variable and the line
  // itself holds only its name.
  const session = needsSession(file) ? ` -b "${SESSION_COOKIE}=$${SESSION_VAR}"` : "";
  return `curl -sS${session} -o ${file.node}-${stem}.json "${downloadUrl(file, origin)}"`;
}

export function manifest(files: RoutedFile[], origin?: string): string {
  return files.map((f) => downloadUrl(f, origin)).join("\n");
}

/** The curl lines, with the prompt for the session before them and its unset
 *  after when one of them needs it. The whole is then in braces, so a shell
 *  handed it by a paste reads every line before it runs the first: `read`
 *  would otherwise take the next pasted line as the cookie's value. */
function withSession(lines: string[], files: RoutedFile[]): string {
  if (!files.some(needsSession)) return lines.join("\n");
  return ["{", SESSION_READ, ...lines, SESSION_UNSET, "}"].join("\n");
}

export function curlScript(files: RoutedFile[], origin?: string): string {
  return withSession(
    files.map((f) => curlLine(f, origin)),
    files,
  );
}

/** The first few curl lines, then a comment saying how many the copy takes:
 *  the bar shows the shape of the script, not all of it. The session's prompt
 *  and unset frame it as they frame the script. */
export function curlExcerpt(files: RoutedFile[], origin?: string): string {
  const lines = files.slice(0, CURL_EXCERPT_LINES).map((f) => curlLine(f, origin));
  const rest = files.length - lines.length;
  if (rest > 0) lines.push(`# … ${rest} more; Copy curl takes all ${files.length}`);
  return withSession(lines, files);
}
