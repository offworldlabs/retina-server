"""routes/detections_archive.py: the public detections archive, read back over HTTP.

Files are written by the archive's own writer (services/parquet_writer.py)
into a per-test root that services.storage._LOCAL_ARCHIVE_DIR is pointed at,
so the receiver position in them is the one the writer publishes and stamps.
Node rows with minted refs stand behind the real ids, so the publication rules
run against the real policy and identity caches.
"""

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from core import state
from core.nodes import Node
from core.users import async_session_maker
from main import app
from routes.detections_archive import ArchiveDetections
from services import detections_store, node_auth, node_refs, publication, r2_client
from services.archive_access import public_scope
from services.parquet_io import write_parquet_batch
from services.parquet_writer import PUBLISHED_SCHEMA, SCHEMA, _flatten, write_detections_parquet

PUB_A, PUB_B = "ret1a2b3c4d", "ret2b3c4d5e"
PRIV = "ret3a4b5c6d"
# A real id with no registration: it has no published identity.
NO_REF = "ret5e6f7a8b"
SYN_A, SYN_B = "synth-alpha", "synth-beta"

# The true geometry a node reports in its CONFIG.
CFG = {"rx_lat": 34.85, "rx_lon": -82.39, "tx_lat": 34.80, "tx_lon": -82.30}

ROW_KEYS = [
    "node_ref",
    "frame_ts_ms",
    "delay_us",
    "doppler_hz",
    "snr_db",
    "adsb_hex",
    "rx_lat",
    "rx_lon",
    "tx_lat",
    "tx_lon",
]


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


def make_private(node_id: str) -> None:
    """``node_id`` re-registered private, and the policy re-read, as its TTL would."""

    async def _go():
        async with async_session_maker() as session:
            await session.execute(update(Node).where(Node.node_id == node_id).values(publication="private"))
            await session.commit()

    asyncio.run(_go())
    publication._reset_for_tests()


def frame(moment: datetime, *delays: float, **extra) -> dict:
    """A frame as a node sends it; each delay marks one detection."""
    n = len(delays)
    return {"timestamp": ms_of(moment), "delay": list(delays), "doppler": [12.5] * n, "snr": [9.0] * n, **extra}


def write(root: Path, node_id: str, flushed: datetime, frames: list[dict], cfg: dict | None = CFG) -> Path:
    key = write_detections_parquet(node_id=node_id, frames=frames, base_dir=root, write_ts=flushed, node_cfg=cfg)
    assert key is not None
    return root / key


def node_dir(root: Path, node_id: str, flushed: datetime) -> Path:
    return root / f"year={flushed:%Y}" / f"month={flushed:%m}" / f"day={flushed:%d}" / f"node_id={node_id}"


def write_unstamped(root: Path, node_id: str, flushed: datetime, frames: list[dict]) -> Path:
    """A file as the writer wrote it before it stamped the rx frame."""
    table = pa.table(_flatten(node_id, frames, ms_of(flushed), CFG), schema=SCHEMA)
    directory = node_dir(root, node_id, flushed)
    return directory / write_parquet_batch(table, directory, flushed)


def write_groups(root: Path, node_id: str, flushed: datetime, *groups: list[dict]) -> Path:
    """A stamped file with one row group per list of frames."""
    tables = [pa.table(_flatten(node_id, frames, ms_of(flushed), CFG), schema=PUBLISHED_SCHEMA) for frames in groups]
    directory = node_dir(root, node_id, flushed)
    sizes = [t.num_rows for t in tables]
    return directory / write_parquet_batch(pa.concat_tables(tables), directory, flushed, row_group_sizes=sizes)


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "archive"
    monkeypatch.setattr("services.storage._LOCAL_ARCHIVE_DIR", str(path))
    detections_store._reset_for_tests()
    yield path
    detections_store._reset_for_tests()


@pytest.fixture
def refs():
    return _seed()


@pytest.fixture
def api():
    """The real app without its lifespan: this route needs none of it."""
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    client.close()


