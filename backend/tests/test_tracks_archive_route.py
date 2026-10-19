"""routes/tracks_archive.py: the public tracks archive, read back over HTTP.

Files are written by the archive's own writer (services/track_writer.py) and,
for the files written before its newer columns existed, in that older 14-column
schema through the same batch publisher, into a per-test root that
services.tasks.track_archive._TRACKS_DIR is pointed at. Node rows with minted
refs stand behind the ids, so the publication rules run against the real
policy and identity caches.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from core import state
from core.nodes import Node
from core.users import async_session_maker
from main import app
from routes.tracks_archive import ArchiveTracks, ArchiveTracksAvailability
from services import node_auth, node_refs, publication, tracks_store
from services.parquet_io import write_parquet_batch
from services.tasks import solver
from services.tasks import track_archive as ta
from services.track_writer import SCHEMA, _flatten, _group_by_node_kind, write_tracks_parquet

PUB_A, PUB_B = "ret1a2b3c4d", "ret2b3c4d5e"
PRIV = "ret3a4b5c6d"
# A real id with no registration and no mirrored ref: it has no published identity.
NO_REF = "ret5e6f7a8b"
# A polled radar with a mirrored ref but no graduated row: on probation.
POLLED = "bla0e0e0e0e"
# A real node mirrored from another environment: no Node row, only the ref it
# arrived with in state.connected_nodes, which a restart empties.
MIRRORED = "ret9c0d1e2f"
SYN_A, SYN_B = "synth-alpha", "synth-beta"

# The schema before track_key and the columns after it.
OLD_SCHEMA = pa.schema(
    [
        ("solve_ts_ms", pa.int64()),
        ("frame_ts_ms", pa.int64()),
        ("lat", pa.float64()),
        ("lon", pa.float64()),
        ("alt_m", pa.float64()),
        ("vel_east_ms", pa.float64()),
        ("vel_north_ms", pa.float64()),
        ("vel_up_ms", pa.float64()),
        ("n_nodes", pa.int32()),
        ("contributing_node_ids", pa.string()),
        ("adsb_hex", pa.string()),
        ("rms_delay_us", pa.float64()),
        ("rms_doppler_hz", pa.float64()),
        ("target_class", pa.string()),
    ]
)

ROW_KEYS = [
    "frame_ts_ms",
    "solve_ts_ms",
    "track_key",
    "lane",
    "node_kind",
    "lat",
    "lon",
    "alt_m",
    "vel_east_ms",
    "vel_north_ms",
    "vel_up_ms",
    "n_nodes",
    "node_refs",
    "adsb_hex",
    "pos_sigma_m",
    "rms_delay_us",
    "rms_doppler_hz",
    "is_anomalous",
    "anomaly_types",
]
NEW_COLUMNS = ["track_key", "lane", "pos_sigma_m", "is_anomalous"]


def at(hour: int, minute: int = 0, second: int = 0, ms: int = 0, day: int = 18) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, ms * 1000, tzinfo=UTC)


def ms_of(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ── The world ────────────────────────────────────────────────────────────────


def _seed() -> dict[str, str]:
    """PUB_A and PUB_B public and PRIV private, each with a minted ref."""
    nodes = {PUB_A: "public", PUB_B: "public", PRIV: "private"}
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


def rec(solve: datetime, ids=(PUB_A, PUB_B), *, frame: datetime | None = None, lat=34.85, lon=-82.39, **extra) -> dict:
    """A record as the solver archives it; lat marks the row."""
    return {
        "solve_ts_ms": ms_of(solve),
        "timestamp_ms": ms_of(frame if frame is not None else solve - timedelta(milliseconds=700)),
        "lat": lat,
        "lon": lon,
        "alt_m": 2310.0,
        "vel_east": 61.2,
        "vel_north": -108.4,
        "vel_up": 0.0,
        "n_nodes": len(ids),
        "contributing_node_ids": list(ids),
        "adsb_hex": None,
        "rms_delay": 0.41,
        "rms_doppler": 3.2,
        "track_key": "mn-dark-1792317600000-34.850--82.390",
        **extra,
    }


def write_new(root: Path, flushed: datetime, records: list[dict]) -> Path:
    key = write_tracks_parquet(records=records, base_dir=root, write_ts=flushed)
    assert key is not None
    return root / key


def stamped(ids) -> list[str]:
    """contributing_node_refs as the append sites stamp them, from the identities as they are now."""
    return [node_refs.owner_identity(n) or "" for n in ids]


def write_21(root: Path, flushed: datetime, records: list[dict]) -> Path:
    """A file as the writer wrote it before contributing_node_refs: 21 columns, one row group per node_kind."""
    table, sizes = _group_by_node_kind(pa.table(_flatten(records), schema=SCHEMA))
    table = table.drop_columns(["contributing_node_refs"])
    directory = partition(root, flushed)
    return directory / write_parquet_batch(table, directory, flushed, row_group_sizes=sizes)


def connect_mirrored(monkeypatch) -> str:
    """MIRRORED connected with a mirrored ref, as the detection mirror leaves it; the ref."""
    ref = node_auth.mint_node_ref()
    monkeypatch.setitem(state.connected_nodes, MIRRORED, {"node_ref": ref, "status": "connected"})
    node_refs._reset_for_tests()
    return ref


def disconnect_mirrored(monkeypatch) -> None:
    """MIRRORED gone from connected_nodes, as after a restart, and nothing cached from before."""
    monkeypatch.delitem(state.connected_nodes, MIRRORED)
    node_refs._reset_for_tests()
    publication._reset_for_tests()
    tracks_store._reset_for_tests()
    assert node_refs.public_identity(MIRRORED) is None


def register_public(node_id: str) -> str:
    """``node_id`` registered public with a minted ref, and the ref map reloaded, as its TTL would; the ref."""
    ref = node_auth.mint_node_ref()

    async def _go():
        async with async_session_maker() as session:
            session.add(Node(node_id=node_id, node_ref=ref, publication="public"))
            await session.commit()

    asyncio.run(_go())
    node_refs._reset_for_tests()
    return ref


def old(solve: datetime, ids: str, *, lat=34.85, lon=-82.39, frame: datetime | None = None) -> dict:
    """A row in the older schema; ``ids`` comma-joined as it was written."""
    return {
        "solve_ts_ms": ms_of(solve),
        "frame_ts_ms": ms_of(frame if frame is not None else solve - timedelta(milliseconds=700)),
        "lat": lat,
        "lon": lon,
        "alt_m": 1000.0,
        "vel_east_ms": 1.0,
        "vel_north_ms": 2.0,
        "vel_up_ms": 0.0,
        "n_nodes": len([i for i in ids.split(",") if i]),
        "contributing_node_ids": ids,
        "adsb_hex": "a1b2c3",
        "rms_delay_us": 0.5,
        "rms_doppler_hz": 2.0,
        "target_class": None,
    }


def partition(root: Path, flushed: datetime) -> Path:
    return root / f"year={flushed:%Y}" / f"month={flushed:%m}" / f"day={flushed:%d}"


def write_old(root: Path, flushed: datetime, rows: list[dict]) -> Path:
    table = pa.table({f.name: [r[f.name] for r in rows] for f in OLD_SCHEMA}, schema=OLD_SCHEMA)
    directory = partition(root, flushed)
    return directory / write_parquet_batch(table, directory, flushed)


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


@pytest.fixture
def api():
    """The real app without its lifespan: these routes need none of it."""
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    client.close()


@pytest.fixture
def opened(monkeypatch):
    """The names of the Parquet files opened, in order."""
    names: list[str] = []
    original = pq.ParquetFile

    class Recording(original):
        def __init__(self, source, *args, **kwargs):
            names.append(Path(source).name)
            super().__init__(source, *args, **kwargs)

    monkeypatch.setattr(pq, "ParquetFile", Recording)
    return names


def tracks(api, start: datetime, end: datetime, **params):
    return api.get("/api/data/tracks", params={"from": iso(start), "to": iso(end), **params})


def lats(response) -> list[float]:
    assert response.status_code == 200, response.text
    return [row["lat"] for row in response.json()["rows"]]


# ── Parameters ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-10-18T10:00:00Z", "2026-10-18T10:30:00Z"),
        ("2026-10-18T12:00:00+02:00", "2026-10-18T05:30:00-05:00"),
        ("2026-10-18T10:00:00", "2026-10-18T10:30:00"),
        (str(ms_of(at(10))), str(ms_of(at(10, 30)))),
        (str(ms_of(at(10))), "2026-10-18T10:30:00.000Z"),
    ],
    ids=["z", "offset", "naive-is-utc", "epoch-ms", "mixed"],
)
def test_from_and_to_parse_as_epoch_ms_or_iso(api, root, refs, start, end):
    write_new(root, at(10, 31), [rec(at(9, 59, 59, 999), lat=1.0), rec(at(10), lat=2.0), rec(at(10, 30), lat=3.0)])
    response = api.get("/api/data/tracks", params={"from": start, "to": end})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["from"], body["to"]) == (ms_of(at(10)), ms_of(at(10, 30)))
    assert [row["lat"] for row in body["rows"]] == [2.0]


def test_a_bare_date_is_its_utc_midnight(api, root, refs):
    response = api.get("/api/data/tracks", params={"from": "2026-10-18", "to": "2026-10-18T00:30:00Z"})
    assert response.status_code == 200, response.text
    assert response.json()["from"] == ms_of(at(0))


def test_a_sub_millisecond_instant_rounds_up(api, root, refs):
    response = api.get("/api/data/tracks", params={"from": "2026-10-18T10:00:00.000400Z", "to": "2026-10-18T10:01:00Z"})
    assert response.json()["from"] == ms_of(at(10)) + 1


def test_the_window_is_capped_at_60_minutes(api, root, refs):
    write_new(root, at(11, 1), [rec(at(10, 0, 0, 1), lat=1.0), rec(at(10, 59, 59, 999), lat=2.0)])
    sixty = tracks(api, at(10), at(11))
    assert lats(sixty) == [1.0, 2.0]
    over = tracks(api, at(10), at(11, 1))
    assert over.status_code == 422
    assert "60-minute cap" in over.json()["detail"]
    assert tracks(api, at(10), at(11, 0, 0, 1)).status_code == 422


@pytest.mark.parametrize(("start", "end"), [(at(10), at(10)), (at(10, 1), at(10))], ids=["equal", "reversed"])
def test_to_must_be_after_from(api, root, refs, start, end):
    response = tracks(api, start, end)
    assert response.status_code == 422
    assert response.json()["detail"] == "to must be after from"


@pytest.mark.parametrize(
    "value", ["yesterday", "", "2026-13-01T00:00:00Z", "2026-10-18T25:00:00Z", "1e12", "99999999999999999"]
)
def test_an_unparseable_from_is_422(api, root, refs, value):
    response = api.get("/api/data/tracks", params={"from": value, "to": "2026-10-18T10:00:00Z"})
    assert response.status_code == 422
    assert response.json()["detail"].startswith("from must be")


def test_from_and_to_are_required(api, root, refs):
    for params, missing in (({"from": "2026-10-18T10:00:00Z"}, "to"), ({"to": "2026-10-18T10:00:00Z"}, "from")):
        response = api.get("/api/data/tracks", params=params)
        assert response.status_code == 422
        [error] = response.json()["detail"]
        assert (error["type"], error["loc"]) == ("missing", ["query", missing])


def test_bbox_keeps_the_rows_inside_it(api, root, refs):
    write_new(
        root,
        at(10, 5),
        [rec(at(10, 1), lat=34.0, lon=-82.0), rec(at(10, 2), lat=40.0, lon=-82.0), rec(at(10, 3), lat=34.0, lon=-90.0)],
    )
    assert lats(tracks(api, at(10), at(10, 30), bbox="33.5,-83,34.5,-81")) == [34.0]
    # Edges are inside.
    assert lats(tracks(api, at(10), at(10, 30), bbox="34,-82,34,-82")) == [34.0]


def test_a_bbox_whose_west_is_east_of_its_east_crosses_the_antimeridian(api, root, refs):
    write_new(
        root,
        at(10, 5),
        [
            rec(at(10, 1), lat=1.0, lon=175.0),
            rec(at(10, 2), lat=2.0, lon=-175.0),
            rec(at(10, 3), lat=3.0, lon=0.0),
            rec(at(10, 4), lat=4.0, lon=180.0),
        ],
    )
    assert lats(tracks(api, at(10), at(10, 30), bbox="-10,170,10,-170")) == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    "bbox",
    [
        "",
        "1,2,3",
        "1,2,3,4,5",
        "a,b,c,d",
        "10,0,5,1",
        "-91,0,0,1",
        "0,0,91,1",
        "0,-181,1,0",
        "0,0,1,181",
        "nan,0,1,1",
        "0,inf,1,1",
        "1e999,0,1,1",
        "0x1,0,1,1",
        "1_0,0,20,1",
    ],
)
def test_an_invalid_bbox_is_422(api, root, refs, bbox):
    response = tracks(api, at(10), at(10, 30), bbox=bbox)
    assert response.status_code == 422
    assert response.json()["detail"].startswith("bbox must be")


# ── Fleets and the older schema ──────────────────────────────────────────────


def _fleets(root: Path) -> None:
    """One new-schema file with a row of each node kind, and a legacy one."""
    write_new(
        root,
        at(10, 5),
        [
            rec(at(10, 1), (PUB_A, PUB_B), lat=1.0),
            rec(at(10, 2), (SYN_A, SYN_B), lat=2.0),
            rec(at(10, 3), (PUB_A, SYN_A), lat=3.0),
            rec(at(10, 4), (), lat=4.0),
        ],
    )
    write_old(
        root,
        at(10, 10),
        [
            old(at(10, 6), f"{PUB_A},{PUB_B}", lat=11.0),
            old(at(10, 7), f"{SYN_A},{SYN_B}", lat=12.0),
            old(at(10, 8), "e2e-node-1,test-node-2", lat=13.0),
            old(at(10, 9), f"{PUB_B},{SYN_B}", lat=14.0),
            old(at(10, 9, 30), "", lat=15.0),
        ],
    )


@pytest.mark.parametrize(
    ("fleet", "expected"),
    [
        (None, [1.0, 11.0]),
        ("real", [1.0, 11.0]),
        ("synthetic", [2.0, 12.0, 13.0]),
        ("all", [1.0, 2.0, 3.0, 4.0, 11.0, 12.0, 13.0, 14.0, 15.0]),
    ],
    ids=["default", "real", "synthetic", "all"],
)
def test_fleet_filters_on_node_kind_with_the_id_prefix_fallback_for_old_files(api, root, refs, fleet, expected):
    _fleets(root)
    params = {"fleet": fleet} if fleet else {}
    response = tracks(api, at(10), at(10, 30), **params)
    assert lats(response) == expected
    assert response.json()["fleet"] == (fleet or "real")


def test_each_row_says_its_kind_and_old_files_derive_it(api, root, refs):
    _fleets(root)
    kinds = {row["lat"]: row["node_kind"] for row in tracks(api, at(10), at(10, 30), fleet="all").json()["rows"]}
    assert kinds == {
        1.0: "real",
        2.0: "synthetic",
        3.0: "mixed",
        4.0: None,
        11.0: "real",
        12.0: "synthetic",
        13.0: "synthetic",
        14.0: "mixed",
        15.0: None,
    }


def test_an_unknown_fleet_is_422(api, root, refs):
    assert tracks(api, at(10), at(10, 30), fleet="mixed").status_code == 422
    assert tracks(api, at(10), at(10, 30), fleet="REAL").status_code == 422


def test_old_files_read_with_the_newer_columns_null(api, root, refs):
    write_old(root, at(10, 10), [old(at(10, 6), f"{PUB_A},{PUB_B}", lat=11.0)])
    [row] = tracks(api, at(10), at(10, 30)).json()["rows"]
    assert list(row) == ROW_KEYS
    assert {key: row[key] for key in NEW_COLUMNS} == dict.fromkeys(NEW_COLUMNS)
    assert row["anomaly_types"] == []
    assert row["node_kind"] == "real"
    assert row["node_refs"] == [refs[PUB_A], refs[PUB_B]]
    assert (row["n_nodes"], row["adsb_hex"], row["alt_m"], row["rms_delay_us"]) == (2, "a1b2c3", 1000.0, 0.5)


def test_one_window_reads_old_and_new_files_together(api, root, refs):
    write_old(root, at(10, 2), [old(at(10, 1), PUB_A, lat=1.0), old(at(10, 1, 30), PUB_A, lat=3.0)])
    write_new(root, at(10, 3), [rec(at(10, 1, 15), (PUB_B,), lat=2.0, is_anomalous=True, anomaly_types=["x"])])
    rows = tracks(api, at(10), at(10, 30)).json()["rows"]
    assert [row["lat"] for row in rows] == [1.0, 2.0, 3.0]
    assert [row["is_anomalous"] for row in rows] == [None, True, None]
    assert [row["track_key"] is None for row in rows] == [True, False, True]


def test_a_new_row_carries_every_column(api, root, refs):
    write_new(
        root,
        at(10, 5),
        [
            rec(
                at(10, 1),
                (PUB_A, PUB_B, PUB_A),
                frame=at(10, 0, 59, 300),
                track_key="mn-adsb-a1b2c3",
                adsb_hex="a1b2c3",
                is_anomalous=False,
                anomaly_types=[],
                solve_count=7,
                target_class="aircraft",
                alt_m=float("nan"),
                vel_up=float("inf"),
            )
        ],
    )
    response = tracks(api, at(10), at(10, 30))
    [row] = response.json()["rows"]
    assert list(row) == ROW_KEYS
    assert row["frame_ts_ms"] == ms_of(at(10, 0, 59, 300))
    assert row["solve_ts_ms"] == ms_of(at(10, 1))
    assert (row["track_key"], row["lane"], row["adsb_hex"]) == ("mn-adsb-a1b2c3", "known", "a1b2c3")
    assert row["node_refs"] == [refs[PUB_A], refs[PUB_B], refs[PUB_A]]
    assert (row["is_anomalous"], row["anomaly_types"]) == (False, [])
    assert (row["alt_m"], row["vel_up_ms"]) == (None, None)
    assert isinstance(row["pos_sigma_m"], float)
    for never in ("contributing_node_ids", "target_class", "solve_count", "aircraft"):
        assert never not in response.text


def test_anomaly_types_split_on_commas(api, root, refs):
    write_new(
        root,
        at(10, 5),
        [
            rec(at(10, 1), lat=1.0, is_anomalous=True, anomaly_types=["speed", "altitude"]),
            rec(at(10, 2), lat=2.0, is_anomalous=True, anomaly_types="speed,,altitude, "),
        ],
    )
    assert [row["anomaly_types"] for row in tracks(api, at(10), at(10, 30)).json()["rows"]] == [
        ["speed", "altitude"],
        ["speed", "altitude"],
    ]


def test_a_row_without_a_finite_position_is_dropped(api, root, refs):
    write_new(
        root,
        at(10, 5),
        [rec(at(10, 1), lat=1.0), rec(at(10, 2), lat=float("nan")), rec(at(10, 3), lat=3.0, lon=float("inf"))],
    )
    assert lats(tracks(api, at(10), at(10, 30), fleet="all")) == [1.0]


# ── Publication ──────────────────────────────────────────────────────────────


def _publication_world(root: Path, monkeypatch) -> None:
    """A public row, then rows a private node, a node with no identity and a
    polled radar on probation contributed to, in new and old files."""
    monkeypatch.delenv("POLLED_RADAR_PROBATION_ENABLED", raising=False)
    # Mirrored from the environment that registered it, so it has an identity
    # and only probation can withhold it.
    monkeypatch.setitem(state.connected_nodes, POLLED, {"node_ref": node_auth.mint_node_ref()})
    write_new(
        root,
        at(10, 5),
        [
            rec(at(10, 1), (PUB_A, PUB_B), lat=1.0),
            rec(at(10, 2), (PUB_A, PRIV), lat=2.0),
            rec(at(10, 3), (PUB_B, NO_REF), lat=3.0),
            rec(at(10, 4), (PUB_A, POLLED), lat=4.0),
            rec(at(10, 4, 30), (PRIV, SYN_A), lat=5.0),
        ],
    )
    write_old(
        root,
        at(10, 10),
        [
            old(at(10, 6), f"{PRIV},{PUB_B}", lat=6.0),
            old(at(10, 7), f"{PUB_B},{PUB_A},", lat=7.0),
            old(at(10, 8), f"{NO_REF}", lat=8.0),
        ],
    )


def test_rows_a_private_node_contributed_to_are_dropped(api, root, refs, monkeypatch):
    _publication_world(root, monkeypatch)
    assert lats(tracks(api, at(10), at(10, 30), fleet="all")) == [1.0, 7.0]


def test_a_probation_node_with_a_published_identity_is_still_withheld(api, root, refs, monkeypatch):
    _publication_world(root, monkeypatch)
    assert 4.0 not in lats(tracks(api, at(10), at(10, 30)))
    # The control: with the fence off the same row is published, under its mirrored ref.
    monkeypatch.setenv("POLLED_RADAR_PROBATION_ENABLED", "0")
    rows = {row["lat"]: row for row in tracks(api, at(10), at(10, 30)).json()["rows"]}
    assert rows[4.0]["node_refs"] == [refs[PUB_A], state.connected_nodes[POLLED]["node_ref"]]


def test_contributors_go_out_as_their_minted_refs_in_order(api, root, refs, monkeypatch):
    _publication_world(root, monkeypatch)
    rows = tracks(api, at(10), at(10, 30)).json()["rows"]
    assert [row["node_refs"] for row in rows] == [[refs[PUB_A], refs[PUB_B]], [refs[PUB_B], refs[PUB_A]]]


@pytest.mark.parametrize("fleet", ["real", "synthetic", "all"])
def test_no_private_id_appears_anywhere_in_the_body(api, root, refs, monkeypatch, fleet):
    _publication_world(root, monkeypatch)
    write_new(root, at(10, 12), [rec(at(10, 11), (SYN_A, SYN_B), lat=9.0), rec(at(10, 11, 30), (PUB_A, SYN_B))])
    body = tracks(api, at(10), at(10, 30), fleet=fleet).text
    assert json.loads(body)["count"] > 0
    assert not re.search(r"ret[0-9a-f]{8}", body)
    assert not re.search(r"bla[0-9a-f]{8}", body)
    assert refs[PRIV] not in body
    if fleet == "real":
        assert "synth-" not in body


def test_an_id_holding_a_comma_never_goes_out_under_fleet_real(api, root, refs):
    """The writer classifies the string it stores, so a synthetic part behind a
    comma inside one id makes the row mixed, not real."""
    write_new(root, at(10, 5), [rec(at(10, 1), (f"{PUB_A},synth-x", PUB_B), lat=1.0)])
    real = tracks(api, at(10), at(10, 30), fleet="real")
    assert lats(real) == []
    assert "synth-" not in real.text
    assert lats(tracks(api, at(10), at(10, 30), fleet="synthetic")) == []
    [row] = tracks(api, at(10), at(10, 30), fleet="all").json()["rows"]
    assert (row["node_kind"], row["node_refs"]) == ("mixed", [refs[PUB_A], "synth-x", refs[PUB_B]])


@pytest.mark.parametrize(("fleet", "published"), [("real", [1.0]), ("synthetic", [3.0]), ("all", [1.0, 3.0])])
def test_a_padded_id_is_not_the_registered_node_it_resembles(api, root, refs, fleet, published):
    """Ids are looked up exactly as stored, as the live feed looks them up: a
    registered id with a space around it has no identity and drops its row."""
    write_new(
        root,
        at(10, 5),
        [
            rec(at(10, 1), (PUB_B,), lat=1.0),
            rec(at(10, 2), (f"{PUB_A} ", PUB_B), lat=2.0),
            rec(at(10, 3), (SYN_A,), lat=3.0),
            rec(at(10, 4), (f"{PUB_A} ", SYN_A), lat=4.0),
        ],
    )
    write_old(root, at(10, 10), [old(at(10, 6), f" {PUB_A},{PUB_B}", lat=6.0), old(at(10, 7), f"{PUB_A} ", lat=7.0)])
    response = tracks(api, at(10), at(10, 30), fleet=fleet)
    assert lats(response) == published
    assert refs[PUB_A] not in response.text


def test_an_unknown_policy_is_a_503(api, root, refs, monkeypatch):
    write_new(root, at(10, 5), [rec(at(10, 1))])
    publication._reset_for_tests()

    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(publication, "_query", unavailable)
    response = tracks(api, at(10), at(10, 30))
    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json() == {"detail": "Publication policy is temporarily unavailable"}


# ── Refs stamped at publish time ────────────────────────────────────────────


def test_a_mirrored_nodes_solve_is_served_after_it_disconnects(api, root, refs, monkeypatch):
    """A real node mirrored from production (test and staging) has no Node row,
    only a ref in connected_nodes. Its solve, archived through the solver while
    it was connected, is still served under that ref once it is gone."""
    mirrored_ref = connect_mirrored(monkeypatch)
    monkeypatch.setattr(ta, "_pending_records", [])
    ids = [PUB_A, PUB_B, MIRRORED]

    def solve(s_in, node_cfgs):
        return {
            "success": True,
            "lat": 34.85,
            "lon": -82.39,
            "alt_m": 2310.0,
            "vel_east": 61.2,
            "vel_north": -108.4,
            "n_nodes": len(ids),
            "contributing_node_ids": list(ids),
            "timestamp_ms": int(time.time() * 1000) - 700,
            "rms_delay": 0.41,
            "rms_doppler": 3.2,
        }

    start = int(time.time() * 1000)
    solver._process_solver_item(({"n_nodes": len(ids), "candidates": []}, {}, time.time()), solve)
    assert ta.flush_track_archive_buffer() is not None
    window = {"from": str(start - 60_000), "to": str(int(time.time() * 1000) + 60_000)}

    [row] = api.get("/api/data/tracks", params=window).json()["rows"]
    assert row["node_refs"] == [refs[PUB_A], refs[PUB_B], mirrored_ref]

    disconnect_mirrored(monkeypatch)
    response = api.get("/api/data/tracks", params=window)
    [row] = response.json()["rows"]
    assert row["node_refs"] == [refs[PUB_A], refs[PUB_B], mirrored_ref]
    assert MIRRORED not in response.text
    assert "contributing_node_refs" not in response.text


@pytest.mark.parametrize("fleet", ["real", "synthetic", "all"])
def test_a_private_contributor_drops_its_row_whatever_ref_it_was_stamped_with(api, root, refs, monkeypatch, fleet):
    """Today's private set, checked by stored id, decides before the stored
    refs are read; a private node's ref is in the file and in no answer."""
    monkeypatch.delenv("POLLED_RADAR_PROBATION_ENABLED", raising=False)
    monkeypatch.setitem(state.connected_nodes, POLLED, {"node_ref": node_auth.mint_node_ref()})
    rows = [
        ((PUB_A, PRIV), 1.0),
        ((PRIV, SYN_A), 2.0),
        ((PUB_A, POLLED), 3.0),
        ((PUB_A, PUB_B), 4.0),
        ((SYN_A, SYN_B), 5.0),
    ]
    path = write_new(
        root,
        at(10, 5),
        [rec(at(10, 1, i), ids, lat=lat, contributing_node_refs=stamped(ids)) for i, (ids, lat) in enumerate(rows)],
    )
    on_disk = ",".join(pq.read_table(path).column("contributing_node_refs").to_pylist())
    assert refs[PRIV] in on_disk and state.connected_nodes[POLLED]["node_ref"] in on_disk

    response = tracks(api, at(10), at(10, 30), fleet=fleet)
    assert lats(response) == {"real": [4.0], "synthetic": [5.0], "all": [4.0, 5.0]}[fleet]
    body = response.text
    assert refs[PRIV] not in body
    assert state.connected_nodes[POLLED]["node_ref"] not in body
    assert not re.search(r"ret[0-9a-f]{8}|bla[0-9a-f]{8}", body)
    assert "contributing_node_refs" not in body


