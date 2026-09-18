"""Server health endpoint.

Reports the whole server rather than any one feature, so it stands on its own
rather than beside the routes it happened to grow up with.
"""

import logging
import os

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from routes.sim_ingest import synthetic_fleet_enabled
from services.health import compute_health_issues

router = APIRouter(tags=["health"])


@router.get("/api/health")
async def health(strict: bool = Query(False)):
    """Report server health.

    Always 200 by default (liveness — used by the Docker healthcheck, which
    must not restart the container on transient degradation). Pass ``strict=1``
    for a readiness probe that returns 503 when degraded — use this for the
    external uptime monitor. Alerting is owned by the health-monitor task, not
    this endpoint, so health stays observable even when nothing polls it.

    The body also carries ``synthetic_fleet``, which is not health at all. The
    console's /sim surface exists only where the server runs a fleet, and a
    caller with a session learns that from /api/auth/me while a visitor without
    one has no such answer — so the signed-out nav would either advertise an
    empty map on production or hide a working one on test. This is the only
    thing the server tells everyone, so the flag rides along here rather than
    growing a second public endpoint that says one boolean. It is a deployment
    fact, not a secret: the compose file setting SYNTHETIC_FLEET_ENABLED is in
    this public repo, and the sim routes themselves are already unauthenticated
    reads wherever they are mounted.

    The strict body is left alone. A readiness probe's caller reads the status
    code and nothing else, and an uptime monitor parsing a 503 for a feature
    flag would be reading it at the one moment the server is least able to
    answer honestly.
    """
    issues = compute_health_issues()
    fleet = {"synthetic_fleet": synthetic_fleet_enabled(os.environ)}
    if issues:
        # Details are logged (and alerted on by the monitor), never exposed on
        # this unauthenticated endpoint.
        logging.warning("Health check degraded: %s", ", ".join(i["type"] for i in issues))
        if strict:
            return JSONResponse({"status": "degraded"}, status_code=503)
        return {"status": "degraded", **fleet}
    return {"status": "ok", **fleet}
