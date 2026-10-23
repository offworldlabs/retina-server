"""services/adsb_store.py: which archived ADS-B fixes a window serves, and what it reads to find them.

Parts are written through the writers' own batch publisher in the archive's
schema, into a per-test root that services.adsb_writer.ADSB_DIR is pointed at.
Node rows with minted refs stand behind the ids, so the privacy rules run
against the real policy and identity caches. Rows are told apart by lat.

Days compacted by services/compaction.py are read through their compact and
the raw parts it does not list, and a day whose compact lives only in object
storage (moto's S3, set up as tests/test_cold_store.py sets it up) through
services/cold_store.py; tests/test_adsb_archive_route.py holds the whole-day
answers before and after.
"""

import asyncio
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

import boto3
import pyarrow as pa
import pytest
from moto import mock_aws
from sqlalchemy import update

from core import state
from core.nodes import Node
from core.users import async_session_maker
from services import adsb_store, adsb_writer, cold_store, compaction, node_auth, node_refs, publication, tracks_store
from services.compaction import ADSB, COMPACT_NAME, PARQUET_CONTENT_TYPE
from services.parquet_io import write_parquet_batch
from services.tasks import adsb_snapshot
from tests.object_store_env import FAKE_AWS_ENV, store_env

PUB, PUB_B = "ret1a2b3c4d", "ret2b3c4d5e"
PRIV = "ret3a4b5c6d"
# A real id with no registration: it has no published identity.
NO_REF = "ret5e6f7a8b"
SYN = "synth-alpha"

PUBLISHED_KEYS = {"ts_ms", "hex", "lat", "lon", "alt_baro", "gs", "track", "flight", "seen_ms"}


def at(hour: int, minute: int = 0, second: int = 0, ms: int = 0, day: int = 18) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, ms * 1000, tzinfo=UTC)


def ms_of(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def seed() -> dict[str, str]:
    """PUB and PUB_B registered public and PRIV private, each with a minted ref; the refs."""
    nodes = {PUB: "public", PUB_B: "public", PRIV: "private"}
    refs = {node_id: node_auth.mint_node_ref() for node_id in nodes}

    async def _go():
        async with async_session_maker() as session:
            for node_id, choice in nodes.items():
                session.add(Node(node_id=node_id, node_ref=refs[node_id], publication=choice))
            await session.commit()

    asyncio.run(_go())
    publication._reset_for_tests()
    node_refs._reset_for_tests()
    return refs


def fix(
    ts: datetime,
    hex_: str = "a1b2c3",
    *,
    lat: float = 34.85,
    lon: float = -82.39,
    world: str = "real",
    source: str = "node",
    node: str | None = PUB,
    seen: datetime | None = None,
    **extra,
) -> dict:
    """One archived fix; ``node`` is its source_node_id, comma-joined for ADSBHub."""
    return {
        "ts_ms": ms_of(ts),
        "hex": hex_,
        "lat": lat,
        "lon": lon,
        "alt_baro": 35000,
        "gs": 450.0,
        "track": 270.0,
        "flight": "DAL1234",
        "seen_ms": ms_of(seen if seen is not None else ts) - 500,
        "world": world,
        "source": source,
        "source_node_id": node,
        **extra,
    }


def write_part(root: Path, flushed: datetime, rows: list[dict], groups: list[int] | None = None) -> Path:
    """A part as the writer lays it out: the flush day's partition, rows sorted by ts_ms, one row group.

    ``groups`` splits the sorted rows into row groups of those sizes instead.
    """
    table = pa.Table.from_pylist(sorted(rows, key=lambda row: row["ts_ms"]), schema=adsb_writer.SCHEMA)
    directory = root / f"year={flushed:%Y}" / f"month={flushed:%m}" / f"day={flushed:%d}"
    return directory / write_parquet_batch(table, directory, flushed, row_group_sizes=groups)


D18, D19 = date(2026, 10, 18), date(2026, 10, 19)
# 2026-10-20 00:30Z: both days ended, and their settle margin passed.
COMPACTED_AT = 1_792_456_200_000


def partition(root: Path, day: date) -> Path:
    return Path(compaction.day_dir(str(root), day))


def compact(root: Path, *days: date) -> None:
    for day in days:
        assert compaction.compact_day(ADSB, str(root), day, now_ms=COMPACTED_AT) == "compacted"


@contextmanager
def configured_object_store(tmp_path: Path, monkeypatch) -> Iterator[list[str]]:
    """A configured object store with one empty bucket, an empty cold cache, and the keys downloaded from it."""
    monkeypatch.setattr(cold_store, "_CACHE_DIR", str(tmp_path / "cold_cache"))
    cold_store._reset_for_tests()
    client = cold_store._client
    downloads: list[str] = []
    download = client.download_bytes

    def counting(key: str) -> bytes | None:
        downloads.append(key)
        return download(key)

    monkeypatch.setattr(client, "download_bytes", counting)
    with store_env(FAKE_AWS_ENV), mock_aws():
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket="retina-data")
        yield downloads
    cold_store._reset_for_tests()