def test_stored_refs_that_do_not_line_up_with_the_ids_drop_the_row(api, root, refs):
    a, b = refs[PUB_A], refs[PUB_B]
    comma_syn, comma_real, padded = (f"{SYN_A},{NO_REF}", PUB_B), (f"{PUB_A},synth-x", PUB_B), (f"{PUB_A} ", PUB_B)
    rows = [
        ((PUB_A, PUB_B), f"{a},{b}", 1.0),
        # An empty slot lines up with its id, which is resolved now.
        ((PUB_A, PUB_B), f",{b}", 2.0),
        ((PUB_A, PUB_B), f"{a},", 3.0),
        ((PUB_A, PUB_B), a, 4.0),
        ((PUB_A, PUB_B), f"{a},{b},{a}", 5.0),
        ((PUB_A, PUB_B), "", 6.0),
        # A real id where its ref belongs.
        ((PUB_A, PUB_B), f"{PUB_A},{b}", 7.0),
        # A synthetic id holding a comma before a real id, stamped as it is
        # published: one slot per part, the real part as its own ref.
        (comma_syn, ",".join(stamped(comma_syn)), 8.0),
        # A synthetic id is published as itself, not under another name.
        ((SYN_A, PUB_B), f"synth-other,{b}", 9.0),
        # The F1 ids, stamped: neither has an identity of its own.
        (comma_real, ",".join(stamped(comma_real)), 10.0),
        (padded, ",".join(stamped(padded)), 11.0),
        # No contributors and no refs.
        ((), "", 12.0),
    ]
    assert stamped(comma_syn) == [f"{SYN_A},{NO_REF}", b]
    assert stamped(comma_real) == stamped(padded) == ["", b]
    write_new(
        root,
        at(10, 5),
        [rec(at(10, 1, i), ids, lat=lat, contributing_node_refs=stored) for i, (ids, stored, lat) in enumerate(rows)],
    )
    response = tracks(api, at(10), at(10, 30), fleet="all")
    assert [(row["lat"], row["node_refs"]) for row in response.json()["rows"]] == [
        (1.0, [a, b]),
        (2.0, [a, b]),
        (3.0, [a, b]),
        (12.0, []),
    ]
    assert not re.search(r"ret[0-9a-f]{8}", response.text)
    assert "synth-" not in response.text


