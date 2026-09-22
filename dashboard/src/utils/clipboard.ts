/**
 * Copies text and says whether it landed; how to report that is the caller's
 * business. The async API needs a secure context, so a plain-http host falls
 * back to copying from a hidden textarea, whose `false` is a refusal too.
 */
export async function writeClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    try {
      ta.select();
      return document.execCommand("copy");
    } finally {
      document.body.removeChild(ta);
    }
  } catch {
    return false;
  }
}
