"""Production's node registry reaches the receivers, and only there.

The source is a per-test database (`node_session`), the receiver the suite's
own (`async_session_maker`), so a push crosses between two databases the way it
does between two droplets: no id, account or row is shared by accident.
"""

import asyncio
import hashlib
import json
import sqlite3
import time
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

from core import sessions
from core.auth import get_user_tenures
from core.nodes import Node, NodeClaim, NodeConfig, NodeEvent, NodeReport
from core.users import User, async_session_maker, get_or_create_magic_link_user
from routes import registry_sync
from services import registry_mirror
from tests.ownership_helpers import own_registered_node

KEY = "receiver-key-for-the-registry-mirror"
OWNER = "owner@example.com"
T0 = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
async def held_file(tmp_path, monkeypatch):
    path = tmp_path / "registry_mirror.json"
    monkeypatch.setattr(registry_mirror, "_held_path", lambda: path)
    yield path
    # The suite's own cleanup (conftest._clean_db) truncates nodes but not
    # node_reports, whose key on nodes would then refuse it.
    async with async_session_maker() as session:
        await session.execute(delete(NodeReport))
        await session.commit()


def _config(node_id: str, version: int, **kw) -> NodeConfig:
    values = dict(
        node_id=node_id,
        version=version,
        rx_lat=34.79,
        rx_lon=-82.35,
        fc_hz=599e6,
        fs_hz=2e6,
        max_range_km=60.0,
        cpi_s=0.5,
        delay_tolerance_us=2.0,
        doppler_tolerance_hz=5.0,
    )
    values.update(kw)
    return NodeConfig(**values)


async def _seed_source(session, node_id: str = "ret1a2b3c4d", ref: str = "ndeaaaaaaaa0001") -> None:
    """One registered, configured, owned node with a report and an event, as production holds it."""
    owner = User(email=OWNER, hashed_password="x", is_active=True, is_verified=True)
    session.add(owner)
    session.add(Node(node_id=node_id, node_ref=ref, board_model="raspberrypi5-4gb", publication="private"))
    await session.flush()
    session.add(_config(node_id, 1, superseded_at=T0))
    session.add(_config(node_id, 2))
    session.add(NodeClaim(node_id=node_id, user_id=str(owner.id), email=OWNER, verified=True))
    session.add(
        NodeReport(
            node_id=node_id,
            received_at=T0,
            boot_id="b" * 32,
            state="running",
            state_since=T0,
            uptime_s=60,
            config_version=2,
            health={"cpu": 0.5},
            versions={},
            errors=[],
        )
    )
    session.add(NodeEvent(node_id=node_id, kind="claimed", actor=OWNER, occurred_at=T0))
    await session.commit()


async def _receiver_registry() -> dict:
    async with async_session_maker() as session:
        return await registry_mirror.read_registry(session)


async def _push(registry: dict, target: registry_mirror.Target, full: bool) -> dict:
    push = registry_mirror.plan_push(target, registry, full)
    result: dict = {"written": {}, "removed": {}, "skipped": {}}
    for chunk in registry_mirror.chunk_push(push, limit=600):
        landed = await registry_mirror.apply(chunk)
        for part, counts in landed.items():
            for table, n in counts.items():
                result[part][table] = result[part].get(table, 0) + n
    return result


def _target() -> registry_mirror.Target:
    return registry_mirror.Target(label="test-api.example", url="https://test-api.example", key=KEY)


# ── The round trip ───────────────────────────────────────────────────────────


async def test_a_full_push_lands_the_registry_as_production_holds_it(node_session):
    await _seed_source(node_session)
    source = await registry_mirror.read_registry(node_session)

    await _push(source, _target(), full=True)

    assert await _receiver_registry() == source


async def test_the_owner_is_bound_to_the_receivers_account_at_that_address(node_session):
    await _seed_source(node_session)
    await _push(await registry_mirror.read_registry(node_session), _target(), full=True)

    async with async_session_maker() as session:
        account = await session.scalar(select(User).where(User.email == OWNER))
        claim = await session.get(NodeClaim, "ret1a2b3c4d")
    assert account is not None
    assert claim.user_id == str(account.id)


async def test_an_existing_account_at_the_address_is_the_owner_not_a_new_one(node_session):
    async with async_session_maker() as session:
        mine = User(email=OWNER, hashed_password="x", is_active=True, is_verified=True)
        session.add(mine)
        await session.commit()
    await _seed_source(node_session)
    await _push(await registry_mirror.read_registry(node_session), _target(), full=True)

    async with async_session_maker() as session:
        assert (await session.get(NodeClaim, "ret1a2b3c4d")).user_id == str(mine.id)
        assert len((await session.scalars(select(User).where(User.email == OWNER))).all()) == 1


