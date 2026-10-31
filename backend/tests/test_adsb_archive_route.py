"""routes/adsb_archive.py: the public ADS-B archive, read back over HTTP.

The rows and what reads them are covered in tests/test_adsb_archive_store.py,
whose helpers write the parts here; these cover the parameters, the policy as
read per request, what the body may carry, and the API reference, and that a
day answers the same from its raw parts, its compact and object storage. ADSBHub
traffic is archived here through the snapshot task itself, placed with
tests/test_adsb_snapshot.py's helpers.
"""

import asyncio
import json
import os
import re
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from core import state
from core.nodes import Node
from core.users import async_session_maker
from main import app
from routes.adsb_archive import ArchiveAdsb
from services import adsb_store, adsb_writer, node_sites, publication
from services import public_location as pl
from services.adsb_regions import cell_of
from services.compaction import COMPACT_NAME, SIDECAR_NAME
from services.tasks import adsb_snapshot
from tests.test_adsb_archive_store import (
    D18,
    D19,
    NO_REF,
    PRIV,
    PUB,
    PUB_B,
    PUBLISHED_KEYS,
    SYN,
    at,
    compact,
    configured_object_store,
    fix,
    made_private,
    ms_of,
    partition,
    seed,
    to_object_storage,
    write_part,
)
from tests.test_adsb_snapshot import _answer, _dest, _node


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "adsb"
    monkeypatch.setattr(adsb_writer, "ADSB_DIR", str(path))
    adsb_store._reset_for_tests()
    yield path
    adsb_store._reset_for_tests()


@pytest.fixture
def refs():
    return seed()


@pytest.fixture
def object_store(tmp_path, monkeypatch) -> Iterator[list[str]]:
    with configured_object_store(tmp_path, monkeypatch) as downloads:
        yield downloads


@pytest.fixture
def api():
    """The real app without its lifespan: this route needs none of it."""
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    client.close()


def adsb(api, start, end, **params):
    return api.get("/api/data/adsb", params={"from": str(ms_of(start)), "to": str(ms_of(end)), **params})


def lats(response) -> list[float]:
    assert response.status_code == 200, response.text
    return [row["lat"] for row in response.json()["rows"]]


def test_the_window_is_capped_at_60_minutes(api, root, refs):
    write_part(root, at(11, 1), [fix(at(10), lat=1.0), fix(at(10, 59, 59, 999), lat=2.0)])
    sixty = adsb(api, at(10), at(11))
    assert lats(sixty) == [1.0, 2.0]
    page = ArchiveAdsb.model_validate(sixty.json(), strict=True)
    assert (page.from_ms, page.to, page.fleet, page.count, page.truncated) == (
        ms_of(at(10)),
        ms_of(at(11)),
        "real",
        2,
        False,
    )
    over = adsb(api, at(10), at(11, 0, 0, 1))
    assert over.status_code == 422
    assert over.json()["detail"] == "to - from exceeds the 60-minute cap"


@pytest.mark.parametrize(("start", "end"), [(at(10), at(10)), (at(10, 1), at(10))], ids=["equal", "reversed"])
def test_to_must_be_after_from(api, root, refs, start, end):
    response = adsb(api, start, end)
    assert response.status_code == 422
    assert response.json()["detail"] == "to must be after from"


def test_bbox_keeps_the_rows_inside_it_including_across_the_antimeridian(api, root, refs):
    write_part(
        root,
        at(10, 5),
        [
            fix(at(10, 1), "aaaaaa", lat=1.0, lon=179.5),
            fix(at(10, 1), "bbbbbb", lat=2.0, lon=-179.5),
            fix(at(10, 1), "cccccc", lat=3.0, lon=0.0),
            fix(at(10, 1), "dddddd", lat=20.0, lon=179.5),
        ],
    )
    assert lats(adsb(api, at(10), at(10, 30), bbox="-10,170,10,-170")) == [1.0, 2.0]
    assert lats(adsb(api, at(10), at(10, 30), bbox="-10,-10,10,10")) == [3.0]


def make_private(node_id: str) -> None:
    async def _go():
        async with async_session_maker() as session:
            await session.execute(update(Node).where(Node.node_id == node_id).values(publication="private"))
            await session.commit()

    asyncio.run(_go())
    # What the registration routes do after a committed change.
    publication.invalidate()


def test_a_node_made_private_today_drops_its_fixes_from_the_same_files(api, root, refs):
    write_part(root, at(10, 5), [fix(at(10, 1), lat=1.0, node=PUB)])
    assert lats(adsb(api, at(10), at(10, 30))) == [1.0]
    make_private(PUB)
    assert lats(adsb(api, at(10), at(10, 30))) == []


