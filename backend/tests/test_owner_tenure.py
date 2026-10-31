"""A node that changes hands keeps what it recorded for its last owner.

Ada runs a private node at home: she reconfigures it, names it, makes it
public, it reports an error, and its detections fill the archive. Then it
becomes Bob's, by each of the three ways a node changes hands: a release and
Bob's own claim link, a release and an administrator's assignment, or an
administrator moving it from her to him. Every owner route shows Bob the node's
records from when it became his and nothing before (core/auth.py,
get_user_tenures): the configuration history, the events, the heartbeat report
and the errors it keeps, the archive listing and its files, and the SNR trend.
The label's own rule, that it is its owner's alone, is tests/test_owner_routes.py's.

Signed in for real throughout (tests/owner_helpers.py), with the routes and
writers production runs, and the archive in a per-test directory.
"""

import asyncio
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import orjson
import pytest
from sqlalchemy import delete, update

from core.nodes import Node, NodeClaim, NodeReport
from core.users import async_session_maker
from routes.node_schemas import HeartbeatRequest
from services import claim_links, node_config, node_refs, publication, storage
from services import snr_series as ss
from services.node_claim_store import put_challenge, set_claim_address
from services.node_claiming import ClaimOutcome, complete_claim
from services.node_config_store import upsert_config
from services.node_report_store import record_report
from services.parquet_writer import write_detections_parquet
from tests.owner_helpers import as_user, real_sessions, sign_in, user_id_of
from tests.ownership_helpers import own, register_node_row, set_node_owner

NODE = "ret0a1b2c3d"
UNKNOWN = "ret0123abcd"
ADA, BOB = "ada@example.com", "bob@example.com"

_BASE = {
    "rx_alt_ft": 120.0,
    "tx_lat": 51.37,
    "tx_lon": -0.88,
    "tx_alt_ft": 900.0,
    "tx_callsign": "Crystal Palace",
    "fc_hz": 570_000_000.0,
    "fs_hz": 2_000_000.0,
    "beam_width_deg": 41.0,
    "beam_azimuth_deg": None,
    "max_range_km": 50.0,
    "cpi_s": 0.5,
    "delay_tolerance_us": 6.67,
    "doppler_tolerance_hz": 5.0,
}
ADA_HOME = {**_BASE, "rx_lat": 51.501234, "rx_lon": -0.141234}
BOB_HOME = {**_BASE, "rx_lat": 53.480001, "rx_lon": -2.240001}

ADA_ERROR = "sdr at the loft not found"
NOT_FOUND = b'{"detail":"Node not found"}'
NO_FILE = b'{"detail":"Archive file not found"}'

HANDOVERS = ("release then claim link", "release then assignment", "transfer")


# ── Fixtures and helpers ─────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def archive_dir(tmp_path, monkeypatch):
    base = tmp_path / "archive"
    base.mkdir()
    monkeypatch.setattr(storage, "_LOCAL_ARCHIVE_DIR", str(base))
    return base


@pytest.fixture(autouse=True)
def _fresh_snr_cache():
    ss._reset_for_tests()
    yield
    ss._reset_for_tests()


@pytest.fixture(autouse=True)
def _no_reports_left():
    """conftest's _clean_db leaves node_reports, whose key to nodes would fail
    the next test's cleanup."""
    yield

    async def _delete():
        async with async_session_maker() as session:
            await session.execute(delete(NodeReport))
            await session.commit()

    asyncio.run(_delete())


@pytest.fixture
def people(client, monkeypatch):
    """Ada and Bob, signed in for real: each a (token, id)."""
    real_sessions(monkeypatch)
    tokens = {email: sign_in(client, email) for email in (ADA, BOB)}
    ada, bob = (SimpleNamespace(token=tokens[email], id=user_id_of(email)) for email in (ADA, BOB))
    return SimpleNamespace(ada=ada, bob=bob)


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


async def _configure(cfg: dict) -> None:
    config = node_config.validate_config(cfg)
    async with async_session_maker() as session:
        async with session.begin():
            await upsert_config(session, NODE, config)


async def _beat(now: datetime, errors: list[str]) -> None:
    beat = {"state": "streaming", "uptime_s": 60, "boot_id": "b00t0001", "config_version": None, "errors": errors}
    async with async_session_maker() as session:
        async with session.begin():
            await record_report(session, NODE, HeartbeatRequest.model_validate(beat), now)


