"""The admin Infrastructure snapshot: the maths, the degradation rules, the cache, the route."""

import asyncio
import time

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from clients import digitalocean as do
from routes import admin_infrastructure as route
from services import infrastructure as infra


def _series(mode, values):
    return {"metric": {"host_id": "1", "mode": mode}, "values": [[t, str(v)] for t, v in values]}


def _snap(**overrides) -> dict:
    snap = {
        "configured": True,
        "reason": None,
        "fetched_at": 1,
        "tag": "retina",
        "stale": False,
        "checks": [],
        "droplets": [],
        "errors": [],
    }
    snap.update(overrides)
    return snap


def test_cpu_series_is_busy_fraction_of_counter_deltas():
    # Over each interval: idle +50 of a total +100, then idle +10 of +100.
    result = [
        _series("idle", [(0, 0), (60, 50), (120, 60)]),
        _series("user", [(0, 0), (60, 30), (120, 100)]),
        _series("system", [(0, 0), (60, 20), (120, 40)]),
    ]
    assert infra.cpu_utilisation_series(result) == [[60, 50.0], [120, 90.0]]


def test_cpu_series_without_idle_or_with_flat_counters_is_empty():
    assert infra.cpu_utilisation_series([_series("user", [(0, 0), (60, 5)])]) == []
    assert infra.cpu_utilisation_series([_series("idle", [(0, 5), (60, 5)]), _series("user", [(0, 1), (60, 1)])]) == []


def test_cpu_series_skips_an_interval_a_mode_has_no_sample_for():
    # user has no 120 sample, so 60-120 and 120-180 would each sum one mode over
    # a 120 s span and the other over 60 s. The intervals either side are intact.
    result = [
        _series("idle", [(0, 0), (60, 50), (120, 60), (180, 110), (240, 160)]),
        _series("user", [(0, 0), (60, 50), (180, 190), (240, 240)]),
    ]
    assert infra.cpu_utilisation_series(result) == [[60, 50.0], [240, 50.0]]


def test_used_fraction_aligns_on_timestamp_and_skips_gaps():
    part = [[0, "25"], [60, "50"], [120, "10"]]
    total = [[0, "100"], [120, "100"]]
    assert infra.used_fraction_series(part, total) == [[0, 75.0], [120, 90.0]]


def test_a_non_finite_sample_is_skipped_not_clamped():
    # NaN clamps to 0% through min/max and serialises as invalid JSON; drop the point instead.
    cpu = [
        _series("idle", [(0, 0), (60, 50), (120, "nan"), (180, 150)]),
        _series("user", [(0, 0), (60, 50), (120, 100), (180, 150)]),
    ]
    assert infra.cpu_utilisation_series(cpu) == [[60, 50.0]]
    assert infra.used_fraction_series([[0, "25"], [60, "nan"]], [[0, "100"], [60, "100"]]) == [[0, 75.0]]


def test_summarise_check_status_and_worst_uptime():
    check = {"id": "c1", "name": "retina-server prod", "target": "https://api.retina.fm/api/health", "enabled": True}
    state = {
        "regions": {
            "us_east": {
                "status": "UP",
                "status_changed_at": "2026-09-01T00:00:00Z",
                "thirty_day_uptime_percentage": 99.9,
            },
            "eu_west": {
                "status": "DOWN",
                "status_changed_at": "2026-09-15T10:00:00Z",
                "thirty_day_uptime_percentage": 98.5,
            },
        },
        "previous_outage": {"region": "eu_west", "started_at": "a", "ended_at": "b", "duration_seconds": 120},
    }
    out = infra.summarise_check(check, state)
    assert out["status"] == "MIXED"
    assert out["uptime_30d"] == 98.5
    assert out["regions"]["eu_west"]["since"] == "2026-09-15T10:00:00Z"
    assert out["last_outage"]["duration_seconds"] == 120

    all_up = {"regions": {r: {"status": "UP", "thirty_day_uptime_percentage": 100.0} for r in ("us_east", "eu_west")}}
    assert infra.summarise_check(check, all_up)["status"] == "UP"
    assert infra.summarise_check(check, {})["status"] == "UNKNOWN"