def test_rows_without_stored_refs_resolve_their_ids_when_read(api, root, refs, monkeypatch):
    """A null column, or a file from before it, is read exactly as before: each
    id resolved now. The known limit: a mirrored node in such a row is served
    only while it is connected."""
    mirrored_ref = connect_mirrored(monkeypatch)
    a, b = refs[PUB_A], refs[PUB_B]
    new = write_new(
        root,
        at(10, 5),
        [
            rec(at(10, 1), (PUB_A, MIRRORED), lat=1.0),
            rec(at(10, 2), (PUB_B, NO_REF), lat=2.0),
            rec(at(10, 3), (PUB_A, PUB_B), lat=3.0),
            rec(at(10, 4), (PUB_A, PRIV), lat=4.0),
        ],
    )
    assert pq.read_table(new).column("contributing_node_refs").null_count == 4
    write_old(
        root,
        at(10, 10),
        [
            old(at(10, 6), f"{PUB_B},{MIRRORED}", lat=6.0),
            old(at(10, 7), NO_REF, lat=7.0),
            old(at(10, 8), f"{PUB_A},{PUB_B}", lat=8.0),
        ],
    )

    def published():
        return [(row["lat"], row["node_refs"]) for row in tracks(api, at(10), at(10, 30)).json()["rows"]]

    assert published() == [(1.0, [a, mirrored_ref]), (3.0, [a, b]), (6.0, [b, mirrored_ref]), (8.0, [a, b])]
    disconnect_mirrored(monkeypatch)
    assert published() == [(3.0, [a, b]), (8.0, [a, b])]