@pytest.fixture
def snapshots(monkeypatch):
    """The snapshot task's inputs and memory, empty; node sites from the live nodes alone."""
    monkeypatch.setattr(state, "adsb_fixes_by_reporter", {}, raising=False)
    monkeypatch.setattr(state, "adsb_fallback", {})
    monkeypatch.setattr(state, "connected_nodes", {})
    monkeypatch.setattr(node_sites, "_positions_from_files", dict)
    for module in (adsb_snapshot, pl, node_sites):
        module._reset_for_tests()
    yield
    for module in (adsb_snapshot, pl, node_sites):
        module._reset_for_tests()


def test_a_node_made_private_after_adsbhub_traffic_was_archived_through_it_keeps_only_what_a_public_node_holds(
    api, root, refs, snapshots
):
    a = (40.0, -100.0)
    b = _dest(*a, 90.0, 10.0)
    assert cell_of(*a) == cell_of(*b)
    state.connected_nodes = {PUB: _node(*a), PUB_B: _node(*b)}
    now = ms_of(at(10))
    # The cell's answer: aaaaaa 140 km west of PUB, inside its clip disc only; bbbbbb between the two.
    aircraft = {"aaaaaa": _dest(*a, 270.0, 140.0), "bbbbbb": _dest(*a, 90.0, 5.0)}
    state.adsb_fallback = {cell_of(*a): _answer(aircraft, seen_ms=now - 2000)}
    # Both nodes are first seen in the polled cell a minute before, and settled by now.
    adsb_snapshot.take_snapshot(now - 60_000)
    adsb_snapshot.take_snapshot(now)
    key = adsb_snapshot.flush_adsb_snapshots(write_ts=at(10, 0, 5))
    assert pq.read_table(root / key, columns=["hex", "source", "source_node_id"]).to_pylist() == [
        {"hex": "aaaaaa", "source": "aggregator", "source_node_id": PUB},
        {"hex": "bbbbbb", "source": "aggregator", "source_node_id": f"{PUB},{PUB_B}"},
    ]
    response = adsb(api, at(10), at(10, 1))
    assert response.status_code == 200, response.text
    assert [row["hex"] for row in response.json()["rows"]] == ["aaaaaa", "bbbbbb"]

    made_private(PUB)
    response = adsb(api, at(10), at(10, 1))
    assert response.status_code == 200, response.text
    assert [row["hex"] for row in response.json()["rows"]] == ["bbbbbb"]
    assert not re.search(r"ret[0-9a-f]{8}", response.text)
    for ref in refs.values():
        assert ref not in response.text
    for column in ("source", "source_node_id", "aggregator"):
        assert column not in response.text


def test_no_attribution_or_withheld_node_appears_in_the_body(api, root, refs):
    write_part(
        root,
        at(10, 5),
        [
            fix(at(10, 1), "aaaaaa", node=PUB),
            fix(at(10, 1), "bbbbbb", node=PRIV),
            fix(at(10, 1), "cccccc", node=NO_REF),
            fix(at(10, 1), "dddddd", world="sim", node=SYN),
            fix(at(10, 1), "eeeeee", world="sim", node=None),
            fix(at(10, 1), "ffffff", source="aggregator", node=f"{PRIV},{PUB}"),
            fix(at(10, 1), "abcdef", source="aggregator", node=PRIV),
        ],
    )
    response = adsb(api, at(10), at(10, 30), fleet="all")
    body = json.loads(response.text)
    assert [row["hex"] for row in body["rows"]] == ["aaaaaa", "dddddd", "eeeeee", "ffffff"]
    assert all(set(row) == PUBLISHED_KEYS for row in body["rows"])
    assert not re.search(r"ret[0-9a-f]{8}", response.text)
    assert "synth-" not in response.text
    for ref in refs.values():
        assert ref not in response.text
    for key in ("source", "source_node_id", "world", "aggregator"):
        assert key not in response.text


def test_an_unknown_policy_is_a_503(api, root, refs, monkeypatch):
    write_part(root, at(10, 5), [fix(at(10, 1))])
    publication._reset_for_tests()

    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(publication, "_query", unavailable)
    response = adsb(api, at(10), at(10, 30))
    assert response.status_code == 503
    assert response.json() == {"detail": "Publication policy is temporarily unavailable"}


def test_the_store_is_read_on_the_archives_own_threads(api, root, refs, monkeypatch):
    threads: list[str] = []

    def read(*args, **kwargs):
        threads.append(threading.current_thread().name)
        return {"rows": []}

    monkeypatch.setattr(adsb_store, "query_adsb", read)
    assert adsb(api, at(10), at(10, 30)).status_code == 200
    assert len(threads) == 1 and threads[0].startswith("archive-store")


