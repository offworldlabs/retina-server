"""The test router's reads answer an administrator or the radar key, and no one else.

They describe this server's process rather than the network: queue depths and
dropped frames, the solver's records, ground truth. The admin console reads them
under Access, and the deploy checks and scripted soak checks with the key.

The suite reaches admin routes through the anonymous-admin bypass, which would
admit every request here, so each test that asserts a refusal turns it off.
"""

import pytest
from fastapi.testclient import TestClient

import core.users as users
import routes.test as test_routes
from main import app

KEY = test_routes._RADAR_API_KEY

# One concrete path per read. A node ref no node holds is enough: the gate
# answers before the handler resolves anything.
READS = {
    "/api/test/dashboard": "/api/test/dashboard",
    "/api/test/ground-truth/{hex_code}": "/api/test/ground-truth/abc123",
    "/api/test/known-hold": "/api/test/known-hold",
    "/api/test/node/{node_ref}/verification": "/api/test/node/no-such-ref/verification",
    "/api/test/mlat-verification": "/api/test/mlat-verification",
    "/api/test/mlat-history": "/api/test/mlat-history",
    "/api/test/mlat-accuracy": "/api/test/mlat-accuracy",
    "/api/test/node/{node_ref}/detection-range": "/api/test/node/no-such-ref/detection-range",
    "/api/test/solver-stats": "/api/test/solver-stats",
}

# The router's other reads, each deliberately outside the gate: the fleet polls
# its scene with no credentials, and the physics page's ground truth stays
# administrator-only.
OTHER_READS = {"/api/simulation/config", "/api/simulation/ground-truth"}


def _admitted(status: int) -> bool:
    """Past the gate, and answered by the handler: its own 4xx counts, a crash does not."""
    return status not in (401, 403) and status < 500


@pytest.fixture(scope="module")
def client():
    """No lifespan: the gate answers before any handler reads what it starts."""
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def no_bypass(monkeypatch):
    monkeypatch.setattr(users, "AUTH_BYPASS", False)


def test_every_read_the_router_serves_is_listed_here():
    """A new read route is gated or deliberately not, never by omission."""
    served = {
        r.path
        for r in app.routes
        if r.path.startswith(("/api/test/", "/api/simulation/")) and "GET" in getattr(r, "methods", ())
    }
    assert served == set(READS) | OTHER_READS


@pytest.mark.usefixtures("no_bypass")
@pytest.mark.parametrize("path", READS.values())
def test_an_anonymous_read_is_refused(client, path):
    assert client.get(path).status_code == 401


@pytest.mark.usefixtures("no_bypass")
@pytest.mark.parametrize("path", READS.values())
def test_a_wrong_key_is_refused(client, path):
    assert client.get(path, headers={"X-API-Key": "not-the-key"}).status_code == 401


@pytest.mark.usefixtures("no_bypass")
@pytest.mark.parametrize("path", READS.values())
def test_the_radar_key_is_admitted(client, path):
    assert _admitted(client.get(path, headers={"X-API-Key": KEY}).status_code)


@pytest.mark.parametrize("path", READS.values())
def test_an_administrator_is_admitted(client, path):
    assert _admitted(client.get(path).status_code)


@pytest.mark.usefixtures("no_bypass")
@pytest.mark.parametrize("path", READS.values())
def test_a_signed_in_user_who_is_not_an_administrator_is_refused(client, monkeypatch, path):
    async def _member(_request):
        return {"id": "u1", "email": "member@example.com", "is_superuser": False}

    monkeypatch.setattr(users, "get_optional_user", _member)
    assert client.get(path).status_code == 403


@pytest.mark.usefixtures("no_bypass")
def test_a_box_with_no_key_admits_no_one_by_key(client, monkeypatch):
    """An unset key must not make an empty header the key."""
    monkeypatch.setattr(test_routes, "_RADAR_API_KEY", "")
    assert client.get("/api/test/dashboard", headers={"X-API-Key": ""}).status_code == 401


@pytest.mark.usefixtures("no_bypass")
def test_the_scene_the_fleet_polls_stays_open(client):
    """The retina-test fleet reads its scene with no credentials."""
    assert client.get("/api/simulation/config").status_code == 200