def test_slots_stamped_while_the_registry_was_cold_are_resolved_when_read(api, root, refs, monkeypatch):
    """The ref map cannot be read at boot, so registered contributors are
    stamped with empty slots. Once it reads again those slots resolve; a
    mirrored ref stamped beside them is served as stored, after its node
    disconnects too."""
    mirrored_ref = connect_mirrored(monkeypatch)
    a, b = refs[PUB_A], refs[PUB_B]
    load = node_refs._load

    def locked():
        raise RuntimeError("database is locked")

    monkeypatch.setattr(node_refs, "_load", locked)
    node_refs._reset_for_tests()
    registered, with_mirrored = (PUB_A, PUB_B), (PUB_A, PUB_B, MIRRORED)
    cold = stamped(registered), stamped(with_mirrored)
    assert cold == (["", ""], ["", "", mirrored_ref])
    monkeypatch.setattr(node_refs, "_load", load)
    node_refs._reset_for_tests()

    write_new(
        root,
        at(10, 5),
        [
            rec(at(10, 1), registered, lat=1.0, contributing_node_refs=cold[0]),
            rec(at(10, 2), with_mirrored, lat=2.0, contributing_node_refs=cold[1]),
        ],
    )

    def published():
        return [(row["lat"], row["node_refs"]) for row in tracks(api, at(10), at(10, 30)).json()["rows"]]

    assert published() == [(1.0, [a, b]), (2.0, [a, b, mirrored_ref])]
    disconnect_mirrored(monkeypatch)
    assert published() == [(1.0, [a, b]), (2.0, [a, b, mirrored_ref])]


