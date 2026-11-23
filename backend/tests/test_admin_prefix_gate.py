"""Every route under /api/admin wants an administrator.

The prefix is the admin API and nothing else. The leaderboard was the one route
under it that answered anyone, and it is GET /api/radar/leaderboard now
(tests/test_leaderboard_routes.py). Held here over the whole route table, so a
route added under the prefix is held to it by being added, wherever its router
lives.

The suite reaches admin routes through the anonymous-admin bypass, which would
admit every request, so the refusals below turn it off.
"""

import re

import pytest
from fastapi import APIRouter, Depends, FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from core.users import get_optional_user, require_admin
from main import app
from tests.owner_helpers import as_user, real_sessions, sign_in
from tests.route_helpers import as_served

PREFIX = "/api/admin"


def _under_prefix(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


def _requires(dependant, gate) -> bool:
    """Whether `gate` is anywhere in a route's dependency tree.

    A router's own dependencies, and those given to include_router, are in the
    route's list by the time it is mounted, and a dependency that itself depends
    on the gate is gated by it.
    """
    return any(dep.call is gate or _requires(dep, gate) for dep in dependant.dependencies)


def ungated(routes, gate=require_admin, mounted_at: str = "") -> list[str]:
    """The routes under the prefix that are reached without `gate`, as "METHODS path".

    A mounted application is walked into. A route under the prefix with no
    dependency tree, a plain Starlette one, counts as ungated: nothing here can
    see what guards it.
    """
    found = []
    for route in as_served(routes):
        path = mounted_at + getattr(route, "path", "")
        if isinstance(route, Mount):
            if _under_prefix(path) or PREFIX.startswith(path.rstrip("/") + "/"):
                found += ungated(route.routes, gate, path)
            continue
        if not _under_prefix(path):
            continue
        dependant = getattr(route, "dependant", None)
        if dependant is None or not _requires(dependant, gate):
            found.append(f"{','.join(sorted(getattr(route, 'methods', None) or ['WEBSOCKET']))} {path}")
    return found


def _admin_routes():
    return [route for route in as_served(app.routes) if _under_prefix(getattr(route, "path", ""))]


def test_every_route_under_the_prefix_wants_an_administrator():
    assert ungated(app.routes) == []


def test_the_walk_covers_the_admin_api():
    """Not vacuously empty: the walk sees every admin router the app mounts."""
    paths = {route.path for route in _admin_routes()}
    assert {
        "/api/admin/users",
        "/api/admin/metrics",
        "/api/admin/infrastructure",
        "/api/admin/polled-radars",
        "/api/admin/openapi.json",
    } <= paths
    assert len(paths) >= 20


def test_the_old_open_leaderboard_would_be_caught():
    """The route this file exists because of, put back beside the real table."""
    stray = APIRouter(prefix=PREFIX)

    @stray.get("/leaderboard")
    async def leaderboard(caller=Depends(get_optional_user)):
        return {}

    assert ungated([*app.routes, *stray.routes]) == ["GET /api/admin/leaderboard"]


def test_the_walk_tells_each_way_of_gating_from_none():
    async def console(user=Depends(require_admin)):
        return user

    def _ok():
        return {}

    direct = APIRouter(prefix=PREFIX)
    direct.add_api_route("/direct", _ok, dependencies=[Depends(require_admin)])
    direct.add_api_route("/nested", _ok, dependencies=[Depends(console)])
    direct.add_api_route("/open", _ok, dependencies=[Depends(get_optional_user)])
    direct.add_api_route("/bare", _ok)

    by_router = APIRouter(prefix=f"{PREFIX}/router", dependencies=[Depends(require_admin)])
    by_router.add_api_route("/read", _ok)

    by_include = APIRouter(prefix=f"{PREFIX}/included")
    by_include.add_api_route("/read", _ok)

    async def _socket(websocket: WebSocket):
        await websocket.close()

    sockets = APIRouter(prefix=PREFIX)
    sockets.add_api_websocket_route("/ws-gated", _socket, dependencies=[Depends(require_admin)])
    sockets.add_api_websocket_route("/ws-open", _socket)

    outside = APIRouter()
    outside.add_api_route("/api/radar/open", _ok)
    outside.add_api_route("/api/administrators", _ok)

    async def _plain(_request):
        return PlainTextResponse("")

    probe = FastAPI()
    for router in (direct, by_router, sockets, outside):
        probe.include_router(router)
    probe.include_router(by_include, dependencies=[Depends(require_admin)])
    probe.router.routes.append(Mount(f"{PREFIX}/static", app=Starlette(routes=[Route("/file", _plain)])))
    probe.router.routes.append(Mount("/api", app=Starlette(routes=[Route("/admin/sub", _plain)])))

    assert ungated(probe.routes) == [
        "GET /api/admin/open",
        "GET /api/admin/bare",
        "WEBSOCKET /api/admin/ws-open",
        "GET,HEAD /api/admin/static/file",
        "GET,HEAD /api/admin/sub",
    ]


# ── The gate answers as declared ─────────────────────────────────────────────

# One request per method and route. A placeholder fills each path parameter:
# the gate answers before the handler, or a body, is looked at.
_REQUESTS = sorted(
    (method, re.sub(r"\{[^}]+\}", "x", route.path))
    for route in _admin_routes()
    for method in getattr(route, "methods", None) or ()
    if method != "HEAD"
)


@pytest.fixture(scope="module")
def bare_client():
    """No lifespan: the gate answers before any handler reads what it starts."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def real(monkeypatch):
    real_sessions(monkeypatch)


@pytest.mark.parametrize(("method", "path"), _REQUESTS, ids=[f"{m} {p}" for m, p in _REQUESTS])
def test_a_caller_with_no_session_is_refused(bare_client, real, method, path):
    as_user(bare_client, None)
    assert bare_client.request(method, path).status_code == 401


@pytest.mark.parametrize(("method", "path"), _REQUESTS, ids=[f"{m} {p}" for m, p in _REQUESTS])
def test_a_signed_in_member_is_refused(bare_client, real, method, path):
    as_user(bare_client, sign_in(bare_client, "member@example.com"))
    try:
        r = bare_client.request(method, path)
    finally:
        as_user(bare_client, None)
    assert r.status_code == 403
    assert r.json() == {"detail": "Admin access required"}
