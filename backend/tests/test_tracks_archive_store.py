"""services/tracks_store.py below the routes: what it hands back, and its cache.

The route tests (tests/test_tracks_archive_route.py) cover the rules; these
cover that the store returns plain Python values, that a warm cache answers
without opening files, that the cache keeps to its byte bound, and that
concurrent calls agree.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

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


def test_concurrent_calls_agree(root, private, monkeypatch):
    # Small enough that entries are evicted while the calls run.
    monkeypatch.setattr(tracks_store, "TRACKS_CACHE_MAX_BYTES", 20_000)
    _fleets(root)
    for minute in range(10):
        write_new(root, at(10, 11 + minute), [rec(at(10, 10, minute), (SYN_A,), lat=100.0 + minute)])
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
