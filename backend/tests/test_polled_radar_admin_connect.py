"""Tests for an administrator connecting a stock blah2 radar to an email address.

The address need not have an account: one is made for it, unverified, and the
radar runs on it from the moment it is connected. The first sign-in by link
proves the address and marks the account verified. Until then the administrator
can take the connection back, since a mistyped address is the one thing the
by-eye check can miss.

Connecting works whether or not owners may register, since it is how staff bring
radars on before registration opens, but only where this server polls.
"""

import asyncio
from unittest.mock import patch

import pytest
from sqlalchemy import select

from core import state
from core.auth import create_magic_link
from core.nodes import Node, NodeClaim, NodeEvent, PolledRadar
from core.users import ANONYMOUS_USER, User, accounts_by_id, async_session_maker, get_or_create_magic_link_user
from services import blah2_poller, node_retirement, polled_registration, publication
from services.node_rate_limits import polled_probe_limiter
from services.polled_radars import create_polled_radar
from tests.test_polled_radar_registration import STOCK_FINGERPRINT, _canned
from tests.test_polled_radars import _args as _row_args

PROBE = "/api/admin/polled-radars/probe"
CONNECT = "/api/admin/polled-radars"
ADDRESS = "radar.example.com:3000"
EMAIL = "ada@example.com"
SUBMIT = {"address": ADDRESS, "fingerprint": STOCK_FINGERPRINT, "publication": "private", "email": EMAIL}
ADMIN = f"admin:{ANONYMOUS_USER['email']}"


def _run(coro):
    result = asyncio.run(coro)
    # asyncio.run() clears the loop on exit (3.12); conftest's _clean_db
    # restores one for the same reason.
    asyncio.set_event_loop(asyncio.new_event_loop())
    return result


@pytest.fixture(autouse=True)
def _allowance_and_registries_left_as_found():
    polled_probe_limiter.reset()
    before = set(state.connected_nodes)
    forget = node_retirement.forget_node
    yield
    polled_probe_limiter.reset()
    for node_id in set(state.connected_nodes) - before:
        forget(node_id)


@pytest.fixture()
def polling(client, monkeypatch):
    # After the client, whose lifespan would otherwise start a real poller.
    monkeypatch.setenv(blah2_poller.ENABLED_ENV, "1")
    monkeypatch.delenv(polled_registration.ENABLED_ENV, raising=False)


@pytest.fixture()
def probes(monkeypatch):
    sent = []
    canned = _canned()

    async def probe_blah2(endpoint, *, resolver, policy):
        sent.append(endpoint.endpoint_key)
        return await canned(endpoint, resolver=resolver, policy=policy)

    monkeypatch.setattr(polled_registration, "probe_blah2", probe_blah2)
    return sent


@pytest.fixture()
def poller_wakes(monkeypatch):
    wakes = []
    monkeypatch.setattr(blah2_poller, "refresh", lambda: wakes.append(True))
    return wakes


def _account(email: str) -> User | None:
    async def read():
        async with async_session_maker() as session:
            return (await session.execute(select(User).where(User.email == email))).scalar_one_or_none()

    return _run(read())


def _claim(node_id: str) -> NodeClaim | None:
    async def read():
        async with async_session_maker() as session:
            return await session.get(NodeClaim, node_id)

    return _run(read())


def _events(node_id: str) -> list[tuple[str, int | None, str]]:
    async def read():
        async with async_session_maker() as session:
            rows = await session.scalars(select(NodeEvent).where(NodeEvent.node_id == node_id).order_by(NodeEvent.id))
            return [(e.kind, e.epoch, e.actor) for e in rows]

    return _run(read())


def _connected(client, **changes) -> str:
    r = client.post(CONNECT, json=SUBMIT | changes)
    assert r.status_code == 201, r.text
    return r.json()["node_id"]


def _signs_in(client, email: str = EMAIL) -> dict:
    token = _run(create_magic_link(email))
    return client.post("/api/auth/magic-link/consume", json={"token": token}).json()["user"]


def _listed(client, node_id: str) -> dict:
    return next(r for r in client.get(CONNECT).json()["radars"] if r["node_id"] == node_id)


# ── Checking ─────────────────────────────────────────────────────────────────


def test_a_check_says_what_the_radar_declares_and_that_the_address_has_no_account(client, polling, probes):
    r = client.post(PROBE, json={"address": ADDRESS, "email": EMAIL})

    assert r.status_code == 200
    assert (r.json()["address"], r.json()["fingerprint"]) == (f"http://{ADDRESS}", STOCK_FINGERPRINT)
    assert r.json()["account"] == {"email": EMAIL, "exists": False}
    assert _account(EMAIL) is None


def test_a_check_finds_an_account_whatever_the_case_the_address_was_typed_in(client, polling, probes):
    _run(get_or_create_magic_link_user(EMAIL))

    r = client.post(PROBE, json={"address": ADDRESS, "email": "Ada@Example.COM"})

    assert r.json()["account"] == {"email": EMAIL, "exists": True}


