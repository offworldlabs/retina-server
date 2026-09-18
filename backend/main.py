"""retina-server API — slim app factory.

All business logic lives in dedicated packages:
  core/       – shared mutable state
  services/   – TCP handler, frame processor, background tasks, storage
  clients/    – external API clients (FCC, Maprad, OpenSky)
  analytics/  – node trust, reputation, coverage, cross-node analysis
  pipeline/   – passive radar signal processing
  routes/     – FastAPI APIRouter modules
"""

import os
import sys

# Force the image-fresh constants.py to win over any stale copy that may live
# in the /app/backend/config named volume on existing servers. start.sh sets
# PYTHONPATH=/app/deploy/config-image, but uvicorn injects the working dir
# (/app/backend) at sys.path[0] AFTER PYTHONPATH is read, so the volume copy
# was being resolved first. Inserting from inside the running process is the
# only place we can guarantee priority — and the deploy keeps booting even
# when the cp-refresh in start.sh fails on a root-owned volume. Safe no-op
# outside the container, since the override directory only exists in the
# Docker image. See commit 19a305b for the original (insufficient) attempt.
_image_config_root = "/app/deploy/config-image"
if os.path.isdir(_image_config_root) and _image_config_root not in sys.path:
    sys.path.insert(0, _image_config_root)

import asyncio
import json
import logging
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from core import state
from core.env_parsing import parse_comma_list
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline
from routes.admin import router as admin_router
from routes.admin_infrastructure import router as admin_infrastructure_router
from routes.analytics import router as analytics_router
from routes.archive import router as archive_router
from routes.auth import router as auth_router
from routes.custody import router as custody_router
from routes.health import router as health_router
from routes.node_responses import API_DESCRIPTION
from routes.nodes import NODE_API_TAGS, NODE_BODY_LIMITS, install_error_handlers
from routes.nodes import router as nodes_router
from routes.output import router as output_router
from routes.radar import router as radar_router
from routes.reference import router as reference_router
from routes.sim_ingest import router as sim_ingest_router
from routes.sim_ingest import synthetic_fleet_enabled
from routes.stats import router as stats_router
from routes.streaming import router as streaming_router
from routes.test import router as test_router
from services import detection_mirror, publication
from services.alerting import log_destination
from services.runtime_coverage import start as _start_coverage
from services.runtime_coverage import stop as _stop_coverage
from services.state_snapshot import SAVE_INTERVAL_S, restore_snapshot, save_snapshot
from services.tasks import (
    adsb_truth_fetcher,
    aircraft_flush_task,
    analytics_refresh_task,
    archive_flush_task,
    archive_lifecycle_task,
    coverage_constraints_task,
    feed_gc_task,
    frame_processor_loop,
    health_monitor_task,
    heartbeat_task,
    prune_synthetic_nodes,
    reputation_evaluator,
    start_solver_workers,
    stop_solver_workers,
    storage_refresh_task,
    track_flush_task,
    users_backup_task,
)
from services.tasks.executor import task_executor, unfinished_task_executors
from services.tasks.solver import solver_workers_stopping
from services.tcp_handler import handle_tcp_client

load_dotenv()
logging.basicConfig(level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO))

log_destination()

TCP_PORT = int(os.getenv("RADAR_TCP_PORT", "3012"))

# ── Global pipeline (default geometry for file-loaded data) ───────────────────
_TAR1090_DATA_DIR = os.path.join(os.path.dirname(__file__), "tar1090_data")
os.makedirs(_TAR1090_DATA_DIR, exist_ok=True)

radar_pipeline = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)

# Write initial receiver.json
with open(os.path.join(_TAR1090_DATA_DIR, "receiver.json"), "w") as _f:
    json.dump(radar_pipeline.generate_receiver_json(), _f)

# Inject pipeline reference into route modules that need it
from routes import radar as _radar_mod  # noqa: E402
from routes import test as _test_mod

_radar_mod.init(radar_pipeline)
_test_mod.init(radar_pipeline)


