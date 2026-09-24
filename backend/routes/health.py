"""Server health endpoint.

Reports the whole server rather than any one feature, so it stands on its own
rather than beside the routes it happened to grow up with.
"""

import logging
import os

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from routes.sim_ingest import synthetic_fleet_enabled
from services import blah2_poller, polled_registration, probation
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

    The body also carries ``synthetic_fleet``, which is not health at all. It
    says whether this deployment runs a fleet to a caller with no credentials,
    which is how the staging smoke test asserts that staging runs none; the
    console learns the same from /api/auth/me. It is a deployment fact, not a
    secret: the compose file setting SYNTHETIC_FLEET_ENABLED is in this public
    repo.

    ``polled_radar_probation`` rides along on the same terms: it says whether
    this deployment holds unvetted polled radars back from the solve, the
    archive and the public map. On by default;
    POLLED_RADAR_PROBATION_ENABLED=0 switches it off. ``polled_radar_polling``
    says whether this deployment polls registered radars at all, which exactly
    one environment may, and ``polled_radar_registration`` whether it takes new
    ones.

    The strict body is left alone. A readiness probe's caller reads the status
    code and nothing else, and an uptime monitor parsing a 503 for a feature
    flag would be reading it at the one moment the server is least able to
    answer honestly.
    """
    issues = compute_health_issues()
    fleet = {
        "synthetic_fleet": synthetic_fleet_enabled(os.environ),
        "polled_radar_probation": probation.enabled(),
        "polled_radar_polling": blah2_poller.enabled(),
        "polled_radar_registration": polled_registration.enabled(),
    }
    if issues:
        # Details are logged (and alerted on by the monitor), never exposed on
        # this unauthenticated endpoint.
        logging.warning("Health check degraded: %s", ", ".join(i["type"] for i in issues))
        if strict:
            return JSONResponse({"status": "degraded"}, status_code=503)
        return {"status": "degraded", **fleet}
    return {"status": "ok", **fleet}