async def test_a_push_spans_chunks_in_foreign_key_order(node_session):
    await _seed_source(node_session)
    push = registry_mirror.plan_push(_target(), await registry_mirror.read_registry(node_session), True)
    chunks = registry_mirror.chunk_push(push, limit=600)

    assert len(chunks) > 1
    order = [name for chunk in chunks for name in chunk["upserts"]]
    assert order == sorted(order, key=[t.name for t in registry_mirror.TABLES].index)
    assert all("full_keys" not in c and "deletes" not in c for c in chunks[:-1])
    assert chunks[-1]["last"] and "full_keys" in chunks[-1]


# ── Diffs and production winning ─────────────────────────────────────────────


async def test_an_unchanged_registry_sends_nothing_after_the_full_push(node_session):
    await _seed_source(node_session)
    registry = await registry_mirror.read_registry(node_session)
    target = _target()
    target.accepted = {
        name: {key: registry_mirror._digest(row) for key, row in rows.items()} for name, rows in registry.items()
    }

    push = registry_mirror.plan_push(target, registry, full=False)

    assert push == {"upserts": {}, "deletes": {}}


async def test_a_local_edit_stands_until_production_changes_that_row(node_session):
    await _seed_source(node_session)
    target = _target()
    first = await registry_mirror.read_registry(node_session)
    await _push(first, target, full=True)
    target.accepted = {n: {k: registry_mirror._digest(r) for k, r in rows.items()} for n, rows in first.items()}

    async with async_session_maker() as session:
        (await session.get(Node, "ret1a2b3c4d")).publication = "public"
        await session.commit()

    # Production unchanged: the diff is empty and the edit stays.
    await _push(await registry_mirror.read_registry(node_session), target, full=False)
    async with async_session_maker() as session:
        assert (await session.get(Node, "ret1a2b3c4d")).publication == "public"

    # Production changes the row: production's version wins.
    (await node_session.get(Node, "ret1a2b3c4d")).board_model = "raspberrypi5-8gb"
    await node_session.commit()
    await _push(await registry_mirror.read_registry(node_session), target, full=False)
    async with async_session_maker() as session:
        node = await session.get(Node, "ret1a2b3c4d")
    assert (node.publication, node.board_model) == ("private", "raspberrypi5-8gb")


async def test_a_row_production_dropped_is_dropped_by_the_next_diff(node_session):
    await _seed_source(node_session)
    target = _target()
    first = await registry_mirror.read_registry(node_session)
    await _push(first, target, full=True)
    target.accepted = {n: {k: registry_mirror._digest(r) for k, r in rows.items()} for n, rows in first.items()}

    await node_session.delete(await node_session.get(NodeClaim, "ret1a2b3c4d"))
    await node_session.commit()
    await _push(await registry_mirror.read_registry(node_session), target, full=False)

    async with async_session_maker() as session:
        assert await session.get(NodeClaim, "ret1a2b3c4d") is None
        assert await session.get(Node, "ret1a2b3c4d") is not None


async def test_a_full_push_removes_what_production_no_longer_lists_and_keeps_local_nodes(node_session):
    await _seed_source(node_session)
    node_session.add(Node(node_id="retdeadbeef", node_ref="ndebbbbbbbb0002"))
    await node_session.commit()
    await _push(await registry_mirror.read_registry(node_session), _target(), full=True)
    async with async_session_maker() as session:
        session.add(Node(node_id="synth-LOCAL-0001", node_ref="ndelocal0000001"))
        await session.commit()

    # Production retires one node; a restarted sender only knows to send a full push.
    await node_session.delete(await node_session.get(Node, "retdeadbeef"))
    await node_session.commit()
    await _push(await registry_mirror.read_registry(node_session), _target(), full=True)

    async with async_session_maker() as session:
        ids = set((await session.scalars(select(Node.node_id))).all())
    assert ids == {"ret1a2b3c4d", "synth-LOCAL-0001"}


async def test_a_node_whose_ref_is_taken_here_is_skipped_and_the_rest_lands(node_session):
    await _seed_source(node_session)
    async with async_session_maker() as session:
        session.add(Node(node_id="synth-LOCAL-0001", node_ref="ndeaaaaaaaa0001"))
        await session.commit()
    node_session.add(Node(node_id="ret9f8e7d6c", node_ref="ndecccccccc0003"))
    await node_session.commit()
    registry = await registry_mirror.read_registry(node_session)
    registry["node_configs"] = {}
    registry["node_claims"] = {}
    registry["node_reports"] = {}
    registry["node_events"] = {}

    result = await _push(registry, _target(), full=True)

    async with async_session_maker() as session:
        ids = set((await session.scalars(select(Node.node_id))).all())
    assert ids == {"synth-LOCAL-0001", "ret9f8e7d6c"}
    assert result["skipped"] == {"nodes": 1}