def to_object_storage(root: Path, day: date) -> str:
    """The day's compact uploaded and deleted locally, as the lifecycle leaves it past its retention; its key."""
    compact_path = partition(root, day) / COMPACT_NAME
    key = compaction.object_key(ADSB, day, COMPACT_NAME)
    assert cold_store.upload_bytes(key, compact_path.read_bytes(), PARQUET_CONTENT_TYPE)
    compact_path.unlink()
    return key


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "adsb"
    monkeypatch.setattr(adsb_writer, "ADSB_DIR", str(path))
    adsb_store._reset_for_tests()
    yield path
    adsb_store._reset_for_tests()


@pytest.fixture
def private():
    seed()
    return publication.private_node_ids()


@pytest.fixture
def object_store(tmp_path, monkeypatch) -> Iterator[list[str]]:
    with configured_object_store(tmp_path, monkeypatch) as downloads:
        yield downloads


@pytest.fixture
def reads(monkeypatch):
    """("footer" | "group", file name) for every raw part's footer read and row group decoded, in order;
    a compact's row groups, read through an open handle, are ("group", "compact")."""
    seen: list[tuple[str, str]] = []
    read_footer, decode = adsb_store._read_footer, adsb_store._decode

    def footer(path):
        seen.append(("footer", Path(path).name))
        return read_footer(path)

    def group(source, index):
        seen.append(("group", Path(source).name if isinstance(source, str) else "compact"))
        return decode(source, index)

    monkeypatch.setattr(adsb_store, "_read_footer", footer)
    monkeypatch.setattr(adsb_store, "_decode", group)
    return seen


def query(private, start: datetime, end: datetime, **kwargs) -> dict:
    return adsb_store.query_adsb(ms_of(start), ms_of(end), private=private, **kwargs)


def lats(body: dict) -> list[float]:
    return [row["lat"] for row in body["rows"]]


# ── Window, fleet, order, cap ────────────────────────────────────────────────


def test_the_window_keeps_its_start_and_leaves_out_its_end(root, private):
    write_part(
        root,
        at(10, 31),
        [
            fix(at(9, 59, 59, 999), lat=1.0),
            fix(at(10), lat=2.0),
            fix(at(10, 29, 59, 999), lat=3.0),
            fix(at(10, 30), lat=4.0),
        ],
    )
    assert lats(query(private, at(10), at(10, 30))) == [2.0, 3.0]


@pytest.mark.parametrize(("fleet", "expected"), [("real", [1.0]), ("synthetic", [2.0]), ("all", [1.0, 2.0])])
def test_the_fleet_picks_the_world(root, private, fleet, expected):
    write_part(root, at(10, 5), [fix(at(10, 1), lat=1.0), fix(at(10, 2), lat=2.0, world="sim", node=SYN)])
    assert lats(query(private, at(10), at(10, 30), fleet=fleet)) == expected


def test_rows_sort_by_time_then_hex(root, private):
    write_part(root, at(10, 5), [fix(at(10, 1), "cccccc"), fix(at(10, 2), "aaaaaa")])
    write_part(root, at(10, 6), [fix(at(10, 1), "bbbbbb"), fix(at(10, 1), "aaaaaa"), fix(at(10, 2), "bbbbbb")])
    body = query(private, at(10), at(10, 30))
    assert [(row["ts_ms"], row["hex"]) for row in body["rows"]] == [
        (ms_of(at(10, 1)), "aaaaaa"),
        (ms_of(at(10, 1)), "bbbbbb"),
        (ms_of(at(10, 1)), "cccccc"),
        (ms_of(at(10, 2)), "aaaaaa"),
        (ms_of(at(10, 2)), "bbbbbb"),
    ]