def test_a_node_registered_after_its_solve_is_served_under_its_ref(api, root, refs):
    """NO_REF contributes before it has a Node row, so it is stamped with an
    empty slot, and its rows are withheld until it registers. Alone it is
    stamped [""], which the writer stores as "", as it stores no slots."""
    a, b = refs[PUB_A], refs[PUB_B]
    shared, alone = (PUB_A, PUB_B, NO_REF), (NO_REF,)
    assert (stamped(shared), stamped(alone)) == ([a, b, ""], [""])
    path = write_new(
        root,
        at(10, 5),
        [
            rec(at(10, 1), shared, lat=1.0, contributing_node_refs=stamped(shared)),
            rec(at(10, 2), alone, lat=2.0, contributing_node_refs=stamped(alone)),
        ],
    )
    assert sorted(pq.read_table(path).column("contributing_node_refs").to_pylist()) == ["", f"{a},{b},"]
    assert lats(tracks(api, at(10), at(10, 30))) == []

    late = register_public(NO_REF)
    response = tracks(api, at(10), at(10, 30))
    assert [(row["lat"], row["node_refs"]) for row in response.json()["rows"]] == [(1.0, [a, b, late]), (2.0, [late])]
    assert NO_REF not in response.text


def test_an_empty_slot_for_a_private_or_probation_node_still_drops_its_row(api, root, refs, monkeypatch):
    """Today's private set is asked by id before any slot is resolved. Resolved,
    these slots would name the ref each node has now."""
    monkeypatch.delenv("POLLED_RADAR_PROBATION_ENABLED", raising=False)
    polled_ref = node_auth.mint_node_ref()
    monkeypatch.setitem(state.connected_nodes, POLLED, {"node_ref": polled_ref})
    assert (node_refs.public_identity(PRIV), node_refs.public_identity(POLLED)) == (refs[PRIV], polled_ref)
    a, b = refs[PUB_A], refs[PUB_B]
    write_new(
        root,
        at(10, 5),
        [
            rec(at(10, 1), (PUB_A, PRIV), lat=1.0, contributing_node_refs=[a, ""]),
            rec(at(10, 2), (PRIV, SYN_A), lat=2.0, contributing_node_refs=["", SYN_A]),
            rec(at(10, 3), (PUB_A, POLLED), lat=3.0, contributing_node_refs=[a, ""]),
            rec(at(10, 4), (PUB_A, PUB_B), lat=4.0, contributing_node_refs=[a, ""]),
        ],
    )
    response = tracks(api, at(10), at(10, 30), fleet="all")
    assert [(row["lat"], row["node_refs"]) for row in response.json()["rows"]] == [(4.0, [a, b])]
    body = response.text
    assert refs[PRIV] not in body
    assert polled_ref not in body
    assert not re.search(r"ret[0-9a-f]{8}|bla[0-9a-f]{8}", body)


def test_an_empty_slot_that_still_has_no_identity_drops_its_row(api, root, refs):
    """NO_REF has no Node row and no mirrored ref, now as when it was stamped."""
    a, b = refs[PUB_A], refs[PUB_B]
    write_new(
        root,
        at(10, 5),
        [
            rec(at(10, 1), (PUB_A, NO_REF), lat=1.0, contributing_node_refs=stamped((PUB_A, NO_REF))),
            rec(at(10, 2), (NO_REF,), lat=2.0, contributing_node_refs=stamped((NO_REF,))),
            rec(at(10, 3), (NO_REF, SYN_A), lat=3.0, contributing_node_refs=stamped((NO_REF, SYN_A))),
            rec(at(10, 4), (PUB_A, PUB_B), lat=4.0, contributing_node_refs=["", b]),
        ],
    )
    response = tracks(api, at(10), at(10, 30), fleet="all")
    assert [(row["lat"], row["node_refs"]) for row in response.json()["rows"]] == [(4.0, [a, b])]
    assert not re.search(r"ret[0-9a-f]{8}", response.text)
    assert SYN_A not in response.text


