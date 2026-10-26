"""services/tracks_store.py below the routes: what it hands back, and its cache.

The route tests (tests/test_tracks_archive_route.py) cover the rules; these
cover that the store returns plain Python values, that a warm cache answers
without opening files, that the cache keeps to its byte bound, that a broken
file warns once whatever the cache keeps, and that concurrent calls agree.

They also cover days compacted by services/compaction.py from parts written as
the route tests write them: that a compacted day answers exactly as its raw
parts did, that late parts are read beside a compact and superseded ones never
are, that only the compact's own footer decides what it supersedes, that a day
kept only in object storage is read through services/cold_store.py (stood in
for, and through moto's S3), as is one whose compact left local disk while
some of the parts it lists stayed, beside its late parts, and that a compact
replaced mid-call is never read half old and half new.
"""

import logging
import os
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path

import boto3
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from moto import mock_aws

from services import cold_store, compaction, publication, tracks_store
from services.cold_store import ColdStoreError
from services.compaction import COMPACT_NAME, PARQUET_CONTENT_TYPE, SIDECAR_NAME, TRACKS
from services.tasks import track_archive as ta
from tests.object_store_env import FAKE_AWS_ENV, store_env
from tests.test_tracks_archive_route import (
    MIRRORED,
    PRIV,
    PUB_A,
    PUB_B,
    SYN_A,
    SYN_B,
    _fleets,
    _seed,
    at,
    connect_mirrored,
    ms_of,
    old,
    partition,
    rec,
    write_new,
    write_old,
)

PLAIN = (int, float, str, bool, type(None))


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "tracks"
    monkeypatch.setattr(ta, "_TRACKS_DIR", str(path))
    tracks_store._reset_for_tests()
    yield path
    tracks_store._reset_for_tests()


@pytest.fixture
def private():
    _seed()
    return publication.private_node_ids()


@pytest.fixture
def opened(monkeypatch):
    names: list[str] = []
    original = pq.ParquetFile

    class Recording(original):
        def __init__(self, source, *args, **kwargs):
            names.append(Path(source).name)
            super().__init__(source, *args, **kwargs)

    monkeypatch.setattr(pq, "ParquetFile", Recording)
    return names


def query(private, start=None, end=None, **kwargs) -> dict:
    return tracks_store.query_tracks(ms_of(start or at(10)), ms_of(end or at(10, 30)), private=private, **kwargs)


def _walk(value, path="body"):
    """Every leaf is exactly a builtin scalar: no numpy, no pyarrow, no subclass."""
    if isinstance(value, dict):
        for key, item in value.items():
            assert type(key) is str, path
            _walk(item, f"{path}.{key}")
    elif isinstance(value, list):
        assert type(value) is list, path
        for i, item in enumerate(value):
            _walk(item, f"{path}[{i}]")
    else:
        assert type(value) in PLAIN, (path, type(value))


def test_the_store_returns_only_plain_python_values(root, private):
    _fleets(root)
    write_new(
        root,
        at(10, 20),
        [rec(at(10, 15), lat=21.0, is_anomalous=True, anomaly_types=["x"], adsb_hex="abc123", alt_m=float("nan"))],
    )
    body = query(private, fleet="all")
    assert body["count"] == len(body["rows"]) > 0
    _walk(body)
    # Not a float NaN that an encoder happens to write as null.
    assert next(row for row in body["rows"] if row["lat"] == 21.0)["alt_m"] is None


def test_a_warm_cache_answers_without_opening_a_file(root, private, opened):
    for minute in range(5):
        write_new(root, at(10, 5 + minute), [rec(at(10, minute), lat=float(minute))])
    first = query(private)
    assert opened
    opened.clear()
    assert query(private) == first
    assert opened == []
    # A narrower window inside it reuses the same decoded row groups.
    assert [row["lat"] for row in query(private, at(10, 1), at(10, 3))["rows"]] == [1.0, 2.0]
    assert opened == []


def test_a_new_file_is_the_only_one_opened_on_the_next_call(root, private, opened):
    write_new(root, at(10, 5), [rec(at(10, 1), lat=1.0)])
    query(private)
    opened.clear()
    added = write_new(root, at(10, 6), [rec(at(10, 2), lat=2.0)])
    assert [row["lat"] for row in query(private)["rows"]] == [1.0, 2.0]
    assert set(opened) == {added.name}