def test_past_the_cap_the_first_rows_go_out_and_truncated_says_so(root, private, monkeypatch):
    write_part(root, at(10, 10), [fix(at(10, minute), lat=float(minute)) for minute in range(5)])
    monkeypatch.setattr(adsb_store, "ADSB_ROW_CAP", 3)
    body = query(private, at(10), at(10, 30))
    assert (body["count"], body["truncated"], lats(body)) == (3, True, [0.0, 1.0, 2.0])
    monkeypatch.setattr(adsb_store, "ADSB_ROW_CAP", 5)
    body = query(private, at(10), at(10, 30))
    assert (body["count"], body["truncated"]) == (5, False)


def test_rows_carry_only_the_published_columns_as_plain_values(root, private):
    write_part(
        root,
        at(10, 5),
        [
            fix(at(10, 1), "abc123", lat=34.5, lon=-82.25),
            fix(at(10, 2), "abc123", lat=34.6, alt_baro=None, gs=float("nan"), track=None, flight=None),
        ],
    )
    rows = query(private, at(10), at(10, 30))["rows"]
    assert rows == [
        {
            "ts_ms": ms_of(at(10, 1)),
            "hex": "abc123",
            "lat": 34.5,
            "lon": -82.25,
            "alt_baro": 35000,
            "gs": 450.0,
            "track": 270.0,
            "flight": "DAL1234",
            "seen_ms": ms_of(at(10, 0, 59)),
        },
        {
            "ts_ms": ms_of(at(10, 2)),
            "hex": "abc123",
            "lat": 34.6,
            "lon": -82.39,
            "alt_baro": None,
            "gs": None,
            "track": None,
            "flight": None,
            "seen_ms": ms_of(at(10, 1, 59)),
        },
    ]
    assert {type(value) for row in rows for value in row.values()} == {int, float, str, type(None)}


def test_seen_ms_goes_out_to_the_whole_second_below_and_never_after_its_snapshot(root, private):
    write_part(
        root,
        at(10, 5),
        [
            # Received 2.75 s before its snapshot.
            fix(at(10, 1), "aaaaaa", seen=at(10, 0, 57, 750)),
            # Stamped 1.5 s after its snapshot by a node whose clock runs ahead.
            fix(at(10, 1, 0, 400), "bbbbbb", seen=at(10, 1, 2, 400)),
        ],
    )
    # A part with no capture time at all: received by its snapshot.
    no_seen = adsb_writer.SCHEMA.remove(adsb_writer.SCHEMA.get_field_index("seen_ms"))
    row = {k: v for k, v in fix(at(10, 2, 3, 700), "cccccc").items() if k != "seen_ms"}
    directory = root / "year=2026" / "month=10" / "day=18"
    write_parquet_batch(pa.Table.from_pylist([row], schema=no_seen), directory, at(10, 6))
    body = query(private, at(10), at(10, 30))
    assert [(row["hex"], row["seen_ms"]) for row in body["rows"]] == [
        ("aaaaaa", ms_of(at(10, 0, 57))),
        ("bbbbbb", ms_of(at(10, 1))),
        ("cccccc", ms_of(at(10, 2, 3))),
    ]


# ── Who a fix is attributed to ───────────────────────────────────────────────


def test_a_private_nodes_own_fix_is_dropped(root, private):
    write_part(root, at(10, 5), [fix(at(10, 1), "aaaaaa", lat=1.0), fix(at(10, 1), "bbbbbb", lat=2.0, node=PRIV)])
    assert lats(query(private, at(10), at(10, 30))) == [1.0]


def test_a_real_node_without_a_published_identity_is_dropped(root, private):
    write_part(root, at(10, 5), [fix(at(10, 1), "aaaaaa", lat=1.0), fix(at(10, 1), "bbbbbb", lat=2.0, node=NO_REF)])
    assert lats(query(private, at(10), at(10, 30))) == [1.0]


def test_a_synthetic_nodes_own_fix_is_kept(root, private):
    write_part(root, at(10, 5), [fix(at(10, 1), lat=1.0, world="sim", node=SYN)])
    assert lats(query(private, at(10), at(10, 30), fleet="synthetic")) == [1.0]