def test_the_reference_lists_the_read_under_archive_without_a_session(api):
    operation = api.get("/openapi.json").json()["paths"]["/api/data/adsb"]["get"]
    assert operation["tags"] == ["archive"]
    assert "security" not in operation
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/ArchiveAdsb"
    )
    assert set(operation["responses"]) == {"200", "422", "429", "503"}
    assert {p["name"] for p in operation["parameters"]} == {"from", "to", "bbox", "fleet"}
    row = api.get("/openapi.json").json()["components"]["schemas"]["ArchiveAdsbRow"]
    assert set(row["properties"]) == PUBLISHED_KEYS
    assert row["properties"]["seen_ms"]["type"] == "integer"
    assert "seen_ms" in row["required"]


# ── Compacted days and days kept only in object storage ─────────────────────

FLEETS = ("real", "synthetic", "all")
WINDOWS = {
    "inside an hour": (at(10), at(10, 30)),
    "across an hour": (at(10, 30), at(11, 30)),
    "across midnight": (at(23, 50), at(0, 10, day=19)),
    "up to midnight": (at(23, 30), at(0, day=19)),
}


def snapshot(ts) -> list[dict]:
    """One snapshot's fixes; lat says the snapshot (hours and hundredths of minutes) and the fix."""
    lat = ts.hour + ts.minute / 100 + ts.second / 10_000
    fresher, stale = ts + timedelta(milliseconds=300), ts - timedelta(seconds=3)
    return [
        # Heard by a public node and, fresher, by a node private today, each in a row of its own, and, staler,
        # by ADSBHub over the private and the public node: the public node's own fix is served.
        fix(ts, "aaaaaa", lat=lat + 0.000_01, node=PUB),
        fix(ts, "aaaaaa", lat=lat + 0.000_09, node=PRIV, seen=fresher),
        fix(ts, "aaaaaa", lat=lat + 0.000_02, source="aggregator", node=f"{PRIV},{PUB}", seen=stale),
        fix(ts, "bbbbbb", lat=lat + 0.000_03, source="aggregator", node=f"{PRIV},{PUB_B}"),
        # Withheld: a private node's own fix, ADSBHub over the private node alone, a real node with no identity.
        fix(ts, "cccccc", lat=lat + 0.000_04, node=PRIV),
        fix(ts, "dddddd", lat=lat + 0.000_05, source="aggregator", node=PRIV),
        fix(ts, "eeeeee", lat=lat + 0.000_06, node=NO_REF),
        # The simulated world: a synthetic node's fix and the simulator's own fleet.
        fix(ts, "f00001", lat=lat + 0.000_07, world="sim", node=SYN),
        fix(ts, "f00002", lat=lat + 0.000_08, world="sim", node=None),
    ]


def two_days(root: Path) -> list[Path]:
    """Parts as the writer files them: 10:00-11:20 on day 18, one straddling 11:00, then
    23:58-23:59 in day 18's partition, the 23:59:50 snapshot flushed after midnight into
    day 19's, and day 19's own first minutes."""
    parts = {
        at(10, 0, 59): [at(10, 0, 20), at(10, 0, 40)],
        at(10, 40, 59): [at(10, 40, 20), at(10, 40, 40)],
        at(11, 0, 59): [at(10, 59, 50), at(11, 0, 10)],
        at(11, 20, 59): [at(11, 20, 20)],
        at(23, 59, 30): [at(23, 58, 50), at(23, 59, 20)],
        at(0, 0, 30, day=19): [at(23, 59, 50), at(0, 0, 20, day=19)],
        at(0, 5, 59, day=19): [at(0, 5, 20, day=19)],
    }
    return [write_part(root, flushed, [r for ts in taken for r in snapshot(ts)]) for flushed, taken in parts.items()]


def answers(api) -> dict:
    bodies = {}
    for fleet in FLEETS:
        for name, (start, end) in WINDOWS.items():
            response = adsb(api, start, end, fleet=fleet)
            assert response.status_code == 200, (fleet, name, response.text)
            bodies[fleet, name] = response.json()
    return bodies


def served(body: dict) -> list[tuple[int, str]]:
    return [(row["ts_ms"], row["hex"]) for row in body["rows"]]


