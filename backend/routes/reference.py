"""The API reference: the public document at `/openapi.json`, and the whole
schema for an administrator.

FastAPI's own `/openapi.json`, `/docs` and `/redoc` are switched off in main.py,
since they publish every route the application mounts.
"""

from typing import Any

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from core.users import require_admin
from routes.openapi_documents import public_document

router = APIRouter(include_in_schema=False)


def _public(app: FastAPI) -> dict[str, Any]:
    # Built once: the routes cannot change after startup.
    document = getattr(app.state, "public_openapi", None)
    if document is None:
        document = app.state.public_openapi = public_document(app.openapi(), app.description)
    return document


@router.get("/openapi.json")
async def public_openapi(request: Request) -> JSONResponse:
    return JSONResponse(_public(request.app))


@router.get("/api/admin/openapi.json", dependencies=[Depends(require_admin)])
async def full_openapi(request: Request) -> JSONResponse:
    return JSONResponse(request.app.openapi())
