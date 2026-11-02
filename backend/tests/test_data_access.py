"""The data-access gate (core/data_access.py) and the keys it checks (services/api_keys.py).

Driven through a small app that mounts the gate as the keyed routes do: an
HTTP route behind require_data_access and a websocket behind ws_data_access.
Every test runs as a deployment does, as tests/test_admin_users.py does: the
anonymous-admin bypass off, an administrator a verified Access identity, and
an account its auth_token cookie.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import orjson
import pytest
import uvicorn
from fastapi import Depends, FastAPI, WebSocket
from fastapi.requests import HTTPConnection
from fastapi.testclient import TestClient
from sqlalchemy import select, update
from starlette.testclient import WebSocketDenialResponse
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from core import data_access, sessions, users
from core.api_access import Tier
from core.data_access import DataAccessDenied, Principal, require_data_access, stream_slot, ws_data_access
from core.users import User, UserSession, async_session_maker
from services import api_keys, entitlements
from services.entitlements import TierLimits
from tests.test_admin_users import AS_ADMIN, _Access, _sign_in

ADA = "ada@example.com"
BOB = "bob@example.com"
GRACE = "grace@example.com"
RADAR_KEY = "a-radar-key"

app = FastAPI()


@app.get("/data")
async def data(principal: Principal = Depends(require_data_access)) -> dict[str, Any]:
    return {"kind": principal.kind, "internal": principal.internal}


@app.websocket("/ws/data")
async def data_socket(websocket: WebSocket, principal: Principal = Depends(ws_data_access)) -> None:
    await websocket.accept()
    await websocket.send_json({"kind": principal.kind, "internal": principal.internal})
    await websocket.close()


@app.websocket("/ws/unanswered")
async def unanswered_socket(websocket: WebSocket) -> None:
    """Returns without accepting or refusing: the fault uvicorn's handshake error reports."""


@pytest.fixture(autouse=True)
def _as_deployed(monkeypatch):
    monkeypatch.setattr(users, "AUTH_BYPASS", False)
    monkeypatch.setattr(users, "access_identity", _Access())


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


async def _customer_minute(requests: int) -> None:
    async with async_session_maker() as db, db.begin():
        await db.execute(update(Tier).where(Tier.name == "customer").values(requests_per_minute=requests))


@pytest.fixture
def two_a_minute():
    """The customer tier at 2 requests a minute, and back at 600 after: tests/conftest.py keeps tiers."""
    asyncio.run(_customer_minute(2))
    yield
    asyncio.run(_customer_minute(600))


def _account(email: str, *, entitled: bool = True) -> User:
    user = asyncio.run(users.get_or_create_magic_link_user(email))
    if entitled:
        asyncio.run(entitlements.grant_customer(str(user.id)))
    return user


def _key(user: User) -> tuple[str, str]:
    """A new key of the account: its id, and the key itself."""
    row, key = asyncio.run(api_keys.create_key(str(user.id), "script"))
    return row.id, key


def _get(client: TestClient, headers: dict[str, str] | None = None) -> tuple[int, Any]:
    """The status, and who the gate took the caller for or the reason it refused."""
    response = client.get("/data", headers=headers)
    body = response.json()
    return response.status_code, body if response.status_code == 200 else body["detail"]["reason"]


def test_a_call_presenting_nothing_is_401(client):
    assert _get(client) == (401, "missing_credentials")


def test_a_revoked_or_unknown_key_is_401_even_beside_a_signed_in_session(client):
    ada = _account(ADA)
    key_id, key = _key(ada)
    cookie = _sign_in(ada)
    asyncio.run(api_keys.revoke_key(key_id))

    assert _get(client, cookie) == (200, {"kind": "session", "internal": False})
    assert _get(client, {"x-apikey": key, **cookie}) == (401, "invalid_key")
    assert _get(client, {"x-apikey": "rtk_0123abcd_" + "A" * 43}) == (401, "invalid_key")


def test_a_key_whose_owner_is_not_entitled_or_is_deactivated_is_403(client):
    _, bobs = _key(_account(BOB, entitled=False))
    ada = _account(ADA)
    _, adas = _key(ada)
    asyncio.run(sessions.deactivate_account(ada.id))

    assert _get(client, {"x-apikey": bobs}) == (403, "not_entitled")
    assert _get(client, {"x-apikey": adas}) == (403, "not_entitled")


def test_an_entitled_key_passes_in_either_header_and_its_use_is_recorded(client):
    ada = _account(ADA)
    _, key = _key(ada)

    assert _get(client, {"x-apikey": key}) == (200, {"kind": "key", "internal": False})
    assert _get(client, {"Authorization": f"Bearer {key}"}) == (200, {"kind": "key", "internal": False})
    (row,) = asyncio.run(api_keys.list_keys(str(ada.id)))
    assert row.last_used_at is not None