def test_a_fix_with_no_node_is_kept_only_in_the_simulated_world(root, private):
    write_part(
        root,
        at(10, 5),
        [fix(at(10, 1), "aaaaaa", lat=1.0, world="sim", node=None), fix(at(10, 1), "bbbbbb", lat=2.0, node=None)],
    )
    assert lats(query(private, at(10), at(10, 30), fleet="all")) == [1.0]


def test_an_adsbhub_fix_is_kept_while_one_of_its_nodes_may_be_published(root, private, caplog):
    def hub(hex_, lat, node):
        return fix(at(10, 1), hex_, lat=lat, source="aggregator", node=node)

    write_part(
        root,
        at(10, 5),
        [
            hub("aaaaaa", 1.0, f"{PRIV},{PUB}"),
            hub("bbbbbb", 2.0, PRIV),
            hub("cccccc", 3.0, f"{PRIV},{NO_REF}"),
            hub("dddddd", 4.0, None),
            hub("eeeeee", 5.0, ""),
        ],
    )
    assert lats(query(private, at(10), at(10, 30))) == [1.0]
    # Its nodes are looked up one by one: no list of them is logged as a node with no identity.
    assert f"{PRIV},{PUB}" not in caplog.text
    assert f"{PRIV},{NO_REF}" not in caplog.text


def test_a_fix_from_an_unknown_source_is_dropped(root, private):
    write_part(root, at(10, 5), [fix(at(10, 1), "aaaaaa", lat=1.0), fix(at(10, 1), "bbbbbb", lat=2.0, source="relay")])
    assert lats(query(private, at(10), at(10, 30))) == [1.0]


def test_one_fix_per_aircraft_is_picked_from_those_that_may_be_published(root, private):
    """A withheld node's fresher fix never hides the public fix of the same aircraft and snapshot."""
    fresh, stale = at(10, 0, 59, 900), at(10, 0, 59)
    write_part(
        root,
        at(10, 5),
        [
            # Seen fresher by a private node, and by ADSBHub over a public node's coverage.
            fix(at(10, 1), "aaaaaa", lat=1.0, node=PRIV, seen=fresh),
            fix(at(10, 1), "aaaaaa", lat=2.0, source="aggregator", node=PUB, seen=stale),
            # Seen fresher by a public node.
            fix(at(10, 1), "bbbbbb", lat=3.0, seen=fresh),
            fix(at(10, 1), "bbbbbb", lat=4.0, source="aggregator", node=PUB, seen=stale),
            # Ties: a node's own fix before ADSBHub's, then the smaller node id.
            fix(at(10, 1), "cccccc", lat=6.0, source="aggregator", node=PUB, seen=stale),
            fix(at(10, 1), "cccccc", lat=5.0, seen=stale),
            fix(at(10, 1), "dddddd", lat=8.0, node=PUB_B, seen=stale),
            fix(at(10, 1), "dddddd", lat=7.0, node=PUB, seen=stale),
        ],
    )
    assert lats(query(private, at(10), at(10, 30))) == [2.0, 3.0, 5.0, 7.0]


def test_a_withheld_adsbhub_fix_never_hides_an_older_one_a_public_node_holds(root, private):
    fresh, stale = at(10, 0, 59, 900), at(10, 0, 59)

    def hub(hex_, lat, node, seen):
        return fix(at(10, 1), hex_, lat=lat, source="aggregator", node=node, seen=seen)

    write_part(
        root,
        at(10, 5),
        [
            # Fresher inside a private node's disc only, older inside a public node's.
            hub("aaaaaa", 1.0, PRIV, fresh),
            hub("aaaaaa", 2.0, PUB, stale),
            # Fresher inside another public node's disc, older inside the first's.
            hub("bbbbbb", 3.0, PUB_B, fresh),
            hub("bbbbbb", 4.0, PUB, stale),
        ],
    )
    assert lats(query(private, at(10), at(10, 30))) == [2.0, 3.0]