def test_privacy_is_decided_per_call_not_cached(root, private):
    write_new(root, at(10, 5), [rec(at(10, 1), (PUB_A,), lat=1.0), rec(at(10, 2), (PUB_A, PRIV), lat=2.0)])
    assert [row["lat"] for row in query(private)["rows"]] == [1.0]
    assert [row["lat"] for row in query(frozenset())["rows"]] == [1.0, 2.0]
    assert [row["lat"] for row in query(frozenset({PUB_A}))["rows"]] == []


def test_each_row_is_published_under_its_own_stored_refs(root, private, monkeypatch):
    """Rows with the same ids are told apart by their stored refs: an older
    ref, none (resolved now), the current one, and an empty slot (resolved now)."""
    from services import node_auth, node_refs

    now = connect_mirrored(monkeypatch)
    then = node_auth.mint_node_ref()
    a = node_refs.ref_for(PUB_A)
    ids = (PUB_A, MIRRORED)
    write_new(
        root,
        at(10, 5),
        [
            rec(at(10, 1), ids, lat=1.0, contributing_node_refs=[a, then]),
            rec(at(10, 2), ids, lat=2.0),
            rec(at(10, 3), ids, lat=3.0, contributing_node_refs=[a, now]),
            rec(at(10, 4), ids, lat=4.0, contributing_node_refs=[a, ""]),
        ],
    )
    for _ in range(2):
        rows = query(private)["rows"]
        assert [(row["lat"], row["node_refs"]) for row in rows] == [
            (1.0, [a, then]),
            (2.0, [a, now]),
            (3.0, [a, now]),
            (4.0, [a, now]),
        ]


def test_the_cache_keeps_to_its_byte_bound(root, private, monkeypatch):
    monkeypatch.setattr(tracks_store, "TRACKS_CACHE_MAX_BYTES", 6_000)
    for minute in range(20):
        write_new(root, at(10, 5 + minute), [rec(at(10, minute), lat=float(minute))])
    for _ in range(2):
        rows = query(private)["rows"]
        assert [row["lat"] for row in rows] == [float(m) for m in range(20)]
        assert 0 < tracks_store._cache._bytes <= 6_000
        assert sum(size for _, size in tracks_store._cache._entries.values()) == tracks_store._cache._bytes


def test_with_no_cache_every_answer_is_still_right(root, private, monkeypatch):
    monkeypatch.setattr(tracks_store, "TRACKS_CACHE_MAX_BYTES", 0)
    _fleets(root)
    for _ in range(2):
        assert [row["lat"] for row in query(private, fleet="all")["rows"]] == [
            1.0,
            2.0,
            3.0,
            4.0,
            11.0,
            12.0,
            13.0,
            14.0,
            15.0,
        ]
        assert tracks_store._cache._bytes == 0


def test_an_entry_larger_than_the_bound_is_not_kept(monkeypatch):
    monkeypatch.setattr(tracks_store, "TRACKS_CACHE_MAX_BYTES", 100)
    cache = tracks_store._Cache()
    cache.put(("a",), "kept", 60)
    cache.put(("b",), "too big", 101)
    assert cache.get(("b",)) is tracks_store._MISS
    assert cache.get(("a",)) == "kept"
    cache.put(("c",), "evicts a", 50)
    assert cache.get(("a",)) is tracks_store._MISS
    assert (cache.get(("c",)), cache._bytes) == ("evicts a", 50)
    cache.put(("c",), "replaced", 70)
    assert (cache.get(("c",)), cache._bytes) == ("replaced", 70)


def _store_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "services.tracks_store"]


@pytest.mark.parametrize("bound", [0, 4_000], ids=["nothing-cached", "evicting"])
def test_a_broken_file_warns_once_per_version_whatever_the_cache_keeps(root, private, monkeypatch, caplog, bound):
    monkeypatch.setattr(tracks_store, "TRACKS_CACHE_MAX_BYTES", bound)
    for minute in range(10):
        write_new(root, at(10, 5 + minute), [rec(at(10, minute), lat=float(minute))])
    broken = partition(root, at(10)) / f"part-101600-{uuid.uuid4().hex}.parquet"
    broken.write_bytes(b"not parquet at all")
    lats = [float(m) for m in range(10)]

    with caplog.at_level(logging.WARNING, logger="services.tracks_store"):
        for _ in range(2):
            assert [row["lat"] for row in query(private)["rows"]] == lats
        with ThreadPoolExecutor(max_workers=8) as pool:
            answers = list(pool.map(lambda _: query(private), range(16)))
        assert all([row["lat"] for row in answer["rows"]] == lats for answer in answers)
        warned = _store_warnings(caplog)
        assert len(warned) == 1 and broken.name in warned[0]
        # Nothing is kept, or the files' own entries no longer all fit.
        assert tracks_store._cache._bytes <= bound
        assert len(tracks_store._cache._entries) < 20

        broken.write_bytes(b"still not parquet, and longer")
        for _ in range(2):
            assert [row["lat"] for row in query(private)["rows"]] == lats
    warned = _store_warnings(caplog)
    assert len(warned) == 2 and broken.name in warned[1]