# ── Lifespan: TCP server + background tasks ───────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    if solver_workers_stopping():
        raise RuntimeError("Previous solver workers are still stopping")
    if unfinished := unfinished_task_executors():
        raise RuntimeError(f"Previous background work is still running: {unfinished}")
    # The anonymous-admin bypass, said out loud once per boot.
    #
    # AUTH_ALLOW_ANONYMOUS_ADMIN=1 is a local-development convenience that no
    # deployed environment sets, so this line on a droplet means the bypass has
    # been restored to one — and it is not one guard among several, it is the
    # whole of the admin boundary
    # (core.users._derive_auth_flags, tests/test_auth.py).  The public surfaces
    # go to some length to publish displaced receiver positions and to withhold
    # private nodes entirely; the admin surfaces behind that boundary serve the
    # true ones, and while this is set they serve them to anyone who asks.
    #
    # A log line, not a refusal: local development has no Access assertion, so
    # refusing here would leave a laptop with no way into the console, and the
    # default is deliberately not this module's to change.
    # WARNING level so it survives the default
    # LOG_LEVEL and lands in the deploy's own logs rather than only in a
    # developer's terminal.
    from core.users import AUTH_BYPASS

    if AUTH_BYPASS:
        logging.warning(
            "AUTH_ALLOW_ANONYMOUS_ADMIN=1: every caller is an anonymous admin. The admin API "
            "serves TRUE node locations, which the public map is built to withhold — treat this "
            "deployment as publishing operator addresses to anyone who finds an admin route."
        )

    # Where sign-in mail is going, said out loud once per boot. Loud when it is
    # going to this log rather than to a mailbox.
    from services import mail

    mail.log_destination()

    # Start runtime coverage if COVERAGE_ENABLED=1
    _start_coverage()

    # Seed runtime-config overlay from source defaults / legacy volume copy.
    # Idempotent: only fills in files that don't already exist in data/runtime/.
    from core.runtime_config import migrate_defaults_into_runtime

    migrate_defaults_into_runtime()

    # No-op everywhere except tests (RETINA_SCHEMA_SOURCE guards create_all off
    # otherwise). The schema comes from Alembic migrations instead: deploy/start.sh
    # runs them before uvicorn starts, and `just setup` runs them for local dev.
    from core.users import create_db_and_tables

    await create_db_and_tables()

    # Migrate any legacy JSON stores (node_owners/claim_codes) to SQLite
    from core.auth import migrate_json_to_db

    await migrate_json_to_db()

    # Prime visibility after the schema and legacy data are ready. A cold
    # failure withholds public data while leaving the owner aircraft feed available.
    publication.invalidate()
    try:
        publication.private_node_ids()
    except publication.PublicationUnavailable:
        logging.exception("Publication policy unavailable at startup; public data withheld")

    # Load the v1 fleet into the in-process registries. All three start empty in
    # a fresh process and only registration writes to them, so without this a
    # deploy drops every node out of the pipeline and nothing puts it back: the
    # node client follows the spec and does not re-register unprompted. Must
    # precede the frame workers below, or the first frames after a restart
    # arrive for a node the associator has never seen.
    from services.node_pipeline import prime_pipeline_at_startup

    await prime_pipeline_at_startup()

    # Restore persisted state before accepting connections
    restored = restore_snapshot()

    from services.alerting import send_alert

    send_alert("server_start", "RETINA server started", {"restored": restored})

    connections: dict[asyncio.Task, asyncio.StreamWriter] = {}
    accepting = True

    def accept_client(reader, writer):
        if not accepting:
            writer.close()
            return
        task = asyncio.create_task(handle_tcp_client(reader, writer))
        connections[task] = writer

        def finished(completed):
            connections.pop(completed, None)
            writer.close()
            if not completed.cancelled() and (error := completed.exception()) is not None:
                logging.error("Radar TCP handler failed", exc_info=(type(error), error, error.__traceback__))

        task.add_done_callback(finished)

    server = await asyncio.start_server(accept_client, "0.0.0.0", TCP_PORT)
    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    logging.info("Radar TCP server listening on %s", addrs)
    async with server:
        tasks: list[asyncio.Task] = []
        try:
            # Start background daemon threads for multinode LM solving.
            # These drain solver_queue independently of frame workers.
            start_solver_workers()

            async def _snapshot_loop():
                """Save state snapshot periodically."""
                async with task_executor("state-snapshot") as run:
                    while True:
                        await asyncio.sleep(SAVE_INTERVAL_S)
                        try:
                            await run(save_snapshot)
                        except Exception:
                            logging.exception("State snapshot save failed")

            # Unset DETECTION_MIRROR_URL leaves this unarmed, and mirror_task then
            # returns at once, so the task list is the same shape in every
            # environment.
            detection_mirror.configure_from_env()

            for task_fn in (
                server.serve_forever,
                reputation_evaluator,
                prune_synthetic_nodes,
                adsb_truth_fetcher,
                feed_gc_task,
                archive_flush_task,
                track_flush_task,
                archive_lifecycle_task,
                users_backup_task,
                analytics_refresh_task,
                coverage_constraints_task,
                storage_refresh_task,
                detection_mirror.mirror_task,
                health_monitor_task,
                heartbeat_task,
                _snapshot_loop,
            ):
                tasks.append(asyncio.create_task(task_fn()))
            tasks.append(asyncio.create_task(aircraft_flush_task(radar_pipeline)))
            # One owned executor per shard preserves each node's frame ordering
            # while letting different shards process concurrently.
            for shard in range(state.frame_queue.shard_count):
                tasks.append(asyncio.create_task(frame_processor_loop(radar_pipeline, shard)))
            yield
        finally:
            # Stop ingress before cancelling consumers. Awaiting cancellation
            # also lets task-owned executors finish their in-flight work.
            accepting = False
            server.close()
            clients = list(connections.items())
            for task, writer in clients:
                writer.close()
                task.cancel()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, *(task for task, _ in clients), return_exceptions=True)
            await server.wait_closed()
            workers_stopped = await asyncio.to_thread(stop_solver_workers)
            unfinished = unfinished_task_executors()
            quiescent = workers_stopped and not unfinished
            if not quiescent:
                logging.error(
                    "Skipping final state/archive writes: solver stopped=%s, unfinished executors=%s",
                    workers_stopped,
                    unfinished,
                )

            if quiescent:
                # Persistence follows worker shutdown, including both a pending
                # failed track batch and records accumulated since that failure.
                try:
                    save_snapshot()
                except Exception:
                    logging.exception("Final state snapshot failed")
                from services.frame_processor import flush_all_archive_buffers
                from services.tasks.track_archive import flush_track_archive_buffer

                try:
                    flush_all_archive_buffers()
                except Exception:
                    logging.exception("Final detection archive flush failed")
                try:
                    while flush_track_archive_buffer() is not None:
                        pass
                except Exception:
                    logging.exception("Final track archive flush failed; batch remains pending")

            from clients import digitalocean
            from services.tasks.periodic import close_http_clients

            try:
                if "adsb-lol" not in unfinished:
                    await close_http_clients()
            except Exception:
                logging.exception("HTTP client shutdown failed")
            try:
                await digitalocean.aclose()
            except Exception:
                logging.exception("DigitalOcean client shutdown failed")
            try:
                if quiescent:
                    state.node_analytics.save_coverage_maps()
                    logging.info("Coverage maps saved to %s", state.COVERAGE_STORAGE_DIR)
            except Exception:
                logging.exception("Final coverage map save failed")
            finally:
                _stop_coverage()


