"""Server health endpoint.

Reports the whole server rather than any one feature, so it stands on its own
rather than beside the routes it happened to grow up with.
"""

import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from services.health import compute_health_issues

router = APIRouter()


@router.get("/api/health")
async def health(strict: bool = Query(False)):
    """Report server health.

    Always 200 by default (liveness — used by the Docker healthcheck, which
    must not restart the container on transient degradation). Pass ``strict=1``
    for a readiness probe that returns 503 when degraded — use this for the
    external uptime monitor. Alerting is owned by the health-monitor task, not
    this endpoint, so health stays observable even when nothing polls it.
    """
    issues = compute_health_issues()
    if issues:
        # Details are logged (and alerted on by the monitor), never exposed on
        # this unauthenticated endpoint.
        logging.warning("Health check degraded: %s", ", ".join(i["type"] for i in issues))
        if strict:
            return JSONResponse({"status": "degraded"}, status_code=503)
        return {"status": "degraded"}
    return {"status": "ok"}
