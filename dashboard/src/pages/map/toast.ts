/**
 * Tiny toast system — no library, no provider. A singleton mounts itself
 * lazily into `document.body` the first time `toast()` is called and
 * renders a stack of dismissible chips at the bottom-right.
 *
 * Usage: `toast("Copied")`, `toast("Failed", { tone: "error" })`.
 */

import { writeClipboard } from "../../utils/clipboard";

type Tone = "info" | "success" | "error" | "warn";

interface ToastEntry {
  id: number;
  text: string;
  tone: Tone;
}

let _container: HTMLDivElement | null = null;
let _entries: ToastEntry[] = [];
let _idSeq = 1;

// Toasts sit on document.body, under `:root`, so the console's tokens resolve
// here and each toast takes the console's theme.
const TONES: { bg: Record<Tone, string>; border: Record<Tone, string>; ink: string } = {
  bg: {
    info: "var(--bg-card)",
    success: "var(--success-light)",
    error: "var(--error-light)",
    warn: "var(--warning-light)",
  },
  border: { info: "var(--border-light)", success: "var(--success)", error: "var(--error)", warn: "var(--warning)" },
  ink: "var(--text-primary)",
};

function ensureContainer(): HTMLDivElement {
  if (_container && document.body.contains(_container)) return _container;
  const c = document.createElement("div");
  c.setAttribute("data-tf-toast", "");
  Object.assign(c.style, {
    position: "fixed",
    bottom: "16px",
    right: "16px",
    zIndex: "2000",
    display: "flex",
    flexDirection: "column",
    gap: "8px",
    pointerEvents: "none",
    fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif",
  } as CSSStyleDeclaration);
  document.body.appendChild(c);
  _container = c;
  return c;
}

function render() {
  const c = ensureContainer();
  c.innerHTML = "";
  for (const e of _entries) {
    const el = document.createElement("div");
    el.textContent = e.text;
    Object.assign(el.style, {
      background: TONES.bg[e.tone],
      border: `1px solid ${TONES.border[e.tone]}`,
      color: TONES.ink,
      padding: "8px 12px",
      borderRadius: "8px",
      fontSize: "13px",
      boxShadow: "0 4px 12px rgba(15,23,42,0.35)",
      pointerEvents: "auto",
      maxWidth: "320px",
    } as CSSStyleDeclaration);
    c.appendChild(el);
  }
}

export function toast(text: string, opts: { tone?: Tone; durationMs?: number } = {}) {
  if (typeof window === "undefined") return;
  const id = _idSeq++;
  const entry: ToastEntry = { id, text, tone: opts.tone ?? "info" };
  _entries.push(entry);
  render();
  const duration = opts.durationMs ?? 2500;
  window.setTimeout(() => {
    _entries = _entries.filter((e) => e.id !== id);
    render();
  }, duration);
}

/** Copy a string to the clipboard, with a built-in success/failure toast. */
export async function copyToClipboard(text: string, label = "Copied"): Promise<boolean> {
  const ok = await writeClipboard(text);
  toast(ok ? label : "Copy failed", { tone: ok ? "success" : "error" });
  return ok;
}