# ── App factory ───────────────────────────────────────────────────────────────

_MAX_BODY_BYTES = int(os.getenv("MAX_REQUEST_BODY_BYTES", str(5 * 1024 * 1024)))  # 5 MB


class LimitUploadSize(BaseHTTPMiddleware):
    """Reject requests with Content-Length exceeding the limit for their path.

    `limits` maps a full request path to its own cap; any path not listed falls
    back to the global `_MAX_BODY_BYTES`. The node caps are far tighter than that
    default because registration is unauthenticated, so its cap is the only thing
    between an anonymous caller and the JSON parser.

    A listed path is also refused in the node API's error taxonomy,
    `{"error": "too_large"}`, which is what the node client parses. Everything
    else keeps the `{"detail": ...}` shape its callers already expect. The wire
    contract declares no 413 for any endpoint, so the code is chosen to match the
    taxonomy rather than transcribed from the spec.

    Only `Content-Length` is checked, so a chunked request carries no cap here.
    Cloudflare fronts every deployed environment and caps there too; this is the
    origin's own copy of that bound rather than the whole of it.
    """

    def __init__(self, app, limits: dict[str, int] | None = None):
        super().__init__(app)
        self._limits = limits or {}

    async def dispatch(self, request: Request, call_next):
        # scope["path"] rather than request.url.path, which rebuilds and reparses
        # a whole URL object on every request through the app. The trailing slash
        # is stripped because this runs ahead of routing, so it never sees the
        # path Starlette would canonicalise: without it, `/v1/nodes/register/`
        # misses the table and falls back to the 5 MB global cap.
        path_limit = self._limits.get(request.scope["path"].rstrip("/"))
        limit = _MAX_BODY_BYTES if path_limit is None else path_limit
        cl = request.headers.get("content-length")
        if cl and int(cl) > limit:
            body = (
                {"detail": f"Request body too large (max {limit} bytes)"}
                if path_limit is None
                else {"error": "too_large"}
            )
            return JSONResponse(status_code=413, content=body)
        return await call_next(request)


