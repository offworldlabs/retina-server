"""services/tracks_store.py below the routes: what it hands back, and its cache.

The route tests (tests/test_tracks_archive_route.py) cover the rules; these
cover that the store returns plain Python values, that a warm cache answers
without opening files, that the cache keeps to its byte bound, that a broken
file warns once whatever the cache keeps, and that concurrent calls agree.
"""

import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from services import publication, tracks_store
from services.tasks import track_archive as ta
from tests.test_tracks_archive_route import (
    MIRRORED,
    PRIV,
    PUB_A,
    SYN_A,
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