def test_files_with_14_21_and_22_columns_read_together(api, root, refs, monkeypatch):
    mirrored_ref = connect_mirrored(monkeypatch)
    a, b = refs[PUB_A], refs[PUB_B]
    legacy = write_old(
        root, at(10, 3), [old(at(10, 1), f"{PUB_A},{MIRRORED}", lat=1.0), old(at(10, 2), PUB_A, lat=2.0)]
    )
    before = write_21(
        root,
        at(10, 6),
        [
            rec(at(10, 4), (PUB_A, MIRRORED), lat=4.0),
            rec(at(10, 5), (PUB_B,), lat=5.0),
            rec(at(10, 5, 30), (SYN_A,), lat=5.5),
        ],
    )
    after = write_new(
        root,
        at(10, 9),
        [
            rec(at(10, 7), (PUB_A, MIRRORED), lat=7.0, contributing_node_refs=stamped((PUB_A, MIRRORED))),
            rec(at(10, 8), (PUB_B,), lat=8.0, contributing_node_refs=stamped((PUB_B,))),
        ],
    )
    assert [pq.read_schema(p).names for p in (legacy, before, after)] == [
        OLD_SCHEMA.names,
        SCHEMA.names[:21],
        SCHEMA.names,
    ]
    assert SCHEMA.names[21] == "contributing_node_refs"

    def published(fleet="real"):
        return [(row["lat"], row["node_refs"]) for row in tracks(api, at(10), at(10, 30), fleet=fleet).json()["rows"]]

    assert published() == [
        (1.0, [a, mirrored_ref]),
        (2.0, [a]),
        (4.0, [a, mirrored_ref]),
        (5.0, [b]),
        (7.0, [a, mirrored_ref]),
        (8.0, [b]),
    ]
    disconnect_mirrored(monkeypatch)
    assert published() == [(2.0, [a]), (5.0, [b]), (7.0, [a, mirrored_ref]), (8.0, [b])]
    assert published("all") == [(2.0, [a]), (5.0, [b]), (5.5, [SYN_A]), (7.0, [a, mirrored_ref]), (8.0, [b])]
    # Availability is unchanged: footers only, every row counted.
    assert hours(availability(api, "2026-10-18", "2026-10-18")) == [(ms_of(at(10)), 4, 2)]
    assert hours(availability(api, "2026-10-18", "2026-10-18", fleet="all")) == [(ms_of(at(10)), 7, 0)]


# ── Order and the row cap ────────────────────────────────────────────────────


def test_rows_sort_by_frame_time_then_solve_time(api, root, refs):
    write_new(
        root,
        at(10, 6),
        [
            rec(at(10, 5, 2), frame=at(10, 5), lat=1.0),
            rec(at(10, 5, 1), frame=at(10, 4), lat=2.0),
        ],
    )
    write_new(root, at(10, 7), [rec(at(10, 5, 1, 500), frame=at(10, 5), lat=3.0)])
    write_old(root, at(10, 8), [old(at(10, 7, 59), PUB_A, frame=at(10, 4, 30), lat=4.0)])
    assert lats(tracks(api, at(10), at(10, 30))) == [2.0, 4.0, 3.0, 1.0]


def test_past_the_cap_the_first_rows_go_out_and_truncated_says_so(api, root, refs, monkeypatch):
    monkeypatch.setattr(tracks_store, "TRACKS_ROW_CAP", 3)
    # The private and synthetic rows sort first, and the cap counts after them.
    write_new(
        root,
        at(10, 10),
        [rec(at(10, 1), (PUB_A, PRIV), lat=-1.0), rec(at(10, 1, 30), (SYN_A,), lat=-2.0)]
        + [rec(at(10, 2 + i), lat=float(i)) for i in range(5)],
    )
    body = tracks(api, at(10), at(10, 30)).json()
    assert (body["count"], body["truncated"]) == (3, True)
    assert [row["lat"] for row in body["rows"]] == [0.0, 1.0, 2.0]


def test_exactly_the_cap_is_not_truncated(api, root, refs, monkeypatch):
    monkeypatch.setattr(tracks_store, "TRACKS_ROW_CAP", 3)
    write_new(root, at(10, 10), [rec(at(10, 2 + i), lat=float(i)) for i in range(3)])
    body = tracks(api, at(10), at(10, 30)).json()
    assert (body["count"], body["truncated"]) == (3, False)


def test_the_real_cap_is_50000_rows(api, root, refs):
    n = 50_001
    start = ms_of(at(10))
    table = pa.table(
        {
            "solve_ts_ms": pa.array([start + i for i in range(n)], pa.int64()),
            "frame_ts_ms": pa.array([start + i for i in range(n)], pa.int64()),
            "lat": pa.array([34.0] * n, pa.float64()),
            "lon": pa.array([-82.0] * n, pa.float64()),
            "alt_m": pa.nulls(n, pa.float64()),
            "vel_east_ms": pa.nulls(n, pa.float64()),
            "vel_north_ms": pa.nulls(n, pa.float64()),
            "vel_up_ms": pa.nulls(n, pa.float64()),
            "n_nodes": pa.array([1] * n, pa.int32()),
            "contributing_node_ids": pa.array([PUB_A] * n, pa.string()),
            "adsb_hex": pa.nulls(n, pa.string()),
            "rms_delay_us": pa.nulls(n, pa.float64()),
            "rms_doppler_hz": pa.nulls(n, pa.float64()),
            "target_class": pa.nulls(n, pa.string()),
        },
        schema=OLD_SCHEMA,
    )
    write_parquet_batch(table, partition(root, at(10, 2)), at(10, 2))
    body = tracks(api, at(10), at(10, 30)).json()
    assert (body["count"], body["truncated"], len(body["rows"])) == (50_000, True, 50_000)
    assert body["rows"][-1]["solve_ts_ms"] == start + 49_999


# ── Which files are read ─────────────────────────────────────────────────────


def test_temporaries_and_upload_sentinels_are_never_opened(api, root, refs, opened):
    good = write_new(root, at(10, 5), [rec(at(10, 1), lat=1.0)])
    # Both readable Parquet holding rows in the window, so reading either would show.
    tmp = good.parent / ".parquet-abc123.tmp"
    sentinel = good.parent / f"{good.name}.uploaded"
    other = write_new(root, at(10, 6), [rec(at(10, 2), lat=2.0)])
    other.rename(tmp)
    write_new(root, at(10, 7), [rec(at(10, 3), lat=3.0)]).rename(sentinel)
    assert lats(tracks(api, at(10), at(10, 30))) == [1.0]
    assert opened and set(opened) == {good.name}


def test_a_file_flushed_before_the_window_opened_is_never_opened(api, root, refs, opened):
    # Named 09:59:59: every row was solved before 10:00:00.
    before = write_new(root, at(9, 59, 59, 950), [rec(at(9, 59, 59, 900), lat=1.0)])
    # Named 10:00:00, the window's first second: a row solved at 10:00:00.500 is in it.
    first = write_new(root, at(10, 0, 0, 700), [rec(at(10, 0, 0, 500), lat=2.0)])
    assert lats(tracks(api, at(10), at(10, 30))) == [2.0]
    assert before.name.startswith("part-095959-") and first.name.startswith("part-100000-")
    assert before.name not in opened
    assert first.name in opened


def test_a_row_solved_before_midnight_is_found_in_the_next_days_partition(api, root, refs):
    late = write_new(root, at(0, 0, 40, day=19), [rec(at(23, 59, 50), lat=1.0), rec(at(0, 0, 10, day=19), lat=2.0)])
    assert "day=19" in str(late)
    assert lats(tracks(api, at(23, 50), at(0, 0, day=19))) == [1.0]


