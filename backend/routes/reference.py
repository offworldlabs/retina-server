"""The API reference: a page at `/`, the public document at `/openapi.json`,
and the whole schema for an administrator.

The page is Scalar (github.com/scalar/scalar, MIT) rendering one document per
service, each fetched live from where it is generated. Only the api vhost sends
`/` to this app; every other vhost serves its SPA there. FastAPI's own
`/openapi.json`, `/docs` and `/redoc` are switched off in main.py, since they
publish every route the application mounts.
"""

import base64
import hashlib
import json
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from core.users import require_admin
from routes.openapi_documents import public_document

router = APIRouter(include_in_schema=False)

# Pinned by version and by hash: this script runs on the API's own origin.
SCALAR_URL = "https://cdn.jsdelivr.net/npm/@scalar/api-reference@1.69.0/dist/browser/standalone.js"
SCALAR_INTEGRITY = "sha384-UL+pt9bcR3hCuzEybA1bAyu6yv9qkzJuYCP5N+HZPOo9ZkUXcMflxqBjC1vfDzfe"

# Values are transcribed from packages/shared/css/tokens.css (the dashboard and
# map's own dark/light palette, in turn RETINA's brand spec in claude-shared) into
# Scalar's own custom-property vocabulary; the two cannot share one stylesheet
# since their variable names differ. theme:"none" drops Scalar's bundled palette
# so these are the only source of colour — layout and spacing stay Scalar's own.
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

# Scalar's AI chat and "Generate MCP" upload the document to Scalar, and its
# default fonts come from Scalar's own CDN; the CSP below would refuse all three.
_SHARED = {
    "agent": {"disabled": True},
    "mcp": {"disabled": True},
    "showDeveloperTools": "never",
    "withDefaultFonts": False,
    "telemetry": False,
    "theme": "none",
    "customCss": THEME_CSS,
}

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
]

_INIT = f"Scalar.createApiReference('#app', {json.dumps([_SHARED | document for document in DOCUMENTS])});"

CSP = "; ".join(
    [
        "default-src 'none'",
        f"script-src {SCALAR_URL} 'sha256-{base64.b64encode(hashlib.sha256(_INIT.encode()).digest()).decode()}'",
        "style-src 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src data:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    ]
)

PAGE = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RETINA API reference</title>
</head>
<body>
<div id="app"></div>
<script src="{SCALAR_URL}" integrity="{SCALAR_INTEGRITY}" crossorigin="anonymous"></script>
<script>{_INIT}</script>
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
    return HTMLResponse(PAGE, headers={"Content-Security-Policy": CSP})


@router.get("/openapi.json")
async def public_openapi(request: Request) -> JSONResponse:
    return JSONResponse(_public(request.app))


@router.get("/api/admin/openapi.json", dependencies=[Depends(require_admin)])
async def full_openapi(request: Request) -> JSONResponse:
    return JSONResponse(request.app.openapi())