def test_the_warnings_given_are_remembered_up_to_a_bound(root, monkeypatch, caplog):
    """Past the bound the longest unused is forgotten, so it would warn again."""
    monkeypatch.setattr(tracks_store, "_WARNED_MAX", 2)

    def fail(name):
        try:
            raise OSError("cannot be read")
        except OSError:
            tracks_store._warn_once(("footer", name, 1, 1), "skipping %s", name)

    with caplog.at_level(logging.WARNING, logger="services.tracks_store"):
        for name in "abacab":
            fail(name)
    assert _store_warnings(caplog) == ["skipping a", "skipping b", "skipping c", "skipping b"]
    assert list(tracks_store._warned) == [("footer", "a", 1, 1), ("footer", "b", 1, 1)]


def test_pairs_tell_null_ids_and_null_refs_apart_and_map_each_row_back():
    """Every mix of null and set ids and refs, across chunks, and "" apart from null."""
    ids = pa.chunked_array([["a", None, "a", None, "b"], [None, "a", "b", "a", None]], pa.string())
    refs = pa.chunked_array([[None, "x", "x", None, None], ["x", None, "", "x", None]], pa.string())
    rows = list(zip(ids.to_pylist(), refs.to_pylist()))
    pairs, index = tracks_store._pairs(ids, refs)
    assert len(pairs) == len(set(pairs)) == len(set(rows)) == 6
    assert [pairs[i] for i in index.to_pylist()] == rows


def test_concurrent_calls_agree(root, private, monkeypatch):
    # Small enough that entries are evicted while the calls run.
    monkeypatch.setattr(tracks_store, "TRACKS_CACHE_MAX_BYTES", 20_000)
    _fleets(root)
    for minute in range(10):
        write_new(root, at(10, 11 + minute), [rec(at(10, 10, minute), (SYN_A,), lat=60.0 + minute)])
    expected = query(private, fleet="all")
    tracks_store._reset_for_tests()
    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(lambda _: query(private, fleet="all"), range(32)))
    assert all(answer == expected for answer in answers)


def test_availability_warm_opens_nothing_and_sees_changes(root, opened):
    write_new(root, at(10, 5), [rec(at(10, 1))])
    gone = write_new(root, at(11, 5), [rec(at(11, 1))])
    write_old(root, at(12, 5), [old(at(12, 1), PUB_A)])
    first = tracks_store.hourly_availability(date(2026, 10, 18), date(2026, 10, 18), fleet="real")
    assert [(h["count"], h["unclassified"]) for h in first["hours"]] == [(1, 0), (1, 0), (0, 1)]
    opened.clear()
    assert tracks_store.hourly_availability(date(2026, 10, 18), date(2026, 10, 18), fleet="real") == first
    assert opened == []
    gone.unlink()
    after = tracks_store.hourly_availability(date(2026, 10, 18), date(2026, 10, 18), fleet="real")
    assert [h["start_ms"] for h in after["hours"]] == [ms_of(at(10)), ms_of(at(12))]
    assert opened == []


# ── Compacted days ───────────────────────────────────────────────────────────

D18, D19 = date(2026, 10, 18), date(2026, 10, 19)
FLEETS = ("real", "synthetic", "all")
# Past the end of 2026-10-19 and its settle margin, so both days can be compacted.
COMPACTED_AT = ms_of(at(1, day=20))
WINDOWS = {
    "inside an hour": (at(10), at(10, 45)),
    "across an hour": (at(10, 30), at(11, 30)),
    "across midnight": (at(23, 50), at(0, 5, day=19)),
    "up to midnight": (at(23, 30), at(0, day=19)),
}