@pytest.fixture
def reads(monkeypatch):
    """The Parquet files opened, and the (file, row group) of every data read, in order."""
    seen = SimpleNamespace(opened=[], decoded=[])
    original = pq.ParquetFile

    class Recording(original):
        def __init__(self, source, *args, **kwargs):
            self._name = Path(source).name
            seen.opened.append(self._name)
            super().__init__(source, *args, **kwargs)

        def read_row_group(self, i, *args, **kwargs):
            seen.decoded.append((self._name, i))
            return super().read_row_group(i, *args, **kwargs)

        def read_row_groups(self, row_groups, *args, **kwargs):
            seen.decoded.extend((self._name, i) for i in row_groups)
            return super().read_row_groups(row_groups, *args, **kwargs)

        def read(self, *args, **kwargs):
            seen.decoded.append((self._name, "all"))
            return super().read(*args, **kwargs)

        def iter_batches(self, *args, **kwargs):
            seen.decoded.append((self._name, "batches"))
            return super().iter_batches(*args, **kwargs)

    monkeypatch.setattr(pq, "ParquetFile", Recording)
    return seen


def detections(api, start: datetime, end: datetime, **params):
    return api.get("/api/data/detections", params={"from": iso(start), "to": iso(end), **params})


def delays(response) -> list[float]:
    assert response.status_code == 200, response.text
    return [row["delay_us"] for row in response.json()["rows"]]


# ── Parameters and the window ────────────────────────────────────────────────


def test_the_window_includes_from_and_excludes_to(api, root, refs):
    write(
        root,
        PUB_A,
        at(10, 30),
        [
            frame(at(9, 59, 59, 999), 1.0),
            frame(at(10), 2.0),
            frame(at(10, 9, 59, 999), 3.0),
            frame(at(10, 10), 4.0),
        ],
    )
    response = detections(api, at(10), at(10, 10))
    assert delays(response) == [2.0, 3.0]
    assert (response.json()["from"], response.json()["to"]) == (ms_of(at(10)), ms_of(at(10, 10)))


@pytest.mark.parametrize(("start", "end"), [(at(10), at(10)), (at(10, 1), at(10))], ids=["equal", "reversed"])
def test_to_must_be_after_from(api, root, refs, start, end):
    response = detections(api, start, end)
    assert response.status_code == 422
    assert response.json()["detail"] == "to must be after from"


def test_the_window_is_capped_at_10_minutes(api, root, refs):
    write(root, PUB_A, at(10, 30), [frame(at(10), 1.0), frame(at(10, 9, 59, 999), 2.0)])
    assert delays(detections(api, at(10), at(10, 10))) == [1.0, 2.0]
    over = detections(api, at(10), at(10, 10, 0, 1))
    assert over.status_code == 422
    assert "10 minutes" in over.json()["detail"]


def test_fleet_keeps_the_nodes_of_one_kind_and_defaults_to_real(api, root, refs):
    write(root, PUB_A, at(10, 30), [frame(at(10, 1), 1.0)])
    write(root, SYN_A, at(10, 30), [frame(at(10, 2), 2.0)])
    assert delays(detections(api, at(10), at(10, 10))) == [1.0]
    assert delays(detections(api, at(10), at(10, 10), fleet="real")) == [1.0]
    assert delays(detections(api, at(10), at(10, 10), fleet="synthetic")) == [2.0]
    assert delays(detections(api, at(10), at(10, 10), fleet="all")) == [1.0, 2.0]
    assert detections(api, at(10), at(10, 10), fleet="mixed").status_code == 422


def test_bbox_keeps_a_row_whose_receiver_or_transmitter_is_inside(api, root, refs):
    write(
        root,
        PUB_A,
        at(10, 30),
        [
            frame(at(10, 1), 1.0, rx_lat=10.0, rx_lon=10.0, tx_lat=40.0, tx_lon=40.0),
            frame(at(10, 2), 2.0, rx_lat=20.0, rx_lon=20.0, tx_lat=10.5, tx_lon=10.5),
            frame(at(10, 3), 3.0, rx_lat=60.0, rx_lon=60.0, tx_lat=61.0, tx_lon=61.0),
            # No geometry at all: neither end is anywhere.
            frame(at(10, 4), 4.0),
        ],
        cfg=None,
    )
    assert delays(detections(api, at(10), at(10, 10))) == [1.0, 2.0, 3.0, 4.0]
    assert delays(detections(api, at(10), at(10, 10), bbox="9,9,11,11")) == [1.0, 2.0]
    assert delays(detections(api, at(10), at(10, 10), bbox="-90,-180,90,180")) == [1.0, 2.0, 3.0]
    assert delays(detections(api, at(10), at(10, 10), bbox="0,0,5,5")) == []