def test_a_call_over_the_tiers_minute_is_429_with_when_to_retry(client, monkeypatch, two_a_minute):
    # Windows are aligned to the clock: at 1000 s this one ends at 1020 s.
    monkeypatch.setattr(data_access, "_clock", lambda: 1000.0)
    _, key = _key(_account(ADA))

    assert [_get(client, {"x-apikey": key})[0] for _ in range(2)] == [200, 200]
    refused = client.get("/data", headers={"x-apikey": key})
    assert (refused.status_code, refused.json()["detail"]["reason"]) == (429, "rate_limited")
    limits = ("retry-after", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")
    assert {name: refused.headers[name] for name in limits} == {
        "retry-after": "20",
        "x-ratelimit-limit": "2",
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "20",
    }


def test_a_signed_in_session_is_held_to_its_tiers_minute_as_a_key_is(client, monkeypatch, two_a_minute):
    monkeypatch.setattr(data_access, "_clock", lambda: 1000.0)
    cookie = _sign_in(_account(ADA))

    assert [_get(client, cookie) for _ in range(3)] == [
        (200, {"kind": "session", "internal": False}),
        (200, {"kind": "session", "internal": False}),
        (429, "rate_limited"),
    ]


async def test_a_stream_past_the_tiers_count_is_429_until_one_is_given_back():
    contributor = Principal(
        kind="key",
        account_id="ada",
        key_id="adas-script",
        limits=TierLimits("contributor", 60, 2, 86_400),
        internal=False,
    )
    entered = asyncio.Event()

    async def stream() -> None:
        async with stream_slot(contributor):
            entered.set()
            await asyncio.Event().wait()

    first = asyncio.create_task(stream())
    await entered.wait()
    async with stream_slot(contributor):
        with pytest.raises(DataAccessDenied) as refused:
            async with stream_slot(contributor):
                pass
        assert (refused.value.status_code, refused.value.reason) == (429, "stream_limit")
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        async with stream_slot(contributor):
            pass


def test_a_revoke_takes_effect_on_the_very_next_call(client):
    ada = _account(ADA)
    key_id, key = _key(ada)
    grace = _account(GRACE, entitled=False)
    grant = asyncio.run(entitlements.grant_customer(str(grace.id)))
    cookie = _sign_in(grace)
    assert _get(client, {"x-apikey": key})[0] == 200
    assert _get(client, cookie)[0] == 200

    asyncio.run(api_keys.revoke_key(key_id))
    asyncio.run(entitlements.revoke_grant(grant.id))

    assert _get(client, {"x-apikey": key}) == (401, "invalid_key")
    assert _get(client, cookie) == (403, "lapsed")


def test_a_cached_answer_expires_on_its_own_so_a_deactivated_owners_key_stops_within_30_s(client, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(data_access, "_clock", lambda: clock[0])
    ada = _account(ADA)
    _, key = _key(ada)
    assert _get(client, {"x-apikey": key})[0] == 200

    # A deactivation tells the gate nothing: the answer it cached at 1000 s
    # holds until it is 30 s old, and only its expiry refuses the key.
    asyncio.run(sessions.deactivate_account(ada.id))
    clock[0] = 1029.0
    assert _get(client, {"x-apikey": key})[0] == 200
    clock[0] = 1031.0
    assert _get(client, {"x-apikey": key}) == (403, "not_entitled")


def test_the_radar_key_is_internal_access_and_only_while_it_is_set(client, monkeypatch):
    monkeypatch.setenv("RADAR_API_KEY", RADAR_KEY)
    assert _get(client, {"X-API-Key": RADAR_KEY}) == (200, {"kind": "radar", "internal": True})
    assert _get(client, {"X-API-Key": "another-key"}) == (401, "invalid_key")

    monkeypatch.setenv("RADAR_API_KEY", "")
    assert _get(client, {"X-API-Key": RADAR_KEY}) == (401, "invalid_key")
    monkeypatch.delenv("RADAR_API_KEY")
    assert _get(client, {"X-API-Key": RADAR_KEY}) == (401, "invalid_key")


def test_an_entitled_session_passes_and_an_administrator_is_internal_even_with_a_cookie(client):
    cookie = _sign_in(_account(ADA))

    assert _get(client, cookie) == (200, {"kind": "session", "internal": False})
    assert _get(client, {**AS_ADMIN, **cookie}) == (200, {"kind": "admin", "internal": True})


def test_an_access_assertion_that_does_not_verify_makes_nobody_an_administrator(client):
    forged = {"Cf-Access-Jwt-Assertion": "an-assertion-access-never-signed"}
    cookie = _sign_in(_account(ADA))

    assert _get(client, forged) == (401, "missing_credentials")
    assert _get(client, {**forged, **cookie}) == (200, {"kind": "session", "internal": False})
    assert _get(client, AS_ADMIN) == (200, {"kind": "admin", "internal": True})


async def _re_check_two_hours_on(conn: HTTPConnection) -> tuple[Principal, datetime, datetime | None]:
    """An open stream's re-check, its session last used two hours ago: who it passes, then
    that time and the session's last use after it."""
    aged = (datetime.now(UTC) - timedelta(hours=2)).replace(microsecond=0)
    async with async_session_maker() as db, db.begin():
        await db.execute(update(UserSession).values(last_used_at=aged))
    principal = await data_access.check_data_access(conn)
    async with async_session_maker() as db:
        return principal, aged, await db.scalar(select(UserSession.last_used_at))


async def _every_auth_token(conn: HTTPConnection) -> sessions.Resolved | None:
    """resolve_request reading every auth_token a Cookie header carries: the first that signs in."""
    for pair in conn.headers["cookie"].split(";"):
        if (resolved := await sessions.resolve(pair.partition("=")[2].strip())) is not None:
            return resolved
    return None


def test_a_streams_re_check_reads_the_session_its_handshake_admitted_without_sliding_it(monkeypatch):
    # The account's own cookie, then one set on a parent domain by another
    # environment, whose session this server does not hold.
    monkeypatch.setattr(sessions, "resolve_request", _every_auth_token)
    ada = _account(ADA)
    cookie = f"{_sign_in(ada)['Cookie']}; auth_token=a-session-this-server-does-not-hold"
    conn = HTTPConnection({"type": "websocket", "headers": [(b"cookie", cookie.encode())], "query_string": b""})
    assert asyncio.run(data_access.check_data_access(conn)).kind == "session"

    principal, aged, last_used = asyncio.run(_re_check_two_hours_on(conn))
    assert (principal.kind, last_used) == ("session", aged)
    asyncio.run(sessions.deactivate_account(ada.id))
    with pytest.raises(DataAccessDenied) as refused:
        asyncio.run(data_access.check_data_access(conn))
    assert refused.value.reason == "missing_credentials"


def _socket_refusal(client: TestClient, path: str, headers: dict[str, str]) -> tuple[int, str, str | None]:
    """The status, reason and WWW-Authenticate of the HTTP response refusing a socket's upgrade."""
    with pytest.raises(WebSocketDenialResponse) as refused, client.websocket_connect(path, headers=headers):
        pass
    denial = refused.value
    return denial.status_code, denial.json()["detail"]["reason"], denial.headers.get("www-authenticate")


def test_a_socket_opens_on_a_key_and_a_refused_one_is_answered_as_a_rest_call_is(client):
    ada = _account(ADA)
    key_id, key = _key(ada)

    with client.websocket_connect(f"/ws/data?apikey={key}") as socket:
        assert socket.receive_json() == {"kind": "key", "internal": False}
    elsewhere = {"origin": "https://elsewhere.example.com", **_sign_in(ada)}
    assert _socket_refusal(client, "/ws/data", elsewhere) == (403, "forbidden_origin", None)
    asyncio.run(api_keys.revoke_key(key_id))
    assert _socket_refusal(client, f"/ws/data?apikey={key}", {}) == (401, "invalid_key", "Bearer")


async def test_a_refusal_reaches_a_real_client_as_http_and_only_an_unanswered_socket_logs_an_error(monkeypatch, caplog):
    import main  # noqa: F401  (installs the log filter, as on the server)

    monkeypatch.setattr(logging.getLogger("uvicorn"), "propagate", True)
    server = uvicorn.Server(uvicorn.Config(app, port=0, log_config=None, lifespan="off"))
    serving = asyncio.create_task(server.serve())
    async with asyncio.timeout(5):
        while not server.started:
            await asyncio.sleep(0.01)
    base = f"ws://127.0.0.1:{server.servers[0].sockets[0].getsockname()[1]}"
    answers = []
    for path in ("/ws/data", "/ws/unanswered"):
        with pytest.raises(InvalidStatus) as refused:
            async with connect(base + path):
                pass
        answers.append(refused.value.response)
    server.should_exit = True
    await serving

    denial, unanswered = answers
    assert (denial.status_code, denial.headers["WWW-Authenticate"], orjson.loads(denial.body)) == (
        401,
        "Bearer",
        {"detail": {"reason": "missing_credentials", "message": "Send an API key, or sign in."}},
    )
    errors = [record.getMessage() for record in caplog.records if record.levelno >= logging.ERROR]
    assert (unanswered.status_code, errors) == (500, ["ASGI callable returned without completing handshake."])