def lats(body: dict) -> list[float]:
    return [row["lat"] for row in body["rows"]]


def compact(root: Path, *days: date) -> None:
    for day in days:
        assert compaction.compact_day(TRACKS, str(root), day, now_ms=COMPACTED_AT) == "compacted"


def _hours_10_and_11(root: Path) -> list[Path]:
    """In each hour, a new-schema part with a row of each kind, one without
    contributors and one a private node contributed to, and a legacy part
    with the same; lat is the hour plus the minute in hundredths."""
    parts = []
    for h in (10, 11):
        parts.append(
            write_new(
                root,
                at(h, 20),
                [
                    rec(at(h, 1), (PUB_A, PUB_B), lat=h + 0.01),
                    rec(at(h, 2), (SYN_A, SYN_B), lat=h + 0.02),
                    rec(at(h, 3), (PUB_A, SYN_A), lat=h + 0.03),
                    rec(at(h, 4), (), lat=h + 0.04),
                    rec(at(h, 5), (PUB_A, PRIV), lat=h + 0.05),
                ],
            )
        )
        parts.append(
            write_old(
                root,
                at(h, 40),
                [
                    old(at(h, 31), f"{PUB_A},{PUB_B}", lat=h + 0.31),
                    old(at(h, 32), f"{SYN_A},{SYN_B}", lat=h + 0.32),
                    old(at(h, 33), "e2e-node-1,test-node-2", lat=h + 0.33),
                    old(at(h, 34), f"{PUB_B},{SYN_B}", lat=h + 0.34),
                    old(at(h, 35), "", lat=h + 0.35),
                    old(at(h, 36), f"{PUB_A},{PRIV}", lat=h + 0.36),
                ],
            )
        )
    return parts


def _two_days(root: Path) -> list[Path]:
    """_hours_10_and_11, then 23:58-23:59 in the day's own partition, and the
    next day's partition: parts flushed after midnight holding rows solved
    before it, new and legacy, and one of its own first minutes. Every part's
    rows lie in one hour."""
    return [
        *_hours_10_and_11(root),
        write_new(root, at(23, 59, 30), [rec(at(23, 58), lat=23.58), rec(at(23, 59), (SYN_A,), lat=23.59)]),
        write_new(
            root, at(0, 0, 30, day=19), [rec(at(23, 59, 50), lat=23.5950), rec(at(23, 59, 51), (SYN_B,), lat=23.5951)]
        ),
        write_old(
            root, at(0, 1, day=19), [old(at(23, 59, 55), PUB_B, lat=23.5955), old(at(23, 59, 56), SYN_A, lat=23.5956)]
        ),
        write_new(root, at(0, 5, day=19), [rec(at(0, 2, day=19), lat=0.02), rec(at(0, 3, day=19), (SYN_A,), lat=0.03)]),
    ]


def _answers(private) -> dict:
    """Every window under every fleet, and both days' availability under every fleet."""
    answers: dict = {
        (fleet, name): query(private, *window, fleet=fleet) for fleet in FLEETS for name, window in WINDOWS.items()
    }
    answers |= {fleet: tracks_store.hourly_availability(D18, D19, fleet=fleet) for fleet in FLEETS}
    return answers


def test_a_compacted_day_answers_exactly_as_its_raw_parts_did(root, private):
    parts = _two_days(root)
    before = _answers(private)
    # Legacy rows classified from their ids, and rows filed under the next day, are among them.
    assert lats(before["real", "inside an hour"]) == [10.01, 10.31]
    assert lats(before["synthetic", "across an hour"]) == [10.32, 10.33, 11.02]
    assert lats(before["real", "across midnight"]) == [23.58, 23.5950, 23.5955, 0.02]
    assert lats(before["synthetic", "up to midnight"]) == [23.59, 23.5951, 23.5956]
    assert [(h["count"], h["unclassified"], h["cold"]) for h in before["real"]["hours"]] == [
        (2, 6, False),
        (2, 6, False),
        (2, 2, False),
        (1, 0, False),
    ]

    compact(root, D18, D19)
    assert _answers(private) == before
    # As the lifecycle leaves them once the parts are uploaded and deleted.
    for part in parts:
        part.unlink()
    assert _answers(private) == before