async def test_build_snapshot_reports_unconfigured_without_token(monkeypatch):
    monkeypatch.delenv(do.TOKEN_ENV, raising=False)
    snap = await infra.build_snapshot(now=1000)
    assert snap["configured"] is False
    assert do.TOKEN_ENV in snap["reason"]
    assert snap["stale"] is False
    assert snap["checks"] == [] and snap["droplets"] == []


def _patch_metrics(monkeypatch, metric):
    async def droplet_metric(name, host_id, start, end):
        return metric(name, host_id, start, end)

    monkeypatch.setattr(do, "droplet_metric", droplet_metric)


def _patch_list(monkeypatch, name, value):
    async def call(*args):
        return value

    monkeypatch.setattr(do, name, call)


async def test_build_snapshot_survives_one_metric_failing(monkeypatch):
    monkeypatch.setenv(do.TOKEN_ENV, "t")
    _patch_list(monkeypatch, "list_checks", [{"id": "c1", "name": "n", "target": "u", "enabled": True}])
    _patch_list(
        monkeypatch, "check_state", {"regions": {"us_east": {"status": "UP", "thirty_day_uptime_percentage": 100.0}}}
    )
    _patch_list(
        monkeypatch,
        "list_droplets",
        [{"id": 7, "name": "retina-prod", "vcpus": 4, "memory": 8192, "disk": 160, "status": "active"}],
    )

    def metric(name, host_id, start, end):
        if name == "cpu":
            raise RuntimeError("boom")
        if name in ("memory_available", "filesystem_free"):
            return [{"metric": {"mountpoint": "/"}, "values": [[end, "25"]]}]
        return [{"metric": {"mountpoint": "/"}, "values": [[end, "100"]]}]

    _patch_metrics(monkeypatch, metric)
    snap = await infra.build_snapshot(now=1000)
    assert snap["configured"] is True
    assert snap["checks"][0]["status"] == "UP"
    droplet = snap["droplets"][0]
    assert droplet["cpu_pct"] is None
    assert droplet["memory_pct"] == 75.0
    assert droplet["disk_pct"] == 75.0
    assert snap["errors"] == ["retina-prod: cpu unavailable (RuntimeError)"]


async def test_a_malformed_sample_costs_its_own_metric_only(monkeypatch):
    monkeypatch.setenv(do.TOKEN_ENV, "t")
    _patch_list(monkeypatch, "list_checks", [])
    _patch_list(
        monkeypatch,
        "list_droplets",
        [{"id": 1, "name": "retina-staging", "vcpus": 2, "memory": 4096, "disk": 80, "status": "active"}],
    )

    def metric(name, host_id, start, end):
        if name == "cpu":
            return [_series("idle", [(start, 0), (end, "bad")])]
        if name in ("memory_available", "filesystem_free"):
            return [{"metric": {"mountpoint": "/"}, "values": [[end, "25"]]}]
        return [{"metric": {"mountpoint": "/"}, "values": [[end, "100"]]}]

    _patch_metrics(monkeypatch, metric)
    snap = await infra.build_snapshot(now=1000)
    droplet = snap["droplets"][0]
    assert droplet["cpu_pct"] is None
    assert droplet["memory_pct"] == 75.0
    assert droplet["disk_pct"] == 75.0
    assert snap["errors"] == ["retina-staging: cpu unavailable (ValueError)"]