def test_a_bbox_whose_west_is_east_of_its_east_crosses_the_antimeridian(api, root, refs):
    write(
        root,
        PUB_A,
        at(10, 30),
        [
            frame(at(10, 1), 1.0, rx_lat=0.0, rx_lon=175.0, tx_lat=0.0, tx_lon=0.0),
            frame(at(10, 2), 2.0, rx_lat=0.0, rx_lon=0.0, tx_lat=0.0, tx_lon=-175.0),
            frame(at(10, 3), 3.0, rx_lat=0.0, rx_lon=0.0, tx_lat=0.0, tx_lon=5.0),
        ],
        cfg=None,
    )
    assert delays(detections(api, at(10), at(10, 10), bbox="-10,170,10,-170")) == [1.0, 2.0]


# ── Order, cap and locality ──────────────────────────────────────────────────


def test_rows_sort_by_frame_time_then_node_ref_then_delay(api, root, refs):
    write(root, SYN_B, at(10, 30), [frame(at(10, 1), 5.0, 4.0), frame(at(10, 2), 6.0)])
    write(root, SYN_A, at(10, 30), [frame(at(10, 2), 9.0), frame(at(10, 1), 8.0, 7.0)])
    response = detections(api, at(10), at(10, 10), fleet="synthetic")
    assert response.status_code == 200, response.text
    assert [(row["frame_ts_ms"], row["node_ref"], row["delay_us"]) for row in response.json()["rows"]] == [
        (ms_of(at(10, 1)), SYN_A, 7.0),
        (ms_of(at(10, 1)), SYN_A, 8.0),
        (ms_of(at(10, 1)), SYN_B, 4.0),
        (ms_of(at(10, 1)), SYN_B, 5.0),
        (ms_of(at(10, 2)), SYN_A, 9.0),
        (ms_of(at(10, 2)), SYN_B, 6.0),
    ]


def test_past_the_cap_the_first_rows_go_out_and_truncated_says_so(api, root, refs, monkeypatch):
    monkeypatch.setattr(detections_store, "DETECTIONS_ROW_CAP", 2)
    write(root, PUB_A, at(10, 30), [frame(at(10, 3), 3.0), frame(at(10, 1), 1.0), frame(at(10, 2), 2.0)])
    body = detections(api, at(10), at(10, 10)).json()
    assert (body["count"], body["truncated"], [row["delay_us"] for row in body["rows"]]) == (2, True, [1.0, 2.0])
    exact = detections(api, at(10), at(10, 2, 30)).json()
    assert (exact["count"], exact["truncated"]) == (2, False)


# The server's clock in the locality and pending cases.
NOW_MS = ms_of(at(12, day=21))
MINUTE_MS = 60_000
DAY_MS = 24 * 60 * MINUTE_MS