# The description is where the node API's cross-cutting policy lives — the error
# taxonomy, and the `x-retry` / `x-terminal` vocabulary the per-response
# annotations use. It is carried into the published node contract by
# scripts/generate_openapi.py, so there is one copy of it rather than a document
# beside the schema that can disagree with the routes. See routes/nodes.py.
app = FastAPI(
    title="retina-server API",
    description=API_DESCRIPTION,
    openapi_tags=NODE_API_TAGS,
    lifespan=lifespan,
    # The defaults would publish every route, admin and test included; the
    # public document and the whole one are served by routes/reference.py.
    openapi_url=None,
)


@app.exception_handler(publication.PublicationUnavailable)
async def publication_unavailable_handler(request: Request, exc: publication.PublicationUnavailable):
    return JSONResponse(
        status_code=503,
        content={"detail": "Publication policy is temporarily unavailable"},
        headers={"Retry-After": "5"},
    )


app.add_middleware(LimitUploadSize, limits=NODE_BODY_LIMITS)

app.add_middleware(
    CORSMiddleware,
    # parse_comma_list rather than a bare split: a trailing comma or a space
    # after a separator would otherwise yield an empty or padded origin, which
    # matches no real Origin header, so an origin the operator believed they
    # had allowed is refused with nothing logged.
    allow_origins=list(
        parse_comma_list(
            os.getenv(
                "CORS_ORIGINS",
                "http://localhost:5173,http://localhost:3000,http://localhost:5174,"
                # Production's names only. Every deployed environment sets
                # CORS_ORIGINS explicitly, so a staging or test name here would
                # only widen what a process falling back to this default trusts.
                "https://retina.fm,https://api.retina.fm,https://app.retina.fm,"
                "https://admin.retina.fm,https://towers.retina.fm",
            )
        )
    ),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "HEAD", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
    max_age=3600,
)

# ── Mount all routers ─────────────────────────────────────────────────────────
for router in (
    health_router,
    stats_router,
    radar_router,
    analytics_router,
    streaming_router,
    archive_router,
    test_router,
    custody_router,
    auth_router,
    admin_router,
    admin_infrastructure_router,
    output_router,
    nodes_router,
    reference_router,
):
    app.include_router(router)

# Scoped to /v1/nodes, and delegating to FastAPI's own handlers everywhere else:
# the node API answers in its own error taxonomy, and the rest of this API's
# callers parse the framework's shape. See routes/nodes.py.
install_error_handlers(app)

# Simulation ingest is a WRITE path (state.adsb_aircraft /
# ground_truth_trails) that only the synthetic fleet uses, so a deployment that
# runs no fleet should not carry it. The rule lives beside the router it gates;
# every compose overlay sets the flag today, and a fleetless deployment closes
# the path by dropping the line.
if synthetic_fleet_enabled(os.environ):
    app.include_router(sim_ingest_router)
else:
    logging.info("Simulation ingest endpoints not mounted (SYNTHETIC_FLEET_ENABLED is not 1)")