def test_a_part_straddling_an_hour_counts_in_its_rows_own_hours_once_compacted(root, private):
    write_new(root, at(12, 10), [rec(at(11, 50), lat=1.0), rec(at(12, 5), lat=2.0)])
    rows = query(private, at(11, 30), at(12, 30))
    before = tracks_store.hourly_availability(D18, D18)
    assert [(h["start_ms"], h["count"]) for h in before["hours"]] == [(ms_of(at(11)), 2)]

    compact(root, D18)
    after = tracks_store.hourly_availability(D18, D18)
    assert [(h["start_ms"], h["count"]) for h in after["hours"]] == [(ms_of(at(11)), 1), (ms_of(at(12)), 1)]
    assert after["total"] == before["total"] == 2
    assert query(private, at(11, 30), at(12, 30)) == rows


def test_a_late_part_is_read_beside_the_compact_and_superseded_parts_never_are(root, tmp_path, private, monkeypatch):
    first = write_new(root, at(10, 20), [rec(at(10, 1), lat=1.0), rec(at(10, 2), (SYN_A,), lat=2.0)])
    legacy = write_old(root, at(10, 40), [old(at(10, 31), PUB_A, lat=3.0)])
    compact(root, D18)
    late = write_new(root, at(10, 50), [rec(at(10, 45), lat=4.0)])

    stat = tracks_store._stat
    stated: list[str] = []

    def recording(path, flushed):
        stated.append(Path(path).name)
        return stat(path, flushed)

    monkeypatch.setattr(tracks_store, "_stat", recording)

    def answers() -> tuple:
        return (
            lats(query(private, at(10), at(11), fleet="all")),
            lats(query(private, at(10), at(11), fleet="real")),
            [(h["count"], h["unclassified"]) for h in tracks_store.hourly_availability(D18, D18)["hours"]],
        )

    assert answers() == ([1.0, 2.0, 3.0, 4.0], [1.0, 3.0, 4.0], [(2, 1)])
    assert set(stated) == {late.name}

    # A superseded part rewritten with other rows is still never read.
    os.replace(write_new(tmp_path / "elsewhere", at(10, 20), [rec(at(10, 6), lat=9.0)]), first)
    assert answers() == ([1.0, 2.0, 3.0, 4.0], [1.0, 3.0, 4.0], [(2, 1)])
    assert first.name not in stated and legacy.name not in stated


def test_a_sidecar_lagging_its_compact_reads_no_row_twice(root, private, monkeypatch):
    write_new(root, at(10, 20), [rec(at(10, 1), lat=1.0)])
    compact(root, D18)
    write_new(root, at(10, 50), [rec(at(10, 45), lat=2.0)])

    def crash(directory, manifest):
        raise OSError("power lost")

    with monkeypatch.context() as patched:
        patched.setattr(compaction, "write_sidecar", crash)
        assert compaction.compact_day(TRACKS, str(root), D18, now_ms=COMPACTED_AT) == "error"
    directory = partition(root, at(10))
    footer = compaction.read_footer_manifest(str(directory / COMPACT_NAME))
    sidecar = compaction.read_sidecar(str(directory))
    assert footer is not None and sidecar is not None
    assert (len(footer.parts), len(sidecar.parts)) == (2, 1)

    assert lats(query(private, at(10), at(11), fleet="all")) == [1.0, 2.0]
    assert [h["count"] for h in tracks_store.hourly_availability(D18, D18)["hours"]] == [2]


def test_an_unusable_compact_is_warned_about_once_and_its_raw_parts_read(root, private, caplog):
    _hours_10_and_11(root)

    def answers() -> dict:
        return {
            fleet: (
                query(private, at(10), at(12), fleet=fleet),
                tracks_store.hourly_availability(D18, D18, fleet=fleet),
            )
            for fleet in FLEETS
        }

    before = answers()
    compact(root, D18)
    (partition(root, at(10)) / COMPACT_NAME).write_bytes(b"not parquet at all")
    with caplog.at_level(logging.WARNING, logger="services.tracks_store"):
        for _ in range(2):
            assert answers() == before
    warned = _store_warnings(caplog)
    assert len(warned) == 1 and COMPACT_NAME in warned[0]


# ── Days kept only in object storage ─────────────────────────────────────────