@pytest.mark.parametrize(
    ("enabled", "retention_days", "opens", "local"),
    [
        (True, 3, -4 * DAY_MS, False),
        (False, 3, -4 * DAY_MS, True),
        (True, 3, -11 * MINUTE_MS, True),
        (True, 0, -4 * DAY_MS, True),
        # It opens after the deletion age, but a node clock running ahead put
        # its first frames in files flushed before it, which may be deleted.
        (True, 3, -3 * DAY_MS + 5 * MINUTE_MS, False),
        # The earliest file it can need was flushed at the deletion age, which the lifecycle keeps.
        (True, 3, -3 * DAY_MS + 10 * MINUTE_MS, True),
        (True, 3, -3 * DAY_MS + 10 * MINUTE_MS - 1, False),
    ],
    ids=[
        "store-on-old-window",
        "store-off",
        "store-on-recent-window",
        "deletion-off",
        "store-on-window-straddling-the-deletion-age",
        "store-on-window-whose-earliest-file-is-at-the-deletion-age",
        "store-on-window-whose-earliest-file-is-just-past-it",
    ],
)
def test_local_is_false_once_the_lifecycle_may_have_deleted_a_file_of_the_window(
    api, root, refs, monkeypatch, enabled, retention_days, opens, local
):
    monkeypatch.setattr(r2_client, "is_enabled", lambda: enabled)
    monkeypatch.setattr(detections_store, "ARCHIVE_RETENTION_DAYS", retention_days)
    monkeypatch.setattr(detections_store, "_now_ms", lambda: NOW_MS)
    start = NOW_MS + opens
    response = api.get("/api/data/detections", params={"from": str(start), "to": str(start + 10 * MINUTE_MS)})
    assert response.status_code == 200, response.text
    assert response.json()["local"] is local


@pytest.mark.parametrize(
    ("written", "ends", "pending"),
    [
        # No archive write recorded since the server started: pending until an
        # hour's write interval and ten minutes' allowance have passed.
        (None, -2 * MINUTE_MS, True),
        (None, -70 * MINUTE_MS + 1, True),
        (None, -70 * MINUTE_MS, False),
        # The last write, 20 minutes ago, holds what was taken over ten minutes before it.
        (-20 * MINUTE_MS, -2 * MINUTE_MS, True),
        (-20 * MINUTE_MS, -30 * MINUTE_MS + 1, True),
        (-20 * MINUTE_MS, -30 * MINUTE_MS, False),
        # A write hours overdue holds nothing new, but past the interval and the allowance nothing is pending.
        (-180 * MINUTE_MS, -70 * MINUTE_MS + 1, True),
        (-180 * MINUTE_MS, -70 * MINUTE_MS, False),
    ],
    ids=[
        "no-write-recorded-newest-minutes",
        "no-write-recorded-just-inside-the-interval",
        "no-write-recorded-past-the-interval",
        "written-newest-minutes",
        "written-just-after-what-the-write-holds",
        "written-all-of-it",
        "write-overdue-inside-the-interval",
        "write-overdue-past-the-interval",
    ],
)
def test_pending_while_the_archive_may_not_have_written_the_window(
    api, root, refs, monkeypatch, written, ends, pending
):
    monkeypatch.setattr(detections_store, "_now_ms", lambda: NOW_MS)
    if written is None:
        monkeypatch.delitem(state.task_last_success, "archive_flush", raising=False)
    else:
        monkeypatch.setitem(state.task_last_success, "archive_flush", (NOW_MS + written) / 1000)
    end = NOW_MS + ends
    response = api.get("/api/data/detections", params={"from": str(end - 10 * MINUTE_MS), "to": str(end)})
    assert response.status_code == 200, response.text
    assert response.json()["pending"] is pending


def test_a_write_landing_while_the_window_is_read_leaves_the_answer_pending(api, root, refs, monkeypatch):
    monkeypatch.setattr(detections_store, "_now_ms", lambda: ms_of(at(12, 0, 6)))
    monkeypatch.setitem(state.task_last_success, "archive_flush", ms_of(at(11, 0, 5)) / 1000)
    write(root, PUB_A, at(11, 0, 4), [frame(at(10, 58), 10.0)])
    listed = detections_store._listed

    def listing_then_the_hourly_write_lands(*args, **kwargs):
        files = listed(*args, **kwargs)
        write(root, PUB_B, at(12, 0, 4), [frame(at(11, 5), 42.0)])
        state.task_last_success["archive_flush"] = ms_of(at(12, 0, 5)) / 1000
        return files

    monkeypatch.setattr(detections_store, "_listed", listing_then_the_hourly_write_lands)
    response = detections(api, at(10, 56), at(11, 6))
    assert delays(response) == [10.0]
    assert response.json()["pending"] is True


# ── What is read ─────────────────────────────────────────────────────────────