def test_a_day_answers_the_same_from_its_raw_parts_its_compact_and_object_storage(api, root, refs, object_store):
    parts = two_days(root)
    before = answers(api)
    # Every window holds rows, the next day's partition among them, and only publishable ones.
    assert served(before["real", "up to midnight"]) == [
        (ms_of(moment), hex_)
        for moment in (at(23, 58, 50), at(23, 59, 20), at(23, 59, 50))
        for hex_ in ("aaaaaa", "bbbbbb")
    ]
    # The node row of aaaaaa, not ADSBHub's staler fix, and when each was received, to the second below.
    assert [(round(row["lat"], 5), row["seen_ms"]) for row in before["real", "inside an hour"]["rows"][:2]] == [
        (10.00201, ms_of(at(10, 0, 19))),
        (10.00203, ms_of(at(10, 0, 19))),
    ]
    assert all(set(row) == PUBLISHED_KEYS for body in before.values() for row in body["rows"])
    assert served(before["synthetic", "across midnight"])[-2:] == [
        (ms_of(at(0, 5, 20, day=19)), "f00001"),
        (ms_of(at(0, 5, 20, day=19)), "f00002"),
    ]
    assert {row["hex"] for body in before.values() for row in body["rows"]} == {
        "aaaaaa",
        "bbbbbb",
        "f00001",
        "f00002",
    }

    compact(root, D18, D19)
    assert answers(api) == before
    # As the lifecycle leaves the days once their parts are uploaded and deleted.
    for part in parts:
        part.unlink()
    assert answers(api) == before
    # And once their compacts are past retention: only the sidecars stay.
    keys = [to_object_storage(root, day) for day in (D18, D19)]
    assert [os.listdir(partition(root, day)) for day in (D18, D19)] == [[SIDECAR_NAME], [SIDECAR_NAME]]
    assert answers(api) == before
    # Each day was fetched once, for every window and fleet after it.
    assert object_store == keys


def test_a_node_made_private_today_drops_its_fixes_from_a_compacted_day_and_from_object_storage(
    api, root, refs, object_store
):
    part = write_part(
        root,
        at(10, 5),
        [
            fix(at(10, 1), "aaaaaa", lat=1.0, node=PUB),
            fix(at(10, 1), "bbbbbb", lat=2.0, source="aggregator", node=PUB),
            fix(at(10, 1), "cccccc", lat=3.0, source="aggregator", node=f"{PUB},{PUB_B}"),
            fix(at(10, 1), "dddddd", lat=4.0, node=PUB_B),
            # Heard by both public nodes, PUB's fix the fresher; and by PUB and, fresher, by a node already private.
            fix(at(10, 1), "eeeeee", lat=5.0, node=PUB),
            fix(at(10, 1), "eeeeee", lat=5.5, node=PUB_B, seen=at(10, 0, 58)),
            fix(at(10, 1), "ffffff", lat=6.0, node=PUB, seen=at(10, 0, 58)),
            fix(at(10, 1), "ffffff", lat=6.5, node=PRIV),
        ],
    )
    compact(root, D18)
    part.unlink()
    assert lats(adsb(api, at(10), at(10, 30))) == [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]

    make_private(PUB)
    # An ADSBHub fix stays while another node it lists is public, and an aircraft another public node heard
    # stays, from that node's own fix.
    assert lats(adsb(api, at(10), at(10, 30))) == [3.0, 4.0, 5.5]
    key = to_object_storage(root, D18)
    assert lats(adsb(api, at(10), at(10, 30))) == [3.0, 4.0, 5.5]
    assert object_store == [key]


def test_a_day_that_cannot_be_loaded_from_object_storage_is_a_503_naming_only_the_date(api, root, refs, object_store):
    part = write_part(root, at(10, 5), [fix(at(10, 1), lat=1.0)])
    compact(root, D18)
    part.unlink()
    # Its sidecar promises a compact the store does not hold.
    (partition(root, D18) / COMPACT_NAME).unlink()

    response = adsb(api, at(10), at(10, 30))

    assert response.status_code == 503
    assert response.json() == {"detail": "The archived day 2026-10-18 could not be loaded from cold storage."}
    assert object_store == ["adsb/year=2026/month=10/day=18/compact.parquet"]


def test_a_day_that_keeps_changing_while_it_is_read_is_a_503_naming_only_the_date(api, root, refs, monkeypatch):
    write_part(root, at(10, 5), [fix(at(10, 1), lat=1.0)])
    day_parts = adsb_store._day_parts

    def listing_a_part_gone_before_its_read(day):
        return [*day_parts(day), str(partition(root, day) / f"part-102500-{'0' * 32}.parquet")]

    monkeypatch.setattr(adsb_store, "_day_parts", listing_a_part_gone_before_its_read)
    response = adsb(api, at(10), at(10, 30))
    assert response.status_code == 503
    assert response.json() == {"detail": "The archived day 2026-10-18 changed while it was read; try again."}