@pytest.fixture
def cold_day(root, tmp_path, private) -> tuple[dict, Path]:
    """Day 18 compacted, its compact moved to a stand-in for object storage
    and its raw parts deleted, leaving the sidecar alone, as the lifecycle
    leaves an old day; the answers the raw parts gave, and the compact."""
    _hours_10_and_11(root)
    before = _answers(private)
    compact(root, D18)
    directory = partition(root, at(10))
    stored = tmp_path / "object-store" / "2026-10-18.parquet"
    stored.parent.mkdir()
    os.replace(directory / COMPACT_NAME, stored)
    for part in directory.glob("part-*.parquet"):
        part.unlink()
    assert sorted(os.listdir(directory)) == [SIDECAR_NAME]
    return before, stored


def serving(path: Path, events: list):
    """open_day standing in for cold_store's: records each call and yields ``path`` until the block exits."""

    @contextmanager
    def open_day(store, day, *, expected, pending=None) -> Iterator[str]:
        events.append(("open", store, day, expected))
        try:
            yield str(path)
        finally:
            events.append("release")

    return open_day


def refuse(*args, **kwargs):
    raise AssertionError("object storage asked")


def test_a_day_kept_only_in_object_storage_answers_as_its_raw_parts_did(root, cold_day, private, monkeypatch):
    before, stored = cold_day
    events: list = []
    monkeypatch.setattr(cold_store, "enabled", lambda: True)
    monkeypatch.setattr(cold_store, "open_day", serving(stored, events))
    read_group = tracks_store._read_group

    def decoding(*args, **kwargs):
        events.append("decode")
        return read_group(*args, **kwargs)

    monkeypatch.setattr(tracks_store, "_read_group", decoding)
    sidecar = compaction.read_sidecar(str(partition(root, at(10))))
    assert sidecar is not None

    for fleet in FLEETS:
        for name in ("inside an hour", "across an hour"):
            events.clear()
            assert query(private, *WINDOWS[name], fleet=fleet) == before[fleet, name]
            # Held, against the sidecar, until every row group is decoded.
            assert events[0] == ("open", TRACKS, D18, sidecar)
            assert events[-1] == "release"
            assert set(events[1:-1]) == {"decode"}


def test_availability_marks_a_cold_days_hours_from_its_sidecar_alone(root, cold_day, monkeypatch):
    before, _ = cold_day
    monkeypatch.setattr(cold_store, "open_day", refuse)
    for name in ("download_bytes", "list_keys"):
        monkeypatch.setattr(cold_store._client, name, refuse)
    monkeypatch.setattr(cold_store, "enabled", lambda: True)
    for fleet in FLEETS:
        cold = {**before[fleet], "from": "2026-10-18", "to": "2026-10-18"}
        cold["hours"] = [{**h, "cold": True} for h in before[fleet]["hours"]]
        assert tracks_store.hourly_availability(D18, D18, fleet=fleet) == cold

    # A rewritten sidecar and a change in the configured store are both seen at once.
    directory = str(partition(root, at(10)))
    sidecar = compaction.read_sidecar(directory)
    assert sidecar is not None and len(sidecar.groups) > 1
    compaction.write_sidecar(directory, replace(sidecar, groups=sidecar.groups[:1], rows=sidecar.groups[0].rows))
    assert [(h["start_ms"], h["cold"]) for h in tracks_store.hourly_availability(D18, D18, fleet="all")["hours"]] == [
        (ms_of(at(10)), True)
    ]
    monkeypatch.setattr(cold_store, "enabled", lambda: False)
    assert tracks_store.hourly_availability(D18, D18, fleet="all")["hours"] == []


def test_without_an_object_store_a_day_kept_only_there_has_no_hours_and_no_rows(root, cold_day, private, monkeypatch):
    monkeypatch.setattr(cold_store, "enabled", lambda: False)
    monkeypatch.setattr(cold_store, "open_day", refuse)
    for fleet in FLEETS:
        assert tracks_store.hourly_availability(D18, D18, fleet=fleet)["hours"] == []
        assert query(private, at(10), at(11), fleet=fleet)["rows"] == []


def test_a_day_that_cannot_be_loaded_from_object_storage_fails_the_call(root, cold_day, private, monkeypatch):
    @contextmanager
    def unavailable(store, day, *, expected, pending=None) -> Iterator[str]:
        raise ColdStoreError(day)
        yield ""

    monkeypatch.setattr(cold_store, "enabled", lambda: True)
    monkeypatch.setattr(cold_store, "open_day", unavailable)
    with pytest.raises(ColdStoreError):
        query(private)