def test_a_frame_before_midnight_is_found_in_the_next_days_partition(api, root, refs):
    write(root, PUB_A, at(0, 30, day=19), [frame(at(23, 55), 1.0), frame(at(0, 10, day=19), 2.0)])
    assert delays(detections(api, at(23, 50), at(0, 0, day=19))) == [1.0]


def test_files_and_row_groups_outside_the_window_are_never_decoded(api, root, refs, reads):
    # Flushed more than the clock margin before the window opened: never opened.
    early = write(root, PUB_A, at(9, 39, 59), [frame(at(9, 35), 1.0)])
    # Flushed after the window opened, every frame after it closed.
    later = write(root, PUB_A, at(10, 30), [frame(at(10, 15), 2.0), frame(at(10, 20), 3.0)])
    # One row group before the window and one inside it.
    split = write_groups(root, PUB_A, at(10, 20), [frame(at(9, 50), 4.0)], [frame(at(10, 5), 5.0)])
    assert delays(detections(api, at(10), at(10, 10))) == [5.0]
    assert early.name not in reads.opened
    assert later.name in reads.opened
    assert reads.decoded == [(split.name, 1)]


def test_an_unstamped_file_contributes_nothing(api, root, refs, reads):
    old = write_unstamped(root, PUB_A, at(10, 30), [frame(at(10, 1), 1.0)])
    write(root, PUB_B, at(10, 30), [frame(at(10, 2), 2.0)])
    assert delays(detections(api, at(10), at(10, 10))) == [2.0]
    assert old.name not in [name for name, _ in reads.decoded]


def test_a_file_rewritten_in_place_is_read_again(api, root, refs):
    """A file is cached by its version: once stamped in place, as the rx backfill does, it is served."""
    path = write_unstamped(root, PUB_A, at(10, 30), [frame(at(10, 1), 1.0)])
    assert delays(detections(api, at(10), at(10, 10))) == []
    stamped = pq.read_table(path, partitioning=None).replace_schema_metadata(PUBLISHED_SCHEMA.metadata)
    pq.write_table(stamped, path)
    assert delays(detections(api, at(10), at(10, 10))) == [1.0]


def test_concurrent_calls_on_a_cold_cache_agree(root, refs):
    write(root, PUB_A, at(10, 30), [frame(at(10, 1), 1.0, 2.0)])
    write(root, SYN_A, at(10, 30), [frame(at(10, 1), 3.0)])
    write(root, PUB_B, at(10, 30), [frame(at(10, 2), 4.0)])
    scope = public_scope()

    def call(_):
        body = detections_store.query_detections(ms_of(at(10)), ms_of(at(10, 10)), fleet="all", scope=scope)
        return [row["delay_us"] for row in body["rows"]]

    with ThreadPoolExecutor(max_workers=8) as pool:
        answers = list(pool.map(call, range(16)))
    assert answers == [answers[0]] * 16
    assert sorted(answers[0]) == [1.0, 2.0, 3.0, 4.0]


# ── Publication ──────────────────────────────────────────────────────────────


def test_a_private_nodes_rows_are_absent_and_its_files_never_read(api, root, refs, reads):
    write(root, PUB_A, at(10, 30), [frame(at(10, 1), 1.0)])
    hidden = write(root, PRIV, at(10, 30), [frame(at(10, 2), 2.0)])
    assert delays(detections(api, at(10), at(10, 10), fleet="all")) == [1.0]
    assert hidden.name not in reads.opened


def test_a_real_node_with_no_identity_is_absent(api, root, refs):
    write(root, PUB_A, at(10, 30), [frame(at(10, 1), 1.0)])
    write(root, NO_REF, at(10, 30), [frame(at(10, 2), 2.0)])
    write(root, SYN_A, at(10, 30), [frame(at(10, 3), 3.0)])
    response = detections(api, at(10), at(10, 10), fleet="all")
    assert delays(response) == [1.0, 3.0]
    assert [row["node_ref"] for row in response.json()["rows"]] == [refs[PUB_A], SYN_A]


