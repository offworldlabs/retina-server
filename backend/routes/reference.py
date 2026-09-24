"""The API reference: a page at `/`, the public document at `/openapi.json`,
and the whole schema for an administrator.

The page is Scalar (github.com/scalar/scalar, MIT) rendering one document per
service, each fetched live from where it is generated. Only the api vhost sends
`/` to this app; every other vhost serves its SPA there. FastAPI's own
`/openapi.json`, `/docs` and `/redoc` are switched off in main.py, since they
publish every route the application mounts.

Above Scalar sits the console's header: the RETINA mark, a light, system and
dark switch in place of Scalar's own toggle, and a link back to HOST_APP.
"""

import base64
import hashlib
import html
import json
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from core.users import require_admin
from routes.openapi_documents import public_document
from services import mail

router = APIRouter(include_in_schema=False)

# Pinned by version and by hash: this script runs on the API's own origin.
SCALAR_URL = "https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.69.0/dist/browser/standalone.js"
SCALAR_INTEGRITY = "sha384-UL+pt9bcR3hCuzEybA1bAyu6yv9qkzJuYCP5N+HZPOo9ZkUXcMflxqBjC1vfDzfe"

# RETINA's palette (packages/shared/css/tokens.css) in Scalar's own variable
# names. The admin console's viewer carries the same stylesheet in
# dashboard/src/utils/scalarTheme.ts, and dashboard/src/test/scalarTheme.test.ts
# fails if the two copies differ or either leaves the tokens. theme:"none" leaves
# Scalar its plain base palette, which these override; layout, spacing and any
# colour not named here stay Scalar's.
THEME_CSS = """
.dark-mode {
  --scalar-background-1: #0d1b2a;
  --scalar-background-2: #132240;
  --scalar-background-3: #1a2b4d;
  --scalar-background-card: #132240;
  --scalar-background-accent: rgba(56, 189, 248, 0.16);
  --scalar-background-alert: rgba(251, 191, 36, 0.15);
  --scalar-background-danger: rgba(244, 63, 94, 0.15);
  --scalar-border-color: rgba(100, 180, 255, 0.14);
  --scalar-color-1: #e2e8f0;
  --scalar-color-2: #94a3b8;
  --scalar-color-3: #64748b;
  --scalar-color-accent: #38bdf8;
  --scalar-color-green: #4ade80;
  --scalar-color-red: #f43f5e;
  --scalar-color-orange: #fbbf24;
  --scalar-color-blue: #38bdf8;
  --scalar-link-color: #38bdf8;
  --scalar-link-color-hover: #7dd3fc;
  --scalar-button-1: #38bdf8;
  --scalar-button-1-color: #082f49;
  --scalar-button-1-hover: #7dd3fc;
  --scalar-header-background-1: #0d1b2a;
  --scalar-header-background-2: #132240;
  --scalar-header-color-1: #e2e8f0;
  --scalar-header-color-2: #94a3b8;
  --scalar-header-border-color: rgba(100, 180, 255, 0.14);
  --scalar-header-call-to-action-color: #38bdf8;
  --scalar-sidebar-background-1: #132240;
  --scalar-sidebar-border-color: rgba(100, 180, 255, 0.14);
  --scalar-sidebar-color-1: #e2e8f0;
  --scalar-sidebar-color-2: #94a3b8;
  --scalar-sidebar-color-active: #38bdf8;
  --scalar-sidebar-item-hover-background: rgba(56, 189, 248, 0.07);
  --scalar-sidebar-item-hover-color: #e2e8f0;
  --scalar-sidebar-item-active-background: rgba(56, 189, 248, 0.16);
  --scalar-sidebar-search-background: rgba(15, 30, 55, 0.9);
  --scalar-sidebar-search-border-color: rgba(100, 180, 255, 0.14);
  --scalar-sidebar-search-color: #94a3b8;
}
.light-mode {
  --scalar-background-1: #f1f5f9;
  --scalar-background-2: #ffffff;
  --scalar-background-3: #f8fafc;
  --scalar-background-card: #ffffff;
  --scalar-background-accent: rgba(59, 130, 246, 0.10);
  --scalar-background-alert: rgba(245, 158, 11, 0.10);
  --scalar-background-danger: rgba(239, 68, 68, 0.10);
  --scalar-border-color: #e2e8f0;
  --scalar-color-1: #0f172a;
  --scalar-color-2: #475569;
  --scalar-color-3: #94a3b8;
  --scalar-color-accent: #3b82f6;
  --scalar-color-green: #10b981;
  --scalar-color-red: #ef4444;
  --scalar-color-orange: #f59e0b;
  --scalar-color-blue: #3b82f6;
  --scalar-link-color: #3b82f6;
  --scalar-link-color-hover: #2563eb;
  --scalar-button-1: #3b82f6;
  --scalar-button-1-color: #ffffff;
  --scalar-button-1-hover: #2563eb;
  --scalar-header-background-1: #f1f5f9;
  --scalar-header-background-2: #ffffff;
  --scalar-header-color-1: #0f172a;
  --scalar-header-color-2: #475569;
  --scalar-header-border-color: #e2e8f0;
  --scalar-header-call-to-action-color: #3b82f6;
  --scalar-sidebar-background-1: #ffffff;
  --scalar-sidebar-border-color: #e2e8f0;
  --scalar-sidebar-color-1: #0f172a;
  --scalar-sidebar-color-2: #475569;
  --scalar-sidebar-color-active: #3b82f6;
  --scalar-sidebar-item-hover-background: rgba(59, 130, 246, 0.05);
  --scalar-sidebar-item-hover-color: #0f172a;
  --scalar-sidebar-item-active-background: rgba(59, 130, 246, 0.10);
  --scalar-sidebar-search-background: #f8fafc;
  --scalar-sidebar-search-border-color: #e2e8f0;
  --scalar-sidebar-search-color: #475569;
}
""".strip()