def test_a_batch_held_past_the_slack_across_midnight_is_not_read(api, root, refs, opened):
    """The documented limit. A window ending more than TRACKS_PARTITION_SLACK_MS
    before midnight does not read the next day, so a row solved in it is missed
    only when its batch was written over ten minutes late (a retried disk failure)."""
    late = write_new(root, at(0, 0, 30, day=19), [rec(at(23, 40), lat=1.0)])
    assert lats(tracks(api, at(23, 30), at(23, 45))) == []
    assert late.name not in opened
    # A window ending within the slack of midnight reads the next day.
    assert lats(tracks(api, at(23, 30), at(23, 51))) == [1.0]


def test_a_file_removed_after_listing_is_skipped_quietly(api, root, refs, monkeypatch, caplog):
    write_new(root, at(10, 5), [rec(at(10, 1), lat=1.0)])
    gone = write_new(root, at(10, 6), [rec(at(10, 2), lat=2.0)])
    listing = tracks_store._part_names
    listed: list[str] = []

    def list_then_remove(directory):
        names = listing(directory)
        listed.extend(names)
        gone.unlink(missing_ok=True)
        return names

    monkeypatch.setattr(tracks_store, "_part_names", list_then_remove)
    with caplog.at_level(logging.WARNING, logger="services.tracks_store"):
        response = tracks(api, at(10), at(10, 30))
    assert gone.name in listed and not gone.exists()
    assert lats(response) == [1.0]
    assert [r for r in caplog.records if r.name == "services.tracks_store"] == []


def test_a_file_removed_between_its_footer_and_its_rows_is_skipped_quietly(api, root, refs, monkeypatch, caplog):
    write_new(root, at(10, 5), [rec(at(10, 1), lat=1.0)])
    gone = write_new(root, at(10, 6), [rec(at(10, 2), lat=2.0)])
    footer = tracks_store._read_groups

    def read_then_remove(path):
        groups = footer(path)
        if path == str(gone):
            gone.unlink()
        return groups

    monkeypatch.setattr(tracks_store, "_read_groups", read_then_remove)
    with caplog.at_level(logging.WARNING, logger="services.tracks_store"):
        response = tracks(api, at(10), at(10, 30))
    assert lats(response) == [1.0]
    assert [r for r in caplog.records if r.name == "services.tracks_store"] == []


def test_an_unreadable_file_is_skipped_with_one_warning(api, root, refs, caplog):
    good = write_new(root, at(10, 5), [rec(at(10, 1), lat=1.0)])
    (good.parent / f"part-100600-{uuid.uuid4().hex}.parquet").write_bytes(b"not parquet at all")
    with caplog.at_level(logging.WARNING, logger="services.tracks_store"):
        first = tracks(api, at(10), at(10, 30))
        second = tracks(api, at(10), at(10, 30))
    assert lats(first) == lats(second) == [1.0]
    warnings = [r for r in caplog.records if r.name == "services.tracks_store"]
    assert len(warnings) == 1 and "part-100600-" in warnings[0].getMessage()


def test_a_missing_root_or_partition_is_an_empty_answer(api, root, refs, caplog):
    with caplog.at_level(logging.WARNING, logger="services.tracks_store"):
        body = tracks(api, at(10), at(10, 30)).json()
    assert [r for r in caplog.records if r.name == "services.tracks_store"] == []
    assert body == {
        "from": ms_of(at(10)),
        "to": ms_of(at(10, 30)),
        "fleet": "real",
        "count": 0,
        "truncated": False,
        "rows": [],
    }


def test_a_new_file_is_served_by_the_next_call(api, root, refs):
    write_new(root, at(10, 5), [rec(at(10, 1), lat=1.0)])
    assert lats(tracks(api, at(10), at(10, 30))) == [1.0]
    write_new(root, at(10, 6), [rec(at(10, 2), lat=2.0)])
    assert lats(tracks(api, at(10), at(10, 30))) == [1.0, 2.0]


def test_a_removed_file_is_not_served_from_the_cache(api, root, refs):
    write_new(root, at(10, 5), [rec(at(10, 1), lat=1.0)])
    gone = write_new(root, at(10, 6), [rec(at(10, 2), lat=2.0)])
    assert lats(tracks(api, at(10), at(10, 30))) == [1.0, 2.0]
    gone.unlink()
    assert lats(tracks(api, at(10), at(10, 30))) == [1.0]


def test_every_value_is_a_plain_json_value_of_the_contract_type(api, root, refs):
    _fleets(root)
    write_new(root, at(10, 20), [rec(at(10, 15), lat=21.0, is_anomalous=True, anomaly_types=["x"], adsb_hex="abc123")])
    body = tracks(api, at(10), at(10, 30), fleet="all").json()
    assert [type(body[k]) for k in ("from", "to", "fleet", "count", "truncated", "rows")] == [
        int,
        int,
        str,
        int,
        bool,
        list,
    ]
    types = {
        "frame_ts_ms": (int,),
        "solve_ts_ms": (int,),
        "track_key": (str, type(None)),
        "lane": (str, type(None)),
        "node_kind": (str, type(None)),
        "lat": (float,),
        "lon": (float,),
        "n_nodes": (int,),
        "node_refs": (list,),
        "adsb_hex": (str, type(None)),
        "is_anomalous": (bool, type(None)),
        "anomaly_types": (list,),
    }
    floats = ("alt_m", "vel_east_ms", "vel_north_ms", "vel_up_ms", "pos_sigma_m", "rms_delay_us", "rms_doppler_hz")
    for row in body["rows"]:
        assert list(row) == ROW_KEYS
        for key, allowed in types.items():
            assert type(row[key]) in allowed, (key, row[key])
        for key in floats:
            assert type(row[key]) in (float, type(None)), (key, row[key])
        assert all(type(ref) is str for ref in row["node_refs"] + row["anomaly_types"])
        assert row["lane"] in ("dark", "known", "follow", None)


# ── Availability ─────────────────────────────────────────────────────────────


def availability(api, start: str, end: str, **params):
    return api.get("/api/data/tracks/availability", params={"from": start, "to": end, **params})


def hours(response) -> list[tuple[int, int, int]]:
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == sum(h["count"] for h in body["hours"])
    return [(h["start_ms"], h["count"], h["unclassified"]) for h in body["hours"]]


def _hourly(root: Path) -> None:
    """Hour 10: new files, 2 real, 3 synthetic, 1 mixed, 1 with no ids, and
    2 legacy rows. Hour 11: nothing. Hour 12: 1 real, 1 legacy. Hour 14: 2
    synthetic, so nothing for the real fleet."""
    write_new(
        root,
        at(10, 30),
        [rec(at(10, 1)), rec(at(10, 2))]
        + [rec(at(10, 3 + i), (SYN_A,)) for i in range(3)]
        + [rec(at(10, 9), (PUB_A, SYN_A)), rec(at(10, 10), ())],
    )
    write_old(root, at(10, 40), [old(at(10, 35), PUB_A), old(at(10, 36), SYN_A)])
    write_new(root, at(12, 20), [rec(at(12, 10))])
    write_old(root, at(12, 30), [old(at(12, 25), PUB_A)])
    write_new(root, at(14, 20), [rec(at(14, 10), (SYN_A,)), rec(at(14, 11), (SYN_B,))])


@pytest.mark.parametrize(
    ("fleet", "expected"),
    [
        ("real", [(10, 2, 2), (12, 1, 1)]),
        ("synthetic", [(10, 3, 2), (12, 0, 1), (14, 2, 0)]),
        ("all", [(10, 9, 0), (12, 2, 0), (14, 2, 0)]),
    ],
    ids=["real", "synthetic", "all"],
)
def test_availability_counts_rows_per_hour_for_the_fleet(api, root, fleet, expected):
    _hourly(root)
    response = availability(api, "2026-10-18", "2026-10-18", fleet=fleet)
    assert hours(response) == [(ms_of(at(h)), count, unclassified) for h, count, unclassified in expected]
    body = response.json()
    assert (body["from"], body["to"], body["fleet"]) == ("2026-10-18", "2026-10-18", fleet)