def test_a_check_finds_an_account_stored_in_another_case_as_connecting_would(client, polling, probes):
    async def older_account():
        async with async_session_maker() as session:
            session.add(User(email="Ada@Example.com", hashed_password="unused"))
            await session.commit()

    _run(older_account())

    assert client.post(PROBE, json={"address": ADDRESS, "email": EMAIL}).json()["account"]["exists"] is True


def test_a_check_is_metered_against_the_administrator_not_the_account(client, polling, probes):
    ada = _run(get_or_create_magic_link_user(EMAIL))
    while polled_probe_limiter.admit(str(ada.id)) is None:
        pass
    assert client.post(PROBE, json={"address": ADDRESS, "email": EMAIL}).status_code == 200

    while polled_probe_limiter.admit(ANONYMOUS_USER["id"]) is None:
        pass
    r = client.post(PROBE, json={"address": ADDRESS, "email": EMAIL})

    assert (r.status_code, r.json()["code"]) == (429, "rate_limited")


def test_a_connection_is_metered_against_the_administrator(client, polling, probes, poller_wakes):
    while polled_probe_limiter.admit(ANONYMOUS_USER["id"]) is None:
        pass

    r = client.post(CONNECT, json=SUBMIT)

    assert (r.status_code, r.json()["code"]) == (429, "rate_limited")
    assert _account(EMAIL) is None


def test_an_address_that_is_not_one_is_refused(client, polling, probes):
    assert client.post(PROBE, json={"address": ADDRESS, "email": "not-an-address"}).status_code == 422
    assert probes == []


# ── Connecting ───────────────────────────────────────────────────────────────


def test_a_radar_connected_to_a_new_address_runs_at_once_on_an_account_made_for_it(
    client, polling, probes, poller_wakes
):
    r = client.post(CONNECT, json=SUBMIT)

    assert r.status_code == 201
    node_id = r.json()["node_id"]
    assert r.json() == {
        "node_id": node_id,
        "epoch": 1,
        "trust_state": "probation",
        "owner": {"email": EMAIL, "created": True},
    }
    account = _account(EMAIL)
    assert (account.is_verified, account.is_superuser, account.provider) == (False, False, "admin")
    claim = _claim(node_id)
    # An owner an administrator chose: nobody has confirmed the address yet.
    assert (claim.user_id, claim.email, claim.verified) == (str(account.id), EMAIL, False)
    assert poller_wakes == [True]
    assert "detection_area" in state.node_analytics.get_node_summary(node_id)
    assert publication.is_private(node_id)
    assert _events(node_id) == [("registered", 1, ADMIN)]


def test_a_radar_connected_to_an_address_with_an_account_goes_to_that_account(client, polling, probes, poller_wakes):
    ada = _run(get_or_create_magic_link_user(EMAIL))

    r = client.post(CONNECT, json=SUBMIT | {"email": "ADA@example.com"})

    assert r.json()["owner"] == {"email": EMAIL, "created": False}
    claim = _claim(r.json()["node_id"])
    # The account's address, not the administrator's typing of it.
    assert (claim.user_id, claim.email) == (str(ada.id), EMAIL)


def test_connecting_works_while_owners_may_not_register(client, polling, probes, poller_wakes):
    assert not polled_registration.enabled()

    assert client.post(CONNECT, json=SUBMIT).status_code == 201
    assert client.post("/api/auth/me/polled-radars/probe", json={"address": ADDRESS}).status_code == 404


def test_an_account_holds_more_radars_than_an_owner_may_register(client, polling, probes, poller_wakes):
    ada = _run(get_or_create_magic_link_user(EMAIL))

    async def seed():
        async with async_session_maker() as session:
            for i in range(polled_registration.MAX_RADARS_PER_ACCOUNT):
                await create_polled_radar(
                    session,
                    **_row_args(
                        owner_user_id=str(ada.id),
                        owner_email=EMAIL,
                        endpoint_key=f"r{i}.example.com:3000",
                        host=f"r{i}.example.com",
                    ),
                )
            await session.commit()

    _run(seed())

    assert client.post(CONNECT, json=SUBMIT).status_code == 201


def test_a_refused_connection_leaves_no_account_behind(client, polling, monkeypatch, poller_wakes):
    monkeypatch.setattr(polled_registration, "probe_blah2", _canned(fingerprint="b" * 64))

    r = client.post(CONNECT, json=SUBMIT)

    assert (r.status_code, r.json()["code"]) == (409, "config_changed")
    assert r.json()["probe"]["fingerprint"] == "b" * 64
    assert _account(EMAIL) is None
    assert poller_wakes == []


@pytest.mark.parametrize(("path", "body"), [(PROBE, {"address": ADDRESS, "email": EMAIL}), (CONNECT, SUBMIT)])
def test_nothing_is_probed_or_connected_where_this_server_does_not_poll(client, probes, monkeypatch, path, body):
    monkeypatch.delenv(blah2_poller.ENABLED_ENV, raising=False)

    r = client.post(path, json=body)

    assert (r.status_code, r.json()["code"]) == (409, "polling_off")
    assert probes == []
    assert _account(EMAIL) is None