# The console's font stack and radii (dashboard/src/App.css, tokens.css), and
# room for the header. This page's alone: the admin viewer sits inside the
# console. The selectors name Scalar's own classes, which
# test_api_reference.py finds in the vendored bundle.
SHELL_CSS = r"""
body {
  margin: 0;
  background: var(--scalar-background-1);
}
:root {
  --scalar-custom-header-height: 56px;
  --scalar-font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
  --scalar-radius: 4px;
  --scalar-radius-lg: 8px;
  --scalar-radius-xl: 8px;
}
li.group\/sidebar-section > .group\/button .group\/button-label {
  text-transform: uppercase;
  font-size: 11px;
  letter-spacing: 0.05em;
  color: var(--scalar-color-3);
}
.markdown th:first-child,
.markdown td:first-child {
  white-space: nowrap;
}
""".strip()

# Scalar's AI chat and "Generate MCP" upload the document to Scalar, and its
# default fonts come from Scalar's own CDN; the CSP below would refuse all three.
_SHARED = {
    "agent": {"disabled": True},
    "mcp": {"disabled": True},
    "showDeveloperTools": "never",
    "withDefaultFonts": False,
    "telemetry": False,
    "theme": "none",
    "hideDarkModeToggle": True,
}

BLAH2_ARM_URL = "https://cdn.jsdelivr.net/gh/offworldlabs/blah2-arm@main/api/openapi.json"

DOCUMENTS = [
    {"title": "retina-server", "slug": "retina-server", "url": "/openapi.json"},
    {
        # Fetched through the api vhost (deploy/nginx), since tower-finder-service
        # sends no CORS header. The same absence refuses a test request from here.
        "title": "tower-finder-service",
        "slug": "tower-finder",
        "url": "/openapi/tower-finder.json",
        "servers": [{"url": "https://towers.retina.fm"}],
        "hideTestRequestButton": True,
    },
    {
        # blah2-api runs on each node, so there is no live instance to fetch
        # from: the document is the one committed in blah2-arm, read through
        # jsDelivr (CORS-open, public repo). Its server is on the node's LAN,
        # which this https page cannot reach, so no test request.
        "title": "blah2-arm (on each node)",
        "slug": "blah2-arm",
        "url": BLAH2_ARM_URL,
        "hideTestRequestButton": True,
    },
]

# The class on <body>, which every colour in THEME_CSS keys on, is this
# script's to set; Scalar reads its own mode only at mount. It runs before
# Scalar's bundle so the first paint is already in the chosen mode. The
# documents sit on a line of their own for the tests.
_INIT = f"""(function () {{
  var documents = {json.dumps([_SHARED | document for document in DOCUMENTS])};
  var dark = matchMedia("(prefers-color-scheme: dark)");
  var buttons = document.querySelectorAll(".retina-theme button");
  var mode;

  function apply() {{
    var chosen = null;
    try {{ chosen = localStorage.getItem("retina.theme"); }} catch (e) {{}}
    if (chosen !== "light" && chosen !== "dark") chosen = "system";
    mode = chosen === "system" ? (dark.matches ? "dark" : "light") : chosen;
    buttons.forEach(function (b) {{ b.setAttribute("aria-pressed", String(b.dataset.theme === chosen)); }});
    document.body.classList.toggle("dark-mode", mode === "dark");
    document.body.classList.toggle("light-mode", mode === "light");
  }}

  buttons.forEach(function (b) {{
    b.addEventListener("click", function () {{
      try {{ localStorage.setItem("retina.theme", b.dataset.theme); }} catch (e) {{}}
      apply();
    }});
  }});
  dark.addEventListener("change", apply);
  // Scalar sets the class too: light as its bundle loads, and on an OS change
  // the mode it was mounted in. This puts ours back before the frame paints.
  new MutationObserver(apply).observe(document.body, {{ attributes: true, attributeFilter: ["class"] }});
  apply();

  document.addEventListener("DOMContentLoaded", function () {{
    Scalar.createApiReference("#app", documents.map(function (d) {{
      return Object.assign({{ forceDarkModeState: mode }}, d);
    }}));
  }});
}})();"""

