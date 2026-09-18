import { useEffect, useRef, useState } from "react";
import { useResolvedTheme, type Theme } from "../../context/ThemeContext";
import { SCALAR_THEME_CSS } from "../../utils/scalarTheme";

/** The whole schema, admin, account and test routes included, behind
 *  require_admin. The public subset is the reference at the api host's root. */
export const DOCUMENT_URL = "/api/admin/openapi.json";

// Served untouched from public/ and run as a plain script; see its NOTICE.md.
const SCALAR_BUNDLE = `${import.meta.env.BASE_URL}vendor/scalar-api-reference-1.69.0/standalone.js`;

let loading: Promise<void> | null = null;

// Added once and kept: the bundle defines window.Scalar and its stylesheet as it
// runs, and has no way to take either back. A failed load is retried next time.
function loadScalar(): Promise<void> {
  loading ??= new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = SCALAR_BUNDLE;
    script.onload = () => resolve();
    script.onerror = () => {
      loading = null;
      script.remove();
      reject(new Error("The reference viewer did not load"));
    };
    document.head.appendChild(script);
  });
  return loading;
}

function configuration(theme: Theme): object {
  return {
    url: DOCUMENT_URL,
    // The AI chat and "Generate MCP" upload the document to Scalar, and the
    // default fonts come from Scalar's CDN.
    agent: { disabled: true },
    mcp: { disabled: true },
    showDeveloperTools: "never",
    withDefaultFonts: false,
    telemetry: false,
    theme: "none",
    customCss: SCALAR_THEME_CSS,
    forceDarkModeState: theme,
    hideDarkModeToggle: true,
  };
}

export default function ApiDocsPage() {
  const mount = useRef<HTMLDivElement>(null);
  const theme = useResolvedTheme();
  const [error, setError] = useState<string | null>(null);

  // Re-created on a theme change: Scalar reads its colour mode once, at mount.
  useEffect(() => {
    let instance: ScalarInstance | undefined;
    let cancelled = false;
    loadScalar().then(
      () => {
        if (cancelled || !mount.current || !window.Scalar) return;
        setError(null);
        // Scalar hydrates rather than mounts into an element that has children,
        // and the instance before this one may have left some.
        mount.current.replaceChildren();
        instance = window.Scalar.createApiReference(mount.current, configuration(theme));
      },
      (e: Error) => {
        if (!cancelled) setError(e.message);
      },
    );
    return () => {
      cancelled = true;
      instance?.destroy();
    };
  }, [theme]);

  return (
    <>
      <div className="page-header">
        <h1>API Reference</h1>
        <p>
          Every route the server mounts, admin, account and test routes included. Test requests go to this host, so
          they run as you.
        </p>
      </div>
      {error && (
        <div className="empty-state" style={{ color: "var(--error)" }}>
          Error: {error}.
        </div>
      )}
      {/* Always rendered, so a retry after a failed load has somewhere to mount. */}
      <div ref={mount} />
    </>
  );
}