def test_the_private_set_is_read_on_every_request(api, root, refs, reads):
    write(root, PUB_A, at(10, 30), [frame(at(10, 1), 1.0)])
    write(root, PUB_B, at(10, 30), [frame(at(10, 2), 2.0)])
    assert delays(detections(api, at(10), at(10, 10))) == [1.0, 2.0]
    assert len(reads.decoded) == 2
    make_private(PUB_A)
    response = detections(api, at(10), at(10, 10))
    assert delays(response) == [2.0]
    assert refs[PUB_A] not in response.text
    # Served from the warm cache: nothing was decoded again.
    assert len(reads.decoded) == 2


def test_no_private_or_real_id_is_anywhere_in_the_body(api, root, refs):
    for i, node_id in enumerate((PUB_A, PUB_B, PRIV, NO_REF, SYN_A)):
        write(root, node_id, at(10, 30), [frame(at(10, 1 + i), float(i))])
    response = detections(api, at(10), at(10, 10), fleet="all")
    body = response.content
    assert response.json()["count"] == 3
    for node_id in (PUB_A, PUB_B, PRIV, NO_REF):
        assert node_id.encode() not in body
    assert refs[PRIV].encode() not in body
    assert not re.search(rb"ret[0-9a-f]{8}", body)
    assert [row["node_ref"] for row in response.json()["rows"]] == [refs[PUB_A], refs[PUB_B], SYN_A]


def test_rx_is_the_published_position_the_writer_stamped(api, root, refs):
    path = write(root, PUB_A, at(10, 30), [frame(at(10, 1), 1.0)])
    stored = pq.read_table(path, partitioning=None)
    stored_rx = (stored.column("rx_lat")[0].as_py(), stored.column("rx_lon")[0].as_py())
    [row] = detections(api, at(10), at(10, 10)).json()["rows"]
    assert (row["rx_lat"], row["rx_lon"]) == stored_rx
    assert row["rx_lat"] != 34.85 and row["rx_lon"] != -82.39
    assert (row["tx_lat"], row["tx_lon"]) == (34.80, -82.30)


def test_an_unknown_policy_is_a_503(api, root, refs, monkeypatch):
    write(root, PUB_A, at(10, 30), [frame(at(10, 1), 1.0)])
    publication._reset_for_tests()

    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(publication, "_query", unavailable)
    response = detections(api, at(10), at(10, 10))
    assert response.status_code == 503
    assert response.json() == {"detail": "Publication policy is temporarily unavailable"}


# ── The API reference ────────────────────────────────────────────────────────


def test_the_documented_body_is_the_one_sent(api, root, refs):
    """The model the API reference publishes (extra keys forbidden) describes a real answer of plain values."""
    write(
        root,
        PUB_A,
        at(10, 30),
        [frame(at(10, 1), 1.0, 2.0, 3.0, adsb=[{"hex": "a1b2c3"}, {"hex": ""}, None])],
    )
    response = detections(api, at(10), at(10, 10))
    page = ArchiveDetections.model_validate(response.json(), strict=True)
    assert (page.from_ms, page.to, page.fleet, page.count, page.truncated, page.local, page.pending) == (
        ms_of(at(10)),
        ms_of(at(10, 10)),
        "real",
        3,
        False,
        True,
        False,
    )
    assert [row.adsb_hex for row in page.rows] == ["a1b2c3", None, None]
    for row in response.json()["rows"]:
        assert list(row) == ROW_KEYS
        assert type(row["frame_ts_ms"]) is int
        assert type(row["node_ref"]) is str
        for key in ("delay_us", "doppler_hz", "snr_db", "rx_lat", "rx_lon", "tx_lat", "tx_lon"):
            assert type(row[key]) is float, (key, row[key])


def test_the_route_needs_no_session_and_is_listed_under_archive(api, root, refs):
    write(root, PUB_A, at(10, 30), [frame(at(10, 1), 1.0)])
    assert detections(api, at(10), at(10, 10)).status_code == 200
    document = api.get("/openapi.json").json()
    operation = document["paths"]["/api/data/detections"]["get"]
    assert operation["tags"] == ["archive"]
    assert "security" not in operation
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/ArchiveDetections"
    )
    assert {p["name"] for p in operation["parameters"]} == {"from", "to", "bbox", "fleet"}