async def test_a_push_commits_while_the_event_loop_is_stalled():
    """The write runs on a thread and holds the lock only as long as it writes.

    Written on the loop, it would hold SQLite's write lock across every await,
    so a loop stalled for longer than other writers' busy timeout locked them
    out (the mail outbox, on the test droplet). Here the loop is stalled on
    purpose, and the row must land meanwhile.
    """
    from core.database import DATABASE_URL

    row = {"node_id": "ret0badcafe", "node_ref": "ndedddddddd0004", "first_seen_at": T0.isoformat()}
    task = asyncio.create_task(registry_mirror.apply({"upserts": {"nodes": [row]}}))
    await asyncio.sleep(0)

    reader = sqlite3.connect(DATABASE_URL.split("///", 1)[1], timeout=0)
    landed = False
    deadline = time.monotonic() + 5
    while not landed and time.monotonic() < deadline:
        time.sleep(0.05)  # Blocks the loop, as a long analytics pass does.
        landed = reader.execute("SELECT 1 FROM nodes WHERE node_id = 'ret0badcafe'").fetchone() is not None
    reader.close()

    assert landed
    assert (await task)["written"] == {"nodes": 1}


# ── When the owner took the node ─────────────────────────────────────────────


async def test_an_owner_the_node_comes_back_to_starts_from_its_return():
    """Ada took the node here at T0, so its start names her. Production then
    moves it to Bob and back, writing the owner and updated_at but no start,
    which its node_claims does not have. Bob's time with it is not hers: her
    start is production's write that handed it back."""
    ada = str((await get_or_create_magic_link_user("ada@example.com")).id)
    await own_registered_node("ret1a2b3c4d", ada, since=T0)
    back = T0 + timedelta(days=10)

    for owner, stamped in (("bob@example.com", T0 + timedelta(days=3)), ("ada@example.com", back)):
        row = {"node_id": "ret1a2b3c4d", "email": None, "verified": False, "undeliverable": False}
        row |= {"updated_at": stamped.isoformat(), "owner_email": owner}
        await registry_mirror.apply({"upserts": {"node_claims": [row]}, "seq": 0, "last": True})

    assert (await get_user_tenures(ada))["ret1a2b3c4d"] == back


# ── The sender ───────────────────────────────────────────────────────────────


def test_the_sender_arms_on_the_detection_mirrors_targets():
    env = {
        "DETECTION_MIRROR_URL": "https://test-api.example,https://staging-api.example",
        "DETECTION_MIRROR_KEY": "a,b",
    }
    assert registry_mirror.configure_from_env(env)
    assert [(t.label, t.key) for t in registry_mirror._targets] == [
        ("test-api.example", "a"),
        ("staging-api.example", "b"),
    ]
    assert not registry_mirror.configure_from_env({**env, "REGISTRY_MIRROR": "off"})
    assert not registry_mirror.configure_from_env({})


async def test_a_failed_chunk_keeps_the_target_where_it_was(node_session, monkeypatch):
    whole = registry_mirror.chunk_push
    monkeypatch.setattr(registry_mirror, "chunk_push", lambda push: whole(push, limit=600))
    await _seed_source(node_session)
    registry = await registry_mirror.read_registry(node_session)
    target = _target()
    calls = []

    def _handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200 if len(calls) == 1 else 503, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        ok = await registry_mirror.push_to_target(client, target, registry, now=0.0)

    assert not ok
    assert len(calls) == 2, "the first chunk landed, the second did not"
    assert target.accepted == {} and target.full_at is None and not target.healthy


async def test_a_push_that_lands_records_what_the_target_holds(node_session):
    await _seed_source(node_session)
    registry = await registry_mirror.read_registry(node_session)
    target = _target()
    seen = []

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == KEY
        assert request.url.path == registry_mirror.PATH
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        assert await registry_mirror.push_to_target(client, target, registry, now=100.0)
        assert await registry_mirror.push_to_target(client, target, registry, now=200.0)

    assert len(seen) == 1, "nothing changed, so the second minute sends nothing"
    assert target.full_at == 100.0
    assert target.stats()["rows_held"] == sum(len(rows) for rows in registry.values())


# ── The receiving route ──────────────────────────────────────────────────────


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(registry_sync.router)
    return app


@pytest.mark.parametrize("env", ["test", "staging", "dev"])
def test_a_receiving_environment_mounts_the_route(env):
    assert registry_sync.receiving_enabled({"RETINA_ENV": env})


@pytest.mark.parametrize("env", ["production", "prod", "", None])
def test_production_never_mounts_it(env):
    assert not registry_sync.receiving_enabled({} if env is None else {"RETINA_ENV": env})