async def test_build_snapshot_survives_one_droplet_failing(monkeypatch):
    monkeypatch.setenv(do.TOKEN_ENV, "t")
    _patch_list(monkeypatch, "list_checks", [])
    _patch_list(
        monkeypatch,
        "list_droplets",
        [
            {"name": "retina-staging", "vcpus": 2, "memory": 4096, "disk": 80, "status": "active"},
            {"id": 2, "name": "retina-prod", "vcpus": 4, "memory": 8192, "disk": 160, "status": "active"},
        ],
    )

    def metric(name, host_id, start, end):
        if name == "cpu":
            return [_series("idle", [(start, 0), (end, 40)]), _series("user", [(start, 0), (end, 100)])]
        if name in ("memory_available", "filesystem_free"):
            return [{"metric": {"mountpoint": "/"}, "values": [[end, "25"]]}]
        return [{"metric": {"mountpoint": "/"}, "values": [[end, "100"]]}]

    _patch_metrics(monkeypatch, metric)
    snap = await infra.build_snapshot(now=1000)
    assert snap["configured"] is True
    assert [d["id"] for d in snap["droplets"]] == [2]
    droplet = snap["droplets"][0]
    assert droplet["cpu_pct"] == 71.4
    assert droplet["memory_pct"] == 75.0
    assert droplet["disk_pct"] == 75.0
    assert snap["errors"] == ["droplet retina-staging: unavailable (KeyError)"]


async def test_build_snapshot_survives_one_check_failing(monkeypatch):
    monkeypatch.setenv(do.TOKEN_ENV, "t")
    _patch_list(
        monkeypatch,
        "list_checks",
        [
            {"id": "c1", "name": "prod", "target": "u1", "enabled": True},
            {"id": "c2", "name": "staging", "target": "u2", "enabled": True},
        ],
    )

    async def state(check_id):
        if check_id == "c1":
            raise RuntimeError("boom")
        return {"regions": {"us_east": {"status": "UP", "thirty_day_uptime_percentage": 100.0}}}

    monkeypatch.setattr(do, "check_state", state)
    _patch_list(monkeypatch, "list_droplets", [])
    snap = await infra.build_snapshot(now=1000)
    assert [c["name"] for c in snap["checks"]] == ["staging"]
    assert snap["errors"] == ["check prod: state unavailable (RuntimeError)"]


async def test_an_http_status_is_named_in_the_error(monkeypatch):
    monkeypatch.setenv(do.TOKEN_ENV, "t")
    _patch_list(monkeypatch, "list_checks", [{"id": "c1", "name": "prod", "target": "u", "enabled": True}])
    request = httpx.Request("GET", "https://api.digitalocean.com/v2/uptime/checks/c1/state")
    forbidden = httpx.HTTPStatusError("forbidden", request=request, response=httpx.Response(403, request=request))

    async def state(check_id):
        raise forbidden

    monkeypatch.setattr(do, "check_state", state)
    _patch_list(monkeypatch, "list_droplets", [])
    snap = await infra.build_snapshot(now=1000)
    assert snap["checks"] == []
    assert snap["errors"] == ["check prod: state unavailable (HTTP 403)"]


async def test_a_droplet_with_no_metrics_is_named_rather_than_left_blank(monkeypatch):
    monkeypatch.setenv(do.TOKEN_ENV, "t")
    _patch_list(monkeypatch, "list_checks", [])
    _patch_list(
        monkeypatch,
        "list_droplets",
        [{"id": 7, "name": "retina-prod", "vcpus": 4, "memory": 8192, "disk": 160, "status": "off"}],
    )
    _patch_metrics(monkeypatch, lambda name, host_id, start, end: [])
    snap = await infra.build_snapshot(now=1000)
    droplet = snap["droplets"][0]
    assert (droplet["cpu_pct"], droplet["memory_pct"], droplet["disk_pct"]) == (None, None, None)
    # The three charts the page draws, not the five GETs behind them.
    assert snap["errors"] == [f"retina-prod: {metric} empty" for metric in ("cpu", "memory", "disk")]


