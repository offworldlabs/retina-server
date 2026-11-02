"""services/track_history.py: the keyed history's pages, hex lookup, row shape and span cap.

Files are written by the archive's own writer through the route tests' helpers,
into a per-test root, with Node rows behind the ids so the publication rules run
against the real policy and identity caches.
"""

from datetime import date

import pytest

from services import compaction, publication, track_history, tracks_store
from services.tasks import track_archive as ta
from services.tracks_store import BBox
from tests.test_tracks_archive_route import PRIV, PUB_A, PUB_B, SYN_A, SYN_B, _seed, at, ms_of, rec, write_new

# Every latitude, and every longitude but those east of 80 W.
BOX = BBox(-90.0, -90.0, 90.0, -80.0)
OTHER_KEY = "mn-dark-1792321200000-35.100--82.000"
# The live feed's hex for OTHER_KEY: "mn" and the first 10 hex digits of its sha256.
OTHER_HEX = "mnaebf0d477f"


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "tracks"
    monkeypatch.setattr(ta, "_TRACKS_DIR", str(path))
    tracks_store._reset_for_tests()
    yield path
    tracks_store._reset_for_tests()


@pytest.fixture
def refs():
    return _seed()


def history(start, end, **kwargs) -> track_history.HistoryPage:
    private = publication.private_node_ids()
    return track_history.query_history(ms_of(start), ms_of(end), max_span_s=86_400, private=private, **kwargs)


def test_pages_are_query_tracks_hour_by_hour_with_no_row_repeated_or_missed(root, refs):
    # Real rows lat 1..10 in hours 9 to 12, in parts that straddle hours; lat 50 and up are left out.
    write_new(
        root,
        at(10, 0, 30),
        [
            rec(at(9, 20), lat=50.0),
            rec(at(9, 40), lat=1.0),
            rec(at(9, 45), (PUB_A, PRIV), lat=51.0),
            rec(at(9, 50), lat=2.0),
        ],
    )
    write_new(
        root,
        at(11, 0, 1),
        [
            rec(at(10, 10), lat=3.0),
            rec(at(10, 15), (SYN_A, SYN_B), lat=52.0),
            rec(at(10, 25), (PUB_A, SYN_A), lat=53.0),
            rec(at(10, 30), lat=54.0, lon=10.0),
            rec(at(10, 59, 59, 900), frame=at(10, 59, 59, 200), lat=4.0),
            # Solved in hour 11, framed in hour 10 before lat 4: it is served with hour 11.
            rec(at(11, 0, 0, 300), frame=at(10, 59, 59), lat=5.0),
        ],
    )
    write_new(
        root,
        at(12, 31),
        [
            rec(at(11, 5), lat=6.0),
            # Equal on frame, solve and track_key: the first ends page one, the second opens page two.
            rec(at(11, 10), lat=7.0),
            rec(at(11, 10), lat=8.0),
            rec(at(11, 30), lat=9.0),
            rec(at(12, 5), lat=10.0),
            rec(at(12, 30), lat=55.0),
        ],
    )
    private = publication.private_node_ids()
    expected = [
        (row["frame_ts_ms"] / 1000, row["lat"], row["lon"])
        for start, end in [(at(9, 30), at(10)), (at(10), at(11)), (at(11), at(12)), (at(12), at(12, 20))]
        for row in tracks_store.query_tracks(ms_of(start), ms_of(end), bbox=BOX, private=private)["rows"]
    ]

    first = history(at(9, 30), at(12, 20), bbox=BOX, limit=7)
    # The day is compacted between the two requests: the second page reads the compact.
    compacted = compaction.compact_day(compaction.TRACKS, str(root), date(2026, 10, 18), now_ms=ms_of(at(1, day=20)))
    assert compacted == "compacted"
    second = history(at(9, 30), at(12, 20), bbox=BOX, limit=7, cursor=first.next_cursor)

    assert (len(first.rows), first.next_cursor is None, len(second.rows), second.next_cursor) == (7, False, 3, None)
    served = first.rows + second.rows
    assert [(row["ts"], row["lat"], row["lon"]) for row in served] == expected
    assert [row["lat"] for row in served] == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert {row["hex"] for row in served} == {"mn61b5e3aea4"}


def test_a_hex_finds_its_adsb_rows_and_the_solver_rows_of_its_track_key(root, refs):
    write_new(
        root,
        at(10, 31),
        [
            rec(at(10, 1), adsb_hex="ABC123", alt_m=3048.0, vel_east=100.0, vel_north=0.0, lat=1.0),
            rec(at(10, 2), track_key=OTHER_KEY, lat=2.0),
            # Found by its track's hex, which the live feed showed for it, while carrying its ADS-B hex.
            rec(at(10, 3), track_key=OTHER_KEY, adsb_hex="def456", lat=3.0),
            rec(at(10, 4), lat=4.0),
            rec(at(10, 5), (PUB_A, PRIV), adsb_hex="abc123", lat=5.0),
            rec(at(10, 6), track_key=OTHER_KEY, alt_m=None, vel_east=None, lat=6.0),
        ],
    )
    page = history(at(10), at(10, 30), hexes=[" abc123", OTHER_HEX.upper()])
    assert [row["lat"] for row in page.rows] == [1.0, 2.0, 3.0, 6.0]
    assert page.rows[0] == {
        "hex": "abc123",
        "ts": 1792317659.3,
        "lat": 1.0,
        "lon": -82.39,
        "alt_baro": 10000,
        "gs": 194.4,
        "track": 90.0,
        "position_source": "multinode_solve",
        "multinode": True,
        "n_nodes": 2,
        "pos_sigma_m": 2100.0,
        "node_refs": [refs[PUB_A], refs[PUB_B]],
        "is_anomalous": False,
    }
    assert [row["hex"] for row in page.rows[1:]] == [OTHER_HEX, "def456", OTHER_HEX]
    assert ("alt_baro" in page.rows[3], "gs" in page.rows[3], "track" in page.rows[3]) == (False, False, False)
    assert page.next_cursor is None


def test_a_span_over_the_cap_is_refused_naming_the_cap(root, refs):
    write_new(root, at(10, 31), [rec(at(10, 1), lat=1.0)])
    with pytest.raises(track_history.SpanTooLong, match="at most 86400 seconds"):
        history(at(10), at(10, day=19).replace(microsecond=1000), bbox=BOX)
    assert [row["lat"] for row in history(at(10), at(10, day=19), bbox=BOX).rows] == [1.0]