@pytest.fixture
def object_store(tmp_path, monkeypatch) -> Iterator[list[str]]:
    """A configured object store with one empty bucket, set up as tests/test_cold_store.py
    sets it up, an empty cold cache, and the keys downloaded from it."""
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


def test_a_day_only_in_object_storage_is_fetched_once_and_answers_as_before(root, cold_day, private, object_store):
    before, stored = cold_day
    key = compaction.object_key(TRACKS, D18, COMPACT_NAME)
    assert cold_store.upload_bytes(key, stored.read_bytes(), PARQUET_CONTENT_TYPE)

    for fleet in FLEETS:
        for name in ("inside an hour", "across an hour"):
            assert query(private, *WINDOWS[name], fleet=fleet) == before[fleet, name]
        hours = tracks_store.hourly_availability(D18, D18, fleet=fleet)["hours"]
        assert hours == [{**h, "cold": True} for h in before[fleet]["hours"]]
    assert object_store == [key]


LEFTOVER_WINDOW = (at(10), at(14))


@pytest.fixture
def leftovers(root, private) -> tuple[dict, Path, Path, bytes]:
    """Day 18 compacted, a late part flushed beside its compact, then the
    compact and three of the four parts it lists deleted locally, as an upload
    cap, a torn part or a short retention can leave a day; what the day
    answered while its compact was on disk, the listed part left, the late
    part, and the compact's bytes."""
    parts = _hours_10_and_11(root)
    compact(root, D18)
    late = write_new(root, at(13, 20), [rec(at(13, 5), lat=13.05), rec(at(13, 6), (SYN_A,), lat=13.06)])
    whole = {
        fleet: (query(private, *LEFTOVER_WINDOW, fleet=fleet), tracks_store.hourly_availability(D18, D18, fleet=fleet))
        for fleet in FLEETS
    }
    directory = partition(root, at(10))
    data = (directory / COMPACT_NAME).read_bytes()
    (directory / COMPACT_NAME).unlink()
    for part in parts[1:]:
        part.unlink()
    assert sorted(os.listdir(directory)) == sorted([SIDECAR_NAME, parts[0].name, late.name])
    return whole, parts[0], late, data


def test_a_day_with_some_listed_parts_left_reads_its_compact_from_object_storage_and_its_late_parts(
    root, leftovers, private, object_store, monkeypatch
):
    whole, _, late, data = leftovers
    key = compaction.object_key(TRACKS, D18, COMPACT_NAME)
    assert cold_store.upload_bytes(key, data, PARQUET_CONTENT_TYPE)
    stat = tracks_store._stat
    stated: list[str] = []

    def recording(path, flushed):
        stated.append(Path(path).name)
        return stat(path, flushed)

    monkeypatch.setattr(tracks_store, "_stat", recording)

    # The whole day, once each, and the late part's rows.
    assert lats(whole["all"][0]) == [
        *(10.01, 10.02, 10.03, 10.04, 10.31, 10.32, 10.33, 10.34, 10.35),
        *(11.01, 11.02, 11.03, 11.04, 11.31, 11.32, 11.33, 11.34, 11.35),
        *(13.05, 13.06),
    ]
    assert [(h["start_ms"], h["count"]) for h in whole["all"][1]["hours"]] == [
        (ms_of(at(10)), 11),
        (ms_of(at(11)), 11),
        (ms_of(at(13)), 2),
    ]
    for fleet in FLEETS:
        body, hours = whole[fleet]
        assert query(private, *LEFTOVER_WINDOW, fleet=fleet) == body
        assert tracks_store.hourly_availability(D18, D18, fleet=fleet) == {
            **hours,
            "hours": [{**h, "cold": True} for h in hours["hours"]],
        }
    # The listed part left on disk is never read, the late one is, and the day is fetched once.
    assert set(stated) == {late.name}
    assert object_store == [key]


def test_a_day_with_every_listed_part_left_reads_them_and_asks_object_storage_nothing(root, private, monkeypatch):
    _hours_10_and_11(root)
    before = {
        fleet: (query(private, at(10), at(12), fleet=fleet), tracks_store.hourly_availability(D18, D18, fleet=fleet))
        for fleet in FLEETS
    }
    compact(root, D18)
    (partition(root, at(10)) / COMPACT_NAME).unlink()
    monkeypatch.setattr(cold_store, "enabled", lambda: True)
    monkeypatch.setattr(cold_store, "open_day", refuse)
    for fleet in FLEETS:
        assert (
            query(private, at(10), at(12), fleet=fleet),
            tracks_store.hourly_availability(D18, D18, fleet=fleet),
        ) == before[fleet]


