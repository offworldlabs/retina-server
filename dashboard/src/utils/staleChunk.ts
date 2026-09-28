/**
 * A tab opened before a deploy still names the old build's page chunks, which
 * the deploy removed, so the next page it opens fails to load. Reloading
 * fetches the new build, since the document itself is never cached.
 */

// The build's entry is a module script, whose hashed name changes whenever any chunk's does.
const moduleScripts = (doc: Document) =>
  [...doc.querySelectorAll('script[type="module"][src]')].map((s) => s.getAttribute("src"));

/** Listens for `vite:preloadError`, which Vite dispatches for a lazy import that fails. */
export async function reloadOnStaleChunk(): Promise<void> {
  // Only a newer build is worth a reload. On the live build already, offline or
  // mid-restart, one brings back the same failure or an error page in place of
  // a console that still works, so the page's error boundary shows instead.
  let live: (string | null)[];
  try {
    const res = await fetch(window.location.href, { cache: "no-store" });
    live = moduleScripts(new DOMParser().parseFromString(await res.text(), "text/html"));
  } catch {
    return;
  }
  // An error or sign-in page names none, and the tab may carry extra ones an
  // extension added, so only a live script the tab lacks marks a newer build.
  const ours = moduleScripts(document);
  if (live.some((src) => !ours.includes(src))) window.location.reload();
}
