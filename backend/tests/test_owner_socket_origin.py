"""/ws/aircraft/owner opens only for a page of the console itself.

A browser sends the session cookie with a WebSocket handshake whatever page
opened it, so the handshake's Origin is judged before the cookie is read
(core/origins.py). The console's own pages are each environment's HOST_APP and
HOST_ADMIN, taken here from the compose overlays that set them, with the
origins their CORS_ORIGINS name for those hosts; loopback names pass only
under RETINA_ENV=dev. A handshake with no Origin is not a browser's, and the
cookie decides it as it always has.
"""

import asyncio
import time
from pathlib import Path
from urllib.parse import urlsplit

import orjson
import pytest
import yaml
from starlette.websockets import WebSocketDisconnect

from core import sessions, state
from routes import streaming
from tests.owner_helpers import as_user, real_sessions, sign_in, user_id_of
from tests.ownership_helpers import own

REPO = Path(__file__).resolve().parents[2]
FEED = "/ws/aircraft/owner"
MINE = "test-owner-kept"  # the synthetic prefix publishes as itself
OTHERS = "test-someone-elses"

PRODUCTION = {"RETINA_ENV": "production", "HOST_APP": "app.retina.fm", "HOST_ADMIN": "admin.retina.fm"}


class _ComposeLoader(yaml.SafeLoader):
    """Compose's own tags (`!reset`, `!override`) read as the value they tag."""


def _untagged(loader, _suffix, node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node, deep=True)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node, deep=True)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("!", _untagged)


def _overlays() -> dict[str, dict[str, str]]:
    """The server's environment as each compose overlay sets it, by overlay."""
    out = {}
    for path in sorted(REPO.glob("docker-compose.*.yml")):
        server = yaml.load(path.read_text(), _ComposeLoader)["services"]["server"]
        out[path.name] = dict(entry.split("=", 1) for entry in server["environment"])
    return out


def _console_origins() -> list[tuple[str, dict[str, str], str]]:
    """(overlay, its settings, an origin of its console), for every origin its
    CORS_ORIGINS names on HOST_APP or HOST_ADMIN."""
    cases = []
    for name, env in _overlays().items():
        settings = {key: env[key] for key in ("RETINA_ENV", "HOST_APP", "HOST_ADMIN")}
        console = {settings["HOST_APP"], settings["HOST_ADMIN"]}
        for origin in env["CORS_ORIGINS"].split(","):
            if urlsplit(origin).hostname in console:
                cases.append((name, settings, origin))
    return cases


def _other_origins() -> list[tuple[str, dict[str, str], str]]:
    """The origins each overlay's CORS_ORIGINS names that are not the console's:
    the towers host, which deployed environments route to tower-finder-service,
    and the API host."""
    cases = []
    for name, env in _overlays().items():
        settings = {key: env[key] for key in ("RETINA_ENV", "HOST_APP", "HOST_ADMIN")}
        for origin in env["CORS_ORIGINS"].split(","):
            if urlsplit(origin).hostname not in {settings["HOST_APP"], settings["HOST_ADMIN"]}:
                cases.append((name, settings, origin))
    return cases


def _configure(monkeypatch, settings: dict[str, str]) -> None:
    for key, value in settings.items():
        monkeypatch.setenv(key, value)


def _served(ws) -> list[str]:
    return [aircraft["hex"] for aircraft in orjson.loads(ws.receive_text())["aircraft"]]


@pytest.fixture
def cookie_reads(monkeypatch):
    """Every session the handler looks up, so a refusal can be shown to come first."""
    reads = []
    real = streaming.read_user_from_token

    async def recorded(token):
        reads.append(token)
        return await real(token)

    monkeypatch.setattr(streaming, "read_user_from_token", recorded)
    return reads


@pytest.fixture
def owner(client, monkeypatch):
    """An owner of MINE signed in on the client, a frame holding MINE and
    somebody else's node waiting, and production's settings."""
    real_sessions(monkeypatch)
    monkeypatch.setattr(streaming, "_WS_AUTH_TOKEN", "")
    monkeypatch.setattr(state, "aircraft_dirty", False)
    _configure(monkeypatch, PRODUCTION)
    token = sign_in(client, "owner@example.com")
    own(MINE, user_id_of("owner@example.com"))
    monkeypatch.setattr(
        state,
        "latest_aircraft_json",
        {"now": 1.0, "aircraft": [{"hex": nid, "node_id": nid} for nid in (MINE, OTHERS)], "detection_arcs": []},
    )
    as_user(client, token)
    return token


