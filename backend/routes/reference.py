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

# Scalar's AI chat and "Generate MCP" upload the document to Scalar, and its
# default fonts come from Scalar's own CDN; the CSP below would refuse all three.
_SHARED = {
    "agent": {"disabled": True},
    "mcp": {"disabled": True},
    "showDeveloperTools": "never",
    "withDefaultFonts": False,
    "telemetry": False,
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