def test_without_an_object_store_a_day_with_some_listed_parts_left_serves_the_parts_on_disk(
    root, leftovers, private, monkeypatch
):
    monkeypatch.setattr(cold_store, "enabled", lambda: False)
    monkeypatch.setattr(cold_store, "open_day", refuse)
    # Best effort: the rows of the listed part left and of the late part, and no error.
    assert lats(query(private, *LEFTOVER_WINDOW, fleet="all")) == [10.01, 10.02, 10.03, 10.04, 13.05, 13.06]
    assert [
        (h["start_ms"], h["count"], h["cold"]) for h in tracks_store.hourly_availability(D18, D18, fleet="all")["hours"]
    ] == [(ms_of(at(10)), 5, False), (ms_of(at(13)), 2, False)]


# ── One version per call ─────────────────────────────────────────────────────


def test_a_compact_replaced_mid_call_is_read_whole_from_one_version(root, tmp_path, private, monkeypatch):
    write_new(root, at(10, 20), [rec(at(10, 1), lat=1.0)])
    write_new(root, at(11, 20), [rec(at(11, 1), lat=2.0)])
    compact(root, D18)
    directory = partition(root, at(10))
    for part in directory.glob("part-*.parquet"):
        part.unlink()
    # The same day re-compacted elsewhere from other rows, with a synthetic group
    # where the first version's second real group was.
    other = tmp_path / "other"
    write_new(other, at(10, 20), [rec(at(10, 1), lat=51.0), rec(at(10, 2), (SYN_A,), lat=52.0)])
    write_new(other, at(11, 20), [rec(at(11, 1), lat=53.0)])
    compact(other, D18)
    replacement = partition(other, at(10)) / COMPACT_NAME

    footer = tracks_store._compact_footer

    def footer_then_replace(*args):
        found = footer(*args)
        if replacement.exists():
            os.replace(replacement, directory / COMPACT_NAME)
        return found

    monkeypatch.setattr(tracks_store, "_compact_footer", footer_then_replace)
    assert lats(query(private, at(10), at(11, 30))) in ([1.0, 2.0], [51.0, 53.0])
    assert not replacement.exists()
    assert lats(query(private, at(10), at(11, 30))) == [51.0, 53.0]


# ── Parts that vanish while a day is read ────────────────────────────────────


def test_a_part_compacted_and_deleted_after_its_day_was_planned_is_read_once_from_the_compact(
    root, private, monkeypatch
):
    first = write_new(root, at(10, 20), [rec(at(10, 1), lat=1.0), rec(at(10, 2), (SYN_A,), lat=2.0)])
    write_old(root, at(10, 40), [old(at(10, 31), PUB_A, lat=3.0)])
    write_new(root, at(10, 50), [rec(at(10, 45), lat=4.0)])
    whole = query(private, at(10), at(11), fleet="all")
    assert lats(whole) == [1.0, 2.0, 3.0, 4.0]
    plan = compaction.read_plan

    def plan_then_prune(*args, **kwargs):
        found = plan(*args, **kwargs)
        if not (partition(root, at(10)) / COMPACT_NAME).exists():
            # The hourly sweep compacts the day, and the lifecycle deletes a part its sidecar lists.
            compact(root, D18)
            first.unlink()
        return found

    monkeypatch.setattr(compaction, "read_plan", plan_then_prune)
    assert query(private, at(10), at(11), fleet="all") == whole


def test_a_day_that_loses_a_part_on_every_read_fails_naming_only_the_day(root, private, monkeypatch):
    write_new(root, at(10, 20), [rec(at(10, 1), lat=1.0)])
    listing = tracks_store._part_names
    listed: list[str] = []

    def listing_a_part_gone_before_its_read(directory):
        listed.append(directory)
        return [*listing(directory), f"part-102500-{uuid.uuid4().hex}.parquet"]

    monkeypatch.setattr(tracks_store, "_part_names", listing_a_part_gone_before_its_read)
    with pytest.raises(ColdStoreError) as raised:
        query(private, at(10), at(11))
    assert str(raised.value) == "The archived day 2026-10-18 changed while it was read; try again."
    assert listed == [str(partition(root, at(10)))] * 3