def _write(archive_dir, write_ts: datetime, frames: list[tuple[datetime, float]]) -> str:
    """One archive file of NODE's, its frames marked by their delay."""
    rows = [{"timestamp": _ms(at), "delay": [marker], "doppler": [12.5], "snr": [marker]} for at, marker in frames]
    key = write_detections_parquet(
        node_id=NODE, frames=rows, base_dir=archive_dir, write_ts=write_ts, node_cfg=ADA_HOME
    )
    assert key is not None
    return key


async def _offer_link(email: str) -> str:
    """The node offering `email`, as its own claim route records it, and the link mailed there."""
    challenge = await claim_links.issue(email=email, node_id=NODE, now=time.time())
    async with async_session_maker() as session:
        async with session.begin():
            await set_claim_address(session, NODE, email)
            await put_challenge(session, NODE, email, challenge.handle, challenge.expires_at)
    return challenge.token


def _hand_over(client, people, how: str) -> None:
    if how == "transfer":
        asyncio.run(set_node_owner(NODE, people.bob.id))
        return
    as_user(client, people.ada.token)
    assert client.delete(f"/api/auth/me/nodes/{NODE}/claim").status_code == 200
    if how == "release then assignment":
        asyncio.run(set_node_owner(NODE, people.bob.id))
        return
    outcome, user, _ref = asyncio.run(complete_claim(asyncio.run(_offer_link(BOB))))
    assert outcome == ClaimOutcome.BOUND and str(user.id) == people.bob.id


def _published(key: str, ref: str) -> str:
    return key.replace(f"node_id={NODE}", f"node_ref={ref}")


def _ref() -> str:
    async def _go():
        async with async_session_maker() as session:
            return (await session.get(Node, NODE)).node_ref

    return asyncio.run(_go())


def _until_after(when: datetime) -> None:
    """Return once the wall clock has passed `when`, so a frame stamped then is in a window ending now."""
    while datetime.now(UTC) <= when:
        pass


@pytest.fixture(params=HANDOVERS)
def handed_over(request, client, people, archive_dir):
    """Ada's time with the node, then Bob's from `before`..`after` on, Bob signed in.

    What Ada's time leaves: configuration versions 1 and 2 at her home (1
    superseded while she held it, 2 the one in force when it changed hands), a
    rename and a publication choice of hers, a beat carrying an error, and an
    archive file of her detections.
    """
    ada_time = datetime.now(UTC) - timedelta(hours=1)
    asyncio.run(register_node_row(NODE, "private"))
    own(NODE, people.ada.id)
    asyncio.run(_configure({**ADA_HOME, "fc_hz": 569_000_000.0}))
    asyncio.run(_configure(ADA_HOME))
    asyncio.run(_beat(ada_time, [ADA_ERROR]))
    ada_key = _write(archive_dir, ada_time, [(ada_time, 1.0)])
    node_refs._reset_for_tests()
    publication._reset_for_tests()
    as_user(client, people.ada.token)
    assert client.patch(f"/api/auth/me/nodes/{NODE}", json={"location_private": False}).status_code == 200
    assert client.patch(f"/api/auth/me/nodes/{NODE}", json={"name": "Ada's loft"}).status_code == 200

    before = datetime.now(UTC)
    _hand_over(client, people, request.param)
    after = datetime.now(UTC)
    node_refs._reset_for_tests()
    publication._reset_for_tests()
    as_user(client, people.bob.token)
    return SimpleNamespace(before=before, after=after, ada_time=ada_time, ada_key=ada_key, ref=_ref(), **vars(people))


def _same_404(*responses, body: bytes) -> None:
    first = responses[0]
    for r in responses:
        assert (r.status_code, r.content) == (404, body)
        assert dict(r.headers) == dict(first.headers)


# ── Configuration history ────────────────────────────────────────────────────


def test_the_history_starts_with_the_version_in_force_when_the_node_changed_hands(client, handed_over):
    """Version 2 is what the board in Bob's hands runs, and his node page
    shows its geometry anyway; version 1 was retired in Ada's time."""
    history = client.get(f"/api/auth/me/nodes/{NODE}/config-history").json()["versions"]

    assert [(v["version"], v["superseded_at"]) for v in history] == [(2, None)]