async def test_check_and_droplet_pipelines_overlap(monkeypatch):
    # Checks cannot finish until droplets has started, so a build that runs the
    # two in sequence never returns and this fails on the deadline rather than
    # on a clock the CI runner shares.
    monkeypatch.setenv(do.TOKEN_ENV, "t")
    gate = asyncio.Event()

    async def gated_checks():
        await gate.wait()
        return []

    async def opening_droplets(tag):
        gate.set()
        return []

    monkeypatch.setattr(do, "list_checks", gated_checks)
    monkeypatch.setattr(do, "list_droplets", opening_droplets)
    snap = await asyncio.wait_for(infra.build_snapshot(now=1000), timeout=2)
    assert snap["configured"] is True
    assert snap["checks"] == [] and snap["droplets"] == [] and snap["errors"] == []


async def test_snapshot_is_cached_for_the_ttl(monkeypatch):
    monkeypatch.setenv(do.TOKEN_ENV, "t")
    calls = []

    async def fake_build(now=None):
        calls.append(now)
        return _snap(fetched_at=now)

    monkeypatch.setattr(infra, "build_snapshot", fake_build)
    first = await infra.snapshot()
    second = await infra.snapshot()
    assert first is second
    assert len(calls) == 1


async def test_concurrent_cold_callers_share_one_build(monkeypatch):
    monkeypatch.setenv(do.TOKEN_ENV, "t")
    calls = []

    async def fake_build(now=None):
        calls.append(now)
        await asyncio.sleep(0)  # let the second caller reach the lock mid-build
        return _snap(fetched_at=now)

    monkeypatch.setattr(infra, "build_snapshot", fake_build)
    first, second = await asyncio.gather(infra.snapshot(), infra.snapshot())
    assert first is second
    assert len(calls) == 1


async def test_unconfigured_snapshot_is_not_cached(monkeypatch):
    monkeypatch.delenv(do.TOKEN_ENV, raising=False)
    assert (await infra.snapshot())["configured"] is False
    monkeypatch.setenv(do.TOKEN_ENV, "t")

    async def fake_build(now=None):
        return _snap(fetched_at=now)

    monkeypatch.setattr(infra, "build_snapshot", fake_build)
    assert (await infra.snapshot())["configured"] is True


def test_route_returns_the_snapshot(monkeypatch):
    from main import app

    async def fake_snapshot():
        return _snap(configured=False, reason="x")

    monkeypatch.setattr(infra, "snapshot", fake_snapshot)
    with TestClient(app, raise_server_exceptions=False) as client:
        r = client.get("/api/admin/infrastructure")
    assert r.status_code == 200
    assert r.json()["configured"] is False


def _patch_slow_snapshot(monkeypatch):
    async def slow():
        await asyncio.sleep(5)

    monkeypatch.setattr(infra, "snapshot", slow)
    monkeypatch.setattr(route, "INFRASTRUCTURE_BUILD_TIMEOUT_S", 0.01)


async def test_route_serves_the_last_good_snapshot_when_a_build_overruns(monkeypatch):
    _patch_slow_snapshot(monkeypatch)
    monkeypatch.setattr(infra, "_cache", (time.time(), _snap(fetched_at=1000, tag="retina")))
    body = await route.admin_infrastructure(_user=None)
    assert body["stale"] is True
    assert body["fetched_at"] == 1000


async def test_route_answers_503_when_a_cold_build_overruns(monkeypatch):
    _patch_slow_snapshot(monkeypatch)
    monkeypatch.setattr(infra, "_cache", None)
    with pytest.raises(HTTPException) as raised:
        await route.admin_infrastructure(_user=None)
    assert raised.value.status_code == 503
    assert raised.value.detail == "infrastructure snapshot timed out"


def test_the_route_is_gated_on_require_admin():
    """The suite runs with AUTH_ALLOW_ANONYMOUS_ADMIN=1, so no request here can
    be refused. The gate is asserted where it is declared instead."""
    from core.users import require_admin
    from main import app

    gated = {
        route.path
        for route in app.routes
        if getattr(route, "dependant", None) and any(dep.call is require_admin for dep in route.dependant.dependencies)
    }

    assert "/api/admin/infrastructure" in gated