def test_a_node_fix_is_served_only_while_every_node_it_names_may_be_published(root, private):
    """The writer names one node, the one whose receiver made the fix."""
    write_part(
        root,
        at(10, 5),
        [
            fix(at(10, 1), "aaaaaa", lat=1.0, node=PUB_B),
            fix(at(10, 1), "bbbbbb", lat=2.0, node=","),
            fix(at(10, 1), "cccccc", lat=3.0, node=f"{PRIV},{PUB}"),
            fix(at(10, 1), "dddddd", lat=4.0, node=f"{PUB},{NO_REF}"),
            fix(at(10, 1), "eeeeee", lat=5.0, node=f"{PUB},{PUB_B}"),
        ],
    )
    assert lats(query(private, at(10), at(10, 30))) == [1.0, 5.0]


# ── A node withheld after its fixes were archived ────────────────────────────

# Each node sends a frame a second, at a phase of its own and landing a little
# later, and stamps every aircraft in it with the frame's time.
FRAMES = {PUB: (430, 90), PUB_B: (100, 40)}  # (phase, latency), ms
HEARD_BY = {
    "aaaaa1": (PUB,),
    "aaaaa2": (PUB,),
    "abbbb1": (PUB, PUB_B),
    "abbbb2": (PUB, PUB_B),
    "bbbbb1": (PUB_B,),
    "bbbbb2": (PUB_B,),
}