def _refused(client, origin: str) -> int:
    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(FEED, headers={"origin": origin}):
        pass
    return refused.value.code


@pytest.mark.parametrize(
    "origin",
    [
        "https://evil.example",
        "https://other.retina.fm",
        "null",
        "https://app.retina.fm.evil.example",
        "https://evil.example/https://app.retina.fm",
        "chrome-extension://abcdefghijklmnop",
        "http://[::1",
    ],
)
def test_a_page_that_is_not_the_consoles_is_refused_before_the_cookie_is_read(client, owner, cookie_reads, origin):
    assert _refused(client, origin) == 1008
    assert cookie_reads == []


@pytest.mark.parametrize(
    "overlay, settings, origin", _console_origins(), ids=[f"{c[0]} {c[2]}" for c in _console_origins()]
)
def test_each_environments_console_opens_the_feed(client, owner, monkeypatch, overlay, settings, origin):
    _configure(monkeypatch, settings)
    with client.websocket_connect(FEED, headers={"origin": origin}) as ws:
        assert _served(ws) == [MINE]


@pytest.mark.parametrize(
    "overlay, settings, origin", _other_origins(), ids=[f"{c[0]} {c[2]}" for c in _other_origins()]
)
def test_the_towers_and_api_hosts_are_not_the_console(client, owner, monkeypatch, overlay, settings, origin):
    _configure(monkeypatch, settings)
    assert _refused(client, origin) == 1008


def test_every_overlay_names_a_console_origin():
    assert {overlay for overlay, _, _ in _console_origins()} == set(_overlays())


def test_without_an_origin_the_cookie_decides(client, owner):
    with client.websocket_connect(FEED) as ws:
        assert _served(ws) == [MINE]
    as_user(client, None)
    with pytest.raises(WebSocketDisconnect) as refused, client.websocket_connect(FEED):
        pass
    assert (refused.value.code, refused.value.reason) == (1008, "Unauthorized")


@pytest.mark.parametrize(
    "origin", ["http://localhost:5174", "http://app.localhost:5174", "http://127.0.0.1:5174", "http://[::1]:5174"]
)
def test_the_dev_servers_loopback_pages_open_it_in_dev_alone(client, owner, monkeypatch, origin):
    assert _refused(client, origin) == 1008

    monkeypatch.setenv("RETINA_ENV", "dev")
    with client.websocket_connect(FEED, headers={"origin": origin}) as ws:
        assert _served(ws) == [MINE]


def test_the_bypass_admin_is_held_to_the_origin_as_well(client, monkeypatch):
    monkeypatch.setattr(streaming, "_WS_AUTH_TOKEN", "")
    _configure(monkeypatch, PRODUCTION)
    assert streaming.AUTH_BYPASS
    assert _refused(client, "https://evil.example") == 1008


@pytest.mark.parametrize("ending", ["revoked", "deactivated"])
def test_a_feed_a_console_page_opened_closes_with_1008_when_its_session_ends(client, owner, ending):
    """The origin is judged in front of the session's own rules, not instead of
    them: a socket it lets through is still closed as soon as its session ends
    (routes/streaming.py, _close_when_signed_out)."""
    user_id = user_id_of("owner@example.com")
    with client.websocket_connect(FEED, headers={"origin": "https://app.retina.fm"}) as ws:
        assert _served(ws) == [MINE]
        if ending == "revoked":
            (row,) = asyncio.run(sessions.list_sessions(user_id))
            assert asyncio.run(sessions.revoke_session(user_id, row.id))
        else:
            assert asyncio.run(sessions.deactivate_account(user_id)) is not None
        deadline = time.monotonic() + 2.0
        while state.ws_owner_clients:
            assert time.monotonic() < deadline, "the socket was not closed"
            time.sleep(0.01)
        with pytest.raises(WebSocketDisconnect) as closed:
            ws.receive_text()
    assert (closed.value.code, closed.value.reason) == (1008, "Unauthorized")