def test_availability_defaults_to_the_real_fleet(api, root):
    _hourly(root)
    assert availability(api, "2026-10-18", "2026-10-18").json()["fleet"] == "real"


def test_availability_never_reads_row_data(api, root, monkeypatch):
    _hourly(root)
    sample = next(root.rglob("part-*.parquet"))

    def refuse(*args, **kwargs):
        raise AssertionError("row data read")

    for name in ("read", "read_row_group", "read_row_groups", "iter_batches", "scan_contents"):
        monkeypatch.setattr(pq.ParquetFile, name, refuse)
    for name in ("read_table", "read_pandas", "ParquetDataset"):
        monkeypatch.setattr(pq, name, refuse)
    monkeypatch.setattr(ds, "dataset", refuse)
    with pytest.raises(AssertionError, match="row data read"):
        pq.ParquetFile(sample).read_row_group(0)

    assert hours(availability(api, "2026-10-18", "2026-10-18", fleet="real")) == [
        (ms_of(at(10)), 2, 2),
        (ms_of(at(12)), 1, 1),
    ]
    assert hours(availability(api, "2026-10-18", "2026-10-18", fleet="all")) == [
        (ms_of(at(10)), 9, 0),
        (ms_of(at(12)), 2, 0),
        (ms_of(at(14)), 2, 0),
    ]


def test_availability_reads_the_day_after_for_rows_solved_in_range(api, root):
    write_new(root, at(0, 0, 30, day=19), [rec(at(23, 59, 50))])
    write_new(root, at(0, 5, day=19), [rec(at(0, 4, day=19))])
    assert hours(availability(api, "2026-10-18", "2026-10-18")) == [(ms_of(at(23)), 1, 0)]
    assert hours(availability(api, "2026-10-19", "2026-10-19")) == [(ms_of(at(0, day=19)), 1, 0)]


def test_availability_falls_back_to_the_flush_hour_without_statistics(api, root):
    table = pa.table({f.name: [r[f.name] for r in [old(at(10, 59), PUB_A)]] for f in OLD_SCHEMA}, schema=OLD_SCHEMA)
    directory = partition(root, at(11, 0, 30))
    directory.mkdir(parents=True)
    pq.write_table(table, directory / f"part-110030-{uuid.uuid4().hex}.parquet", write_statistics=False)
    assert hours(availability(api, "2026-10-18", "2026-10-18", fleet="all")) == [(ms_of(at(11)), 1, 0)]


def test_availability_sees_a_new_file_on_the_next_call(api, root):
    write_new(root, at(10, 5), [rec(at(10, 1))])
    assert hours(availability(api, "2026-10-18", "2026-10-18")) == [(ms_of(at(10)), 1, 0)]
    write_new(root, at(10, 6), [rec(at(10, 2))])
    assert hours(availability(api, "2026-10-18", "2026-10-18")) == [(ms_of(at(10)), 2, 0)]


def test_availability_skips_temporaries_and_sentinels(api, root):
    good = write_new(root, at(10, 5), [rec(at(10, 1))])
    write_new(root, at(10, 6), [rec(at(10, 2))]).rename(good.parent / ".parquet-abc.tmp")
    write_new(root, at(10, 7), [rec(at(10, 3))]).rename(good.parent / f"{good.name}.uploaded")
    assert hours(availability(api, "2026-10-18", "2026-10-18")) == [(ms_of(at(10)), 1, 0)]


def test_availability_over_an_empty_archive_has_no_hours(api, root, caplog):
    with caplog.at_level(logging.WARNING, logger="services.tracks_store"):
        body = availability(api, "2026-10-01", "2026-10-31").json()
    assert [r for r in caplog.records if r.name == "services.tracks_store"] == []
    assert body == {"from": "2026-10-01", "to": "2026-10-31", "fleet": "real", "hours": [], "total": 0}


def test_availability_is_capped_at_92_days(api, root):
    first = date(2026, 7, 1)
    assert availability(api, first.isoformat(), (first + timedelta(days=91)).isoformat()).status_code == 200
    over = availability(api, first.isoformat(), (first + timedelta(days=92)).isoformat())
    assert over.status_code == 422
    assert "92-day cap" in over.json()["detail"]


def test_availability_to_before_from_is_422(api, root):
    response = availability(api, "2026-10-18", "2026-10-17")
    assert response.status_code == 422
    assert response.json()["detail"] == "to must not be before from"
    assert availability(api, "2026-10-18", "2026-10-18").status_code == 200


@pytest.mark.parametrize(
    "value", ["2026-10-1", "20261018", "2026/10/18", "2026-02-30", "18-10-2026", "2026-10-18T00:00:00Z", "x", ""]
)
def test_availability_wants_strict_dates(api, root, value):
    for params in ({"from": value, "to": "2026-10-18"}, {"from": "2026-10-18", "to": value}):
        response = api.get("/api/data/tracks/availability", params=params)
        assert response.status_code == 422, params
        assert "must be a UTC date" in response.json()["detail"]


def test_availability_rejects_an_unknown_fleet(api, root):
    assert availability(api, "2026-10-18", "2026-10-18", fleet="mixed").status_code == 422


# ── The API reference ────────────────────────────────────────────────────────


def test_the_documented_bodies_are_the_ones_sent(api, root, refs):
    """The models the API reference publishes (extra keys forbidden) describe real answers."""
    write_new(
        root, at(10, 5), [rec(at(10, 1), frame=at(10, 0, 59), vel_up=1.5, is_anomalous=True, anomaly_types=["x"])]
    )
    write_old(root, at(10, 10), [old(at(10, 6), PUB_A)])
    page = ArchiveTracks.model_validate(tracks(api, at(10), at(10, 30)).json(), strict=True)
    assert (page.from_ms, page.to, page.fleet, page.count, page.truncated) == (
        ms_of(at(10)),
        ms_of(at(10, 30)),
        "real",
        2,
        False,
    )
    new, legacy = page.rows
    assert new.frame_ts_ms == ms_of(at(10, 0, 59))
    assert (new.vel_up_ms, new.rms_delay_us, new.rms_doppler_hz) == (1.5, 0.41, 3.2)
    assert (legacy.rms_delay_us, legacy.rms_doppler_hz) == (0.5, 2.0)
    assert new.pos_sigma_m is not None and legacy.pos_sigma_m is None

    counts = ArchiveTracksAvailability.model_validate(availability(api, "2026-10-18", "2026-10-18").json(), strict=True)
    assert (counts.from_day, counts.to, counts.fleet, counts.total) == ("2026-10-18", "2026-10-18", "real", 1)
    assert [(h.start_ms, h.count, h.unclassified) for h in counts.hours] == [(ms_of(at(10)), 1, 1)]


def test_the_reference_lists_both_reads_under_archive(api):
    document = api.get("/openapi.json").json()
    for path in ("/api/data/tracks", "/api/data/tracks/availability"):
        operation = document["paths"][path]["get"]
        assert operation["tags"] == ["archive"]
        assert "security" not in operation
        assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].startswith(
            "#/components/schemas/ArchiveTracks"
        )
    assert {p["name"] for p in document["paths"]["/api/data/tracks"]["get"]["parameters"]} == {
        "from",
        "to",
        "bbox",
        "fleet",
    }