def test_once_reconfigured_none_of_the_last_owners_versions_is_shown(client, handed_over):
    asyncio.run(_configure(BOB_HOME))
    asyncio.run(_configure({**BOB_HOME, "fc_hz": 571_000_000.0}))

    r = client.get(f"/api/auth/me/nodes/{NODE}/config-history")

    assert [v["version"] for v in r.json()["versions"]] == [4, 3]
    assert str(ADA_HOME["rx_lat"]).encode() not in r.content
    assert str(ADA_HOME["rx_lon"]).encode() not in r.content


# ── Events ───────────────────────────────────────────────────────────────────


def test_the_events_are_the_new_owners_alone(client, handed_over):
    assert client.get(f"/api/auth/me/nodes/{NODE}/events").json() == {"events": []}

    assert client.patch(f"/api/auth/me/nodes/{NODE}", json={"name": "Bob's shed"}).status_code == 200
    r = client.get(f"/api/auth/me/nodes/{NODE}/events")

    assert [(e["kind"], e["actor"]) for e in r.json()["events"]] == [("renamed", "you")]
    assert handed_over.ada.id.encode() not in r.content


# ── The heartbeat report ─────────────────────────────────────────────────────


def test_the_report_is_null_until_the_node_beats_for_its_new_owner(client, handed_over):
    """The beat on file is from Ada's time: it describes the node as she ran it."""
    r = client.get(f"/api/auth/me/nodes/{NODE}/report")
    assert (r.status_code, r.content) == (200, b"null")


def test_the_errors_the_report_keeps_are_the_new_owners_alone(client, handed_over):
    """The row keeps errors across beats, Ada's among them."""
    asyncio.run(_beat(datetime.now(UTC), ["sdr overrun"]))

    report = client.get(f"/api/auth/me/nodes/{NODE}/report").json()

    assert [e["message"] for e in report["errors"]] == ["sdr overrun"]
    assert ADA_ERROR not in orjson.dumps(report).decode()


# ── The archive ──────────────────────────────────────────────────────────────


def test_the_listing_leaves_out_what_was_written_before(client, handed_over, archive_dir):
    bobs = _write(archive_dir, handed_over.after + timedelta(seconds=2), [(handed_over.after, 2.0)])
    day = handed_over.ada_time.strftime("%Y/%m/%d")

    for params in ({}, {"node_ref": handed_over.ref}, {"date": day}):
        listing = client.get("/api/auth/me/archive", params=params).json()
        keys = [f["key"] for f in listing["files"]]
        assert _published(handed_over.ada_key, handed_over.ref) not in keys, params
    listing = client.get("/api/auth/me/archive").json()
    assert [f["key"] for f in listing["files"]] == [_published(bobs, handed_over.ref)]
    assert listing["count"] == listing["total"] == 1


def test_a_file_from_before_is_404_as_an_unknown_one_is(client, handed_over):
    ada_file = client.get(f"/api/auth/me/archive/{_published(handed_over.ada_key, handed_over.ref)}")
    unknown = client.get(f"/api/auth/me/archive/{_published(handed_over.ada_key, 'nde0123abcd99')}")

    _same_404(ada_file, unknown, body=NO_FILE)


def test_a_file_written_since_is_given_without_the_frames_from_before(client, handed_over, archive_dir):
    """A flush writes what arrived since the last one, so the first file of
    Bob's time can still hold frames of Ada's."""
    key = _write(
        archive_dir,
        handed_over.after + timedelta(seconds=2),
        [(handed_over.before - timedelta(milliseconds=1), 3.0), (handed_over.after + timedelta(milliseconds=1), 4.0)],
    )

    body = client.get(f"/api/auth/me/archive/{_published(key, handed_over.ref)}").json()

    assert [frame["delay"] for frame in body["detections"]] == [[4.0]]
    assert body["count"] == 1


# ── SNR ──────────────────────────────────────────────────────────────────────


def test_the_snr_trend_starts_when_the_node_became_the_new_owners(client, handed_over, archive_dir):
    bobs = handed_over.after + timedelta(milliseconds=1)
    _write(archive_dir, datetime.now(UTC), [(handed_over.before - timedelta(milliseconds=1), 50.0), (bobs, 7.0)])
    _until_after(bobs)

    series = client.get(f"/api/auth/me/nodes/{NODE}/snr", params={"hours": 24}).json()

    assert [(p["count"], p["median_db"]) for p in series["points"]] == [(1, 7.0)]
    assert series["through_ms"] == _ms(bobs)