@pytest.mark.parametrize("server_key, sent", [("", ""), ("", "anything"), (KEY, ""), (KEY, "wrong")])
def test_without_the_key_the_route_answers_like_an_unknown_one(monkeypatch, server_key, sent):
    monkeypatch.setenv("RADAR_API_KEY", server_key)
    with TestClient(_app()) as client:
        response = client.post(registry_mirror.PATH, json={"upserts": {}}, headers={"X-API-Key": sent})
    assert response.status_code == 404


def test_with_the_key_the_route_applies_the_chunk(monkeypatch):
    monkeypatch.setenv("RADAR_API_KEY", KEY)
    row = {
        "node_id": "ret0badcafe",
        "node_ref": "ndedddddddd0004",
        "first_seen_at": (T0 - timedelta(days=1)).isoformat(),
    }
    with TestClient(_app()) as client:
        response = client.post(registry_mirror.PATH, json={"upserts": {"nodes": [row]}}, headers={"X-API-Key": KEY})
    assert response.status_code == 200
    assert response.json()["written"] == {"nodes": 1}


# ── Sign-in sessions ─────────────────────────────────────────────────────────


def _session_row(token: str, owner: str = OWNER, **kw) -> dict:
    """A user_sessions row as production sends it: the owner as an address, the token as its hash."""
    row = {
        "id": str(uuid.uuid4()),
        "token_hash": hashlib.sha256(token.encode()).hexdigest(),
        "created_at": T0.isoformat(),
        "last_used_at": T0.isoformat(),
        "expires_at": (T0 + timedelta(days=7)).isoformat(),
        "user_agent": "Firefox on Windows",
        "ip": None,
        "owner_email": owner,
    }
    row.update(kw)
    return row


async def test_a_mirrored_session_signs_its_cookie_in_here():
    """Ada signed in on production. Its push lands her session under the
    receiver's account at her address, so the same cookie signs her in here."""
    token = "a-token-production-minted"
    landed = await registry_mirror.apply({"upserts": {"user_sessions": [_session_row(token)]}, "seq": 0, "last": True})

    assert landed["written"] == {"user_sessions": 1}
    resolved = await sessions.resolve(token, now=T0 + timedelta(hours=1))
    assert resolved is not None
    assert resolved.user.email == OWNER
    async with async_session_maker() as session:
        account = await session.scalar(select(User).where(User.email == OWNER))
    assert resolved.session.user_id == str(account.id)


async def test_a_session_of_an_account_closed_here_is_skipped():
    async with async_session_maker() as session:
        session.add(User(email=OWNER, hashed_password="x", is_active=False, is_verified=True))
        await session.commit()

    landed = await registry_mirror.apply({"upserts": {"user_sessions": [_session_row("t")]}, "seq": 0, "last": True})

    assert landed["written"] == {}
    assert landed["skipped"] == {"user_sessions": 1}
    assert await sessions.resolve("t", now=T0) is None


async def test_a_full_push_keeps_the_sessions_this_environment_opened_and_drops_productions_ended_ones():
    mine = await get_or_create_magic_link_user("local@example.com")
    local_token, _ = await sessions.open_session(mine, user_agent=None, ip=None, now=T0)
    first, second = "production-one", "production-two"
    rows = [_session_row(first), _session_row(second)]
    keys = [registry_mirror.key_of(registry_mirror.TABLES[-1], row) for row in rows]
    await registry_mirror.apply(
        {"upserts": {"user_sessions": rows}, "full_keys": {"user_sessions": keys}, "seq": 0, "last": True}
    )

    # Production ended the first session; its next full push no longer lists it.
    await registry_mirror.apply({"upserts": {}, "full_keys": {"user_sessions": keys[1:]}, "seq": 0, "last": True})

    later = T0 + timedelta(hours=1)
    assert await sessions.resolve(first, now=later) is None
    assert (await sessions.resolve(second, now=later)).user.email == OWNER
    assert (await sessions.resolve(local_token, now=later)).user.id == mine.id


async def test_the_sender_reads_sessions_with_their_owner_as_an_address():
    ada = await get_or_create_magic_link_user(OWNER)
    token, row = await sessions.open_session(
        ada, user_agent="Mozilla/5.0 (X11; Linux x86_64) Firefox/131.0", ip=None, now=T0
    )

    async with async_session_maker() as session:
        registry = await registry_mirror.read_registry(session)

    key = registry_mirror.key_of(registry_mirror.TABLES[-1], {"token_hash": hashlib.sha256(token.encode()).hexdigest()})
    sent = registry["user_sessions"][key]
    assert sent["owner_email"] == OWNER
    assert "user_id" not in sent
    assert sent["id"] == row.id
    assert token not in json.dumps(sent)