def test_the_list_says_whether_this_server_polls(client, polling, monkeypatch):
    assert client.get(CONNECT).json()["polling_enabled"] is True

    monkeypatch.delenv(blah2_poller.ENABLED_ENV)

    assert client.get(CONNECT).json()["polling_enabled"] is False


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("post", PROBE, {"address": ADDRESS, "email": EMAIL}),
        ("post", CONNECT, SUBMIT),
        ("delete", f"{CONNECT}/bla00000000", None),
    ],
)
def test_every_route_is_for_administrators(client, polling, probes, method, path, body):
    with patch("core.users.AUTH_BYPASS", False):
        assert client.request(method, path, json=body).status_code == 401
    assert probes == []


# ── The owner's first sign-in ────────────────────────────────────────────────


def test_the_first_sign_in_by_the_address_verifies_the_account_the_radar_is_on(client, polling, probes, poller_wakes):
    node_id = _connected(client)
    assert _listed(client, node_id)["owner"] == {"email": EMAIL, "signed_in": False}

    user = _signs_in(client)

    assert user["id"] == _claim(node_id).user_id
    assert _account(EMAIL).is_verified is True
    assert _listed(client, node_id)["owner"] == {"email": EMAIL, "signed_in": True}


def test_the_list_names_the_account_a_radar_was_reassigned_to(client, polling, probes, poller_wakes):
    node_id = _connected(client)
    bob = _run(get_or_create_magic_link_user("bob@example.com"))

    assert client.put(f"/api/admin/nodes/{node_id}/owner", json={"user_id": str(bob.id)}).status_code == 200

    assert _listed(client, node_id)["owner"] == {"email": "bob@example.com", "signed_in": True}


async def test_an_owner_id_that_names_no_account_finds_none():
    ada = await get_or_create_magic_link_user(EMAIL)
    async with async_session_maker() as session:
        found = await accounts_by_id(session, [str(ada.id), "user-a", "00000000-0000-0000-0000-000000000000"])

    assert list(found) == [str(ada.id)]


async def test_signing_in_leaves_an_account_that_was_already_verified_as_it_was():
    first = await get_or_create_magic_link_user(EMAIL)
    again = await get_or_create_magic_link_user(EMAIL)

    assert (again.id, again.is_verified) == (first.id, True)


# ── Taking a connection back ─────────────────────────────────────────────────


def test_a_connection_is_taken_back_with_its_account_while_nobody_has_signed_in(client, polling, probes, poller_wakes):
    node_id = _connected(client)

    r = client.delete(f"{CONNECT}/{node_id}")

    assert r.status_code == 200
    assert _account(EMAIL) is None
    assert _claim(node_id) is None
    assert _events(node_id) == [("registered", 1, ADMIN), ("removed", 1, ADMIN)]
    assert poller_wakes == [True, True]
    assert node_id not in state.connected_nodes

    async def read():
        async with async_session_maker() as session:
            return (await session.get(Node, node_id)).status, await session.get(PolledRadar, node_id)

    assert _run(read()) == ("retired", None)


def test_an_account_holding_another_radar_stays_when_one_is_taken_back(client, polling, probes, poller_wakes):
    node_id = _connected(client)
    _connected(client, address="other.example.com:3000")

    assert client.delete(f"{CONNECT}/{node_id}").status_code == 200
    assert _account(EMAIL) is not None


def test_a_radar_whose_owner_has_signed_in_is_not_taken_back(client, polling, probes, poller_wakes):
    node_id = _connected(client)
    _signs_in(client)

    r = client.delete(f"{CONNECT}/{node_id}")

    assert r.status_code == 409
    assert _claim(node_id).user_id == str(_account(EMAIL).id)
    assert poller_wakes == [True]


def test_a_radar_connected_to_an_account_already_in_use_is_not_taken_back(client, polling, probes, poller_wakes):
    _run(get_or_create_magic_link_user(EMAIL))
    node_id = _connected(client)

    assert client.delete(f"{CONNECT}/{node_id}").status_code == 409


def test_a_radar_an_administrator_registered_as_its_owner_is_not_taken_back(
    client, polling, probes, poller_wakes, monkeypatch
):
    # Registered through the owner's route, under an identity with no account row.
    monkeypatch.setenv(polled_registration.ENABLED_ENV, "1")
    node_id = client.post("/api/auth/me/polled-radars", json=SUBMIT).json()["node_id"]

    assert _listed(client, node_id)["owner"]["signed_in"] is True
    assert client.delete(f"{CONNECT}/{node_id}").status_code == 409


def test_only_a_polled_radar_can_be_taken_back(client, polling):
    assert client.delete(f"{CONNECT}/bla00000000").status_code == 404
    assert client.delete(f"{CONNECT}/ret1a2b3c4d").status_code == 404