def test_a_trend_read_for_the_last_owner_is_not_served_to_the_next(client, people, archive_dir):
    """The answer is cached for a minute, keyed on the tenure as well as the
    node and window, so Ada's cannot be handed to Bob within that minute."""
    asyncio.run(register_node_row(NODE, "private"))
    own(NODE, people.ada.id, since=datetime.now(UTC) - timedelta(hours=2))
    detected = datetime.now(UTC) - timedelta(hours=1)
    _write(archive_dir, detected, [(detected, 1.0)])
    as_user(client, people.ada.token)
    assert [p["count"] for p in client.get(f"/api/auth/me/nodes/{NODE}/snr").json()["points"]] == [1]

    asyncio.run(set_node_owner(NODE, people.bob.id))
    as_user(client, people.bob.token)

    assert client.get(f"/api/auth/me/nodes/{NODE}/snr").json()["points"] == []


# ── When a tenure starts ─────────────────────────────────────────────────────


def test_each_way_of_changing_hands_records_when_and_for_whom(handed_over):
    async def _claim():
        async with async_session_maker() as session:
            return await session.get(NodeClaim, NODE)

    claim = asyncio.run(_claim())

    assert claim.user_id == claim.owned_since_user_id == handed_over.bob.id
    assert handed_over.before <= claim.owned_since <= handed_over.after


def test_the_previous_owner_is_answered_as_about_an_unknown_node(client, handed_over):
    as_user(client, handed_over.ada.token)
    for path in ("config-history", "events", "report", "snr"):
        _same_404(
            client.get(f"/api/auth/me/nodes/{NODE}/{path}"),
            client.get(f"/api/auth/me/nodes/{UNKNOWN}/{path}"),
            body=NOT_FOUND,
        )


def test_assigning_an_owner_the_node_they_have_keeps_their_history(client, people):
    asyncio.run(register_node_row(NODE, "private"))
    own(NODE, people.ada.id)
    as_user(client, people.ada.token)
    assert client.patch(f"/api/auth/me/nodes/{NODE}", json={"name": "Ada's loft"}).status_code == 200

    asyncio.run(set_node_owner(NODE, people.ada.id))

    events = client.get(f"/api/auth/me/nodes/{NODE}/events").json()["events"]
    assert [(e["kind"], e["actor"]) for e in events] == [("renamed", "you")]


def test_a_write_that_keeps_the_owner_does_not_hide_their_history(client, people):
    """A second link for the owner's own address, as a resend racing the first
    click leaves, finds her binding, confirms the address again and stamps the
    row's updated_at. That is no change of hands, and what she did in between
    is still hers."""
    asyncio.run(register_node_row(NODE, "private"))
    assert asyncio.run(complete_claim(asyncio.run(_offer_link(ADA))))[0] == ClaimOutcome.BOUND
    as_user(client, people.ada.token)
    assert client.patch(f"/api/auth/me/nodes/{NODE}", json={"name": "Ada's loft"}).status_code == 200

    assert asyncio.run(complete_claim(asyncio.run(_offer_link(ADA))))[0] == ClaimOutcome.BOUND

    events = client.get(f"/api/auth/me/nodes/{NODE}/events").json()["events"]
    assert [(e["kind"], e["actor"]) for e in events] == [("renamed", "you")]


def test_an_owner_set_by_code_that_predates_the_tenure_starts_when_it_was_set(client, people):
    """An image from before the tenure columns, serving after a rollback,
    moves the owner as set_owner did then: user_id and updated_at, the start
    left as Ada's. Bob's history starts at that write, not at hers."""
    asyncio.run(register_node_row(NODE, "private"))
    own(NODE, people.ada.id)
    as_user(client, people.ada.token)
    assert client.patch(f"/api/auth/me/nodes/{NODE}", json={"name": "Ada's loft"}).status_code == 200

    async def _old_set_owner():
        async with async_session_maker() as session:
            async with session.begin():
                await session.execute(
                    update(NodeClaim)
                    .where(NodeClaim.node_id == NODE)
                    .values(
                        user_id=people.bob.id,
                        email=None,
                        verified=False,
                        undeliverable=False,
                        updated_at=datetime.now(UTC),
                    )
                )

    asyncio.run(_old_set_owner())
    as_user(client, people.bob.token)

    assert client.get(f"/api/auth/me/nodes/{NODE}/events").json() == {"events": []}