CSP = "; ".join(
    [
        "default-src 'none'",
        f"script-src {SCALAR_URL} 'sha256-{base64.b64encode(hashlib.sha256(_INIT.encode()).digest()).decode()}'",
        "style-src 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src data:",
        f"connect-src 'self' {BLAH2_ARM_URL}",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)

# The console's mark, as a data: URI the policy's img-src admits.
FAVICON = "data:image/svg+xml," + quote(
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
    '<rect width="32" height="32" rx="8" fill="#3b82f6"/>'
    '<text x="16" y="21.5" fill="#fff" font-family="-apple-system, Segoe UI, sans-serif" '
    'font-size="15" font-weight="700" text-anchor="middle">R</text></svg>'
)

# The console's header (dashboard/src/App.css) in Scalar's variables, which
# THEME_CSS defines for the mode on <body>.
_HEADER_CSS = """
.retina-header {
  position: sticky;
  top: 0;
  z-index: 100;
  box-sizing: border-box;
  height: 56px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 16px;
  background: var(--scalar-background-2);
  border-bottom: 1px solid var(--scalar-border-color);
  color: var(--scalar-color-1);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Oxygen, Ubuntu, Cantarell, sans-serif;
  -webkit-font-smoothing: antialiased;
}
.retina-mark {
  flex-shrink: 0;
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: var(--scalar-button-1);
  color: var(--scalar-button-1-color);
  font-size: 14px;
  font-weight: 700;
  text-decoration: none;
}
.retina-title {
  flex: 1;
  font-size: 16px;
  font-weight: 600;
  white-space: nowrap;
}
.retina-theme {
  display: flex;
  gap: 2px;
  padding: 2px;
  background: var(--scalar-background-1);
  border: 1px solid var(--scalar-border-color);
  border-radius: 4px;
}
.retina-theme button {
  display: flex;
  padding: 6px;
  background: none;
  border: none;
  border-radius: 3px;
  color: var(--scalar-color-2);
  cursor: pointer;
}
.retina-theme button svg {
  width: 15px;
  height: 15px;
}
.retina-theme button:hover {
  background: var(--scalar-background-3);
  color: var(--scalar-color-1);
}
.retina-theme button[aria-pressed="true"] {
  background: var(--scalar-background-accent);
  color: var(--scalar-color-accent);
}
.retina-open {
  padding: 6px 14px;
  border-radius: 6px;
  background: var(--scalar-button-1);
  color: var(--scalar-button-1-color);
  font-size: 13px;
  font-weight: 600;
  text-decoration: none;
  white-space: nowrap;
}
.retina-open:hover {
  background: var(--scalar-button-1-hover);
}
""".strip()

# The console's icons, in its order.
_THEMES = {
    "light": '<circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42'
    'M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>',
    "system": '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
    "dark": '<path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>',
}
_SWITCH = "".join(
    f'<button type="button" data-theme="{theme}" aria-pressed="false" title="{theme.title()}" '
    f'aria-label="{theme.title()}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{icon}</svg></button>'
    for theme, icon in _THEMES.items()
)


def _page() -> str:
    # HOST_APP is the environment's console, so staging's page links to staging's.
    app = mail.link_to("/")
    if app:
        href = html.escape(app)
        mark = f'<a class="retina-mark" href="{href}" aria-label="RETINA">R</a>'
        open_app = f'<a class="retina-open" href="{href}">Open app</a>'
    else:
        mark = '<span class="retina-mark" aria-hidden="true">R</span>'
        open_app = ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RETINA API reference</title>
<link rel="icon" href="{FAVICON}">
<style>
{THEME_CSS}
{SHELL_CSS}
{_HEADER_CSS}
</style>
</head>
<body>
<header class="retina-header">
{mark}
<span class="retina-title">API Reference</span>
<div class="retina-theme" role="group" aria-label="Appearance">{_SWITCH}</div>
{open_app}
</header>
<div id="app"></div>
<script>{_INIT}</script>
<script src="{SCALAR_URL}" integrity="{SCALAR_INTEGRITY}" crossorigin="anonymous"></script>
</body>
</html>
"""


def _public(app: FastAPI) -> dict[str, Any]:
    # Built once: the routes cannot change after startup.
    document = getattr(app.state, "public_openapi", None)
    if document is None:
        document = app.state.public_openapi = public_document(app.openapi(), app.description)
    return document


@router.get("/")
async def reference_page() -> HTMLResponse:
    return HTMLResponse(_page(), headers={"Content-Security-Policy": CSP})


@router.get("/openapi.json")
async def public_openapi(request: Request) -> JSONResponse:
    return JSONResponse(_public(request.app))


@router.get("/api/admin/openapi.json", dependencies=[Depends(require_admin)])
async def full_openapi(request: Request) -> JSONResponse:
    return JSONResponse(request.app.openapi())