def archive_reports(reporters: tuple[str, ...], monkeypatch) -> dict[str, list[dict]]:
    """Ten minutes from 10:00 through the snapshot task's writer, with only ``reporters`` reporting.

    Each aircraft flies east, so a fix's position says when it was made.
    Returns each node's own fixes, as the route would serve them, by time then hex.
    """
    monkeypatch.setattr(state, "adsb_fixes_by_reporter", {}, raising=False)
    monkeypatch.setattr(state, "adsb_fallback", {})
    monkeypatch.setattr(state, "connected_nodes", {})
    adsb_snapshot._reset_for_tests()
    start = ms_of(at(10))
    own: dict[str, list[dict]] = {node: [] for node in reporters}
    batch: list[dict] = []
    for k in range(60):
        # The task wakes every 10 s and works a while first, so a snapshot falls anywhere in its second.
        ts = start + k * 10_000 + k * 397 % 1000
        for j, (hex_, heard) in enumerate(HEARD_BY.items()):
            for node in (n for n in heard if n in reporters):
                phase, latency = FRAMES[node]
                seen = (ts - latency - phase) // 1000 * 1000 + phase
                lat, lon = 34.0 + 0.1 * j, -82.8 + 0.002 * (seen - start) / 1000
                state.adsb_fixes_by_reporter.setdefault(hex_, {})[node] = {
                    "flight": "DAL123",
                    "lat": lat,
                    "lon": lon,
                    "alt_baro": 34000,
                    "gs": 450.0,
                    "track": 90.0,
                    "last_seen_ms": seen,
                    "world": "real",
                }
                served = {"lat": lat, "lon": lon, "alt_baro": 34000, "gs": 450.0, "track": 90.0, "flight": "DAL123"}
                own[node].append({"ts_ms": ts, "hex": hex_, **served, "seen_ms": seen // 1000 * 1000})
        batch += adsb_snapshot.snapshot_rows(ts)
        if k % 6 == 5:
            adsb_writer.write_adsb_parquet(rows=batch, base_dir=adsb_writer.ADSB_DIR, write_ts=at(10, k // 6, 59))
            batch = []
    return own


def made_private(node_id: str):
    """Register node_id private, as its owner would; the policy the reader is then handed."""

    async def _go():
        async with async_session_maker() as session:
            await session.execute(update(Node).where(Node.node_id == node_id).values(publication="private"))
            await session.commit()

    asyncio.run(_go())
    publication.invalidate()
    return publication.private_node_ids()


def test_a_node_made_private_after_its_fixes_were_archived_leaves_only_the_other_public_nodes_own_fixes(
    root, private, monkeypatch
):
    own = archive_reports((PUB, PUB_B), monkeypatch)
    # Both public: PUB_B's fix goes out wherever it is the fresher one.
    rows = query(private, at(10), at(10, 30))["rows"]
    assert {r["hex"] for r in rows if r in own[PUB_B] and r not in own[PUB]} == {"abbbb1", "abbbb2", "bbbbb1", "bbbbb2"}
    # PUB_B made private: PUB's own fixes of the aircraft it heard, at every snapshot, and nothing else.
    assert query(made_private(PUB_B), at(10), at(10, 30))["rows"] == own[PUB]


def test_once_a_node_is_withheld_the_answer_is_what_it_would_be_had_the_node_never_reported(
    root, private, monkeypatch, tmp_path
):
    archive_reports((PUB, PUB_B), monkeypatch)
    withheld = query(made_private(PUB_B), at(10), at(10, 30))
    monkeypatch.setattr(adsb_writer, "ADSB_DIR", str(tmp_path / "without"))
    adsb_store._reset_for_tests()
    archive_reports((PUB,), monkeypatch)
    assert withheld == query(private, at(10), at(10, 30))


# ── What a window reads ──────────────────────────────────────────────────────


def test_only_row_groups_that_can_hold_the_window_and_fleet_are_decoded(root, private, reads):
    write_part(root, at(9, 59, 59), [fix(at(9, 59, 50), lat=0.0)])
    inside = write_part(root, at(10, 5), [fix(at(10, 4), lat=1.0)])
    after = write_part(root, at(10, 40), [fix(at(10, 35), lat=2.0)])
    simulated = write_part(root, at(10, 6), [fix(at(10, 5), lat=3.0, world="sim", node=SYN)])
    assert lats(query(private, at(10), at(10, 30))) == [1.0]
    # The part flushed before the window opened is never opened at all.
    assert sorted(name for kind, name in reads if kind == "footer") == sorted([inside.name, after.name, simulated.name])
    assert [name for kind, name in reads if kind == "group"] == [inside.name]


def test_a_row_group_is_decoded_when_it_reaches_the_windows_start_and_not_when_it_starts_at_its_end(
    root, private, reads
):
    part = write_part(
        root,
        at(10, 35),
        [
            # Ends at the window's start.
            fix(at(9, 59), "aaaaaa", lat=1.0),
            fix(at(10), "aaaaaa", lat=2.0),
            # Starts at the window's start.
            fix(at(10), "bbbbbb", lat=3.0),
            fix(at(10, 1), "bbbbbb", lat=4.0),
            # Starts at the window's end.
            fix(at(10, 30), "cccccc", lat=5.0),
            fix(at(10, 31), "cccccc", lat=6.0),
        ],
        groups=[2, 2, 2],
    )
    assert lats(query(private, at(10), at(10, 30))) == [2.0, 3.0, 4.0]
    # The first two groups, each once.
    assert [name for kind, name in reads if kind == "group"] == [part.name, part.name]


def test_a_part_is_listed_while_its_flush_second_can_hold_the_windows_start(root, private, reads):
    """A part named for second S can hold a snapshot taken up to S + 999 ms."""
    write_part(root, at(10, 0, 0, 999), [fix(at(10, 0, 0, 999), lat=1.0)])
    # Opening at S + 1000 ms: the part is not even opened.
    assert lats(query(private, at(10, 0, 1), at(10, 30))) == []
    assert reads == []
    # Opening at S + 999 ms: it is, and its snapshot is served.
    assert lats(query(private, at(10, 0, 0, 999), at(10, 30))) == [1.0]


def test_a_window_reads_the_next_days_partition_only_within_the_slack(root, private, monkeypatch):
    listed: list[date] = []
    day_parts = adsb_store._day_parts

    def recording(day):
        listed.append(day)
        return day_parts(day)

    monkeypatch.setattr(adsb_store, "_day_parts", recording)
    # Snapshotted before midnight, flushed after it.
    write_part(root, at(0, 0, 30, day=19), [fix(at(23, 59, 40), lat=1.0)])
    assert lats(query(private, at(23, 30), at(0, day=19))) == [1.0]
    assert listed == [date(2026, 10, 18), date(2026, 10, 19)]
    listed.clear()
    assert lats(query(private, at(22), at(23))) == []
    assert listed == [date(2026, 10, 18)]


def test_the_cache_keeps_to_its_byte_bound_and_answers_the_same(root, private, reads, monkeypatch):
    write_part(root, at(10, 5), [fix(at(10, 1), lat=1.0)])
    write_part(root, at(10, 6), [fix(at(10, 2), lat=2.0)])
    warm = query(private, at(10), at(10, 30))
    assert lats(warm) == [1.0, 2.0]
    reads.clear()
    assert query(private, at(10), at(10, 30)) == warm
    assert reads == []

    monkeypatch.setattr(tracks_store, "TRACKS_CACHE_MAX_BYTES", 0)
    adsb_store._reset_for_tests()
    assert query(private, at(10), at(10, 30)) == warm
    first = sorted(reads)
    assert len(first) == 4
    reads.clear()
    assert query(private, at(10), at(10, 30)) == warm
    assert sorted(reads) == first


# ── Compacted days ───────────────────────────────────────────────────────────


def test_a_late_part_is_read_beside_the_compact_and_superseded_parts_never_are(root, private, reads, tmp_path):
    first = write_part(
        root, at(10, 5), [fix(at(10, 1), "aaaaaa", lat=1.0), fix(at(10, 2), lat=2.0, world="sim", node=SYN)]
    )
    compact(root, D18)
    late = write_part(root, at(10, 50), [fix(at(10, 45), "bbbbbb", lat=3.0)])
    reads.clear()

    assert lats(query(private, at(10), at(11), fleet="all")) == [1.0, 2.0, 3.0]
    assert [name for kind, name in reads if kind == "footer"] == [late.name]
    assert sorted(name for kind, name in reads if kind == "group") == ["compact", "compact", late.name]

    # A superseded part rewritten with other rows is still never read.
    os.replace(write_part(tmp_path / "elsewhere", at(10, 5), [fix(at(10, 3), "cccccc", lat=9.0)]), first)
    adsb_store._reset_for_tests()
    reads.clear()
    assert lats(query(private, at(10), at(11), fleet="all")) == [1.0, 2.0, 3.0]
    assert first.name not in {name for _, name in reads}


def test_a_day_with_some_listed_parts_left_reads_its_compact_from_object_storage_and_its_late_parts(
    root, private, reads, object_store
):
    kept = write_part(root, at(10, 5), [fix(at(10, 1), "aaaaaa", lat=1.0)])
    gone = write_part(
        root, at(11, 5), [fix(at(11, 1), "aaaaaa", lat=2.0), fix(at(11, 2), lat=3.0, world="sim", node=SYN)]
    )
    compact(root, D18)
    late = write_part(root, at(13, 5), [fix(at(13, 1), "bbbbbb", lat=4.0)])
    whole = query(private, at(10), at(14), fleet="all")
    assert lats(whole) == [1.0, 2.0, 3.0, 4.0]
    # The compact and one listed part leave local disk; the other listed part and the late one stay.
    key = to_object_storage(root, D18)
    gone.unlink()
    adsb_store._reset_for_tests()
    reads.clear()

    for _ in range(2):
        assert query(private, at(10), at(14), fleet="all") == whole
    # The listed part left on disk is never read, the late one is, and the day is fetched once.
    assert {name for kind, name in reads if kind == "footer"} == {late.name}
    assert kept.name not in {name for _, name in reads}
    assert object_store == [key]


def test_once_a_node_is_withheld_a_compacted_day_answers_as_if_it_had_never_reported_from_disk_and_object_storage(
    root, private, monkeypatch, tmp_path, object_store
):
    archive_reports((PUB, PUB_B), monkeypatch)
    # A node fix is attributed to every node it names: this one names PUB_B beside a public node, and is the
    # freshest fix of its aircraft in that snapshot.
    write_part(root, at(10, 0, 30), [fix(at(10), "abbbb1", lat=40.0, node=f"{PUB},{PUB_B}")])
    compact(root, D18)
    for part in adsb_store._day_parts(D18):
        os.unlink(part)
    public = query(private, at(10), at(10, 30))

    withheld = made_private(PUB_B)
    from_compact = query(withheld, at(10), at(10, 30))
    key = to_object_storage(root, D18)
    adsb_store._reset_for_tests()
    from_object_storage = query(withheld, at(10), at(10, 30))
    assert object_store == [key]

    monkeypatch.setattr(adsb_writer, "ADSB_DIR", str(tmp_path / "without"))
    adsb_store._reset_for_tests()
    archive_reports((PUB,), monkeypatch)
    never = query(private, at(10), at(10, 30))
    # While PUB_B was public its fixes went out of the compact, the one naming it with PUB among them.
    assert public != never and 40.0 in lats(public)
    assert from_compact == never
    assert from_object_storage == never
