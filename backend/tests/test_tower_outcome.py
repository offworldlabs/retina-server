"""The weekly archive post to tower-finder's fleet feedback: who is sent, what, and when."""

import asyncio
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import select

from config.constants import tower_outcome_settings
from core.nodes import Node, NodeConfig
from core.users import async_session_maker
from services import node_sites, performance_store, public_location
from services.tasks.tower_outcome_post import run_once, tower_outcome_post_task

_NOW = datetime(2026, 11, 9, 4, 30, tzinfo=UTC)  # Monday of ISO week 2026-W46, past the 04:00 slot
_PUBLIC = "ret0123abcd"
_ROW = {
    "node_id": _PUBLIC,
    # The published position; the registry holds (10.1234, 20.5678).
    "rx_lat": 10.1227,
    "rx_lon": 20.5752,
    "tx_lat": 10.5,
    "tx_lon": 20.5,
    "fc_hz": 563000000.0,
    "callsign": "QAAA",
    "source": "archive",
    "outcome": "observed",
    "run_id": "archive-2026-W46",
    "snr_median_db": 13.5,
    "verified_range_p85_km": 48.25,
    "hours_observed": 150.0,
    "adsb_match_rate": 0.25,
    "observed_at": "2026-11-09T00:00:00+00:00",
    "system_constant_db": -151.25,
    # light_GA reads "window" and helicopter "evidence": floors, so they stay off.
    "range_50pct_km": {"widebody": 72.5, "narrowbody": 61.0},
}
# What a tower-finder from before system_constant_db and range_50pct_km answers to that row.
_OLDER_422 = (
    '{"detail":[{"type":"model_attributes_type","loc":["body","function-after[_outcome_matches_source(), Towe'
    'rOutcome]"],"msg":"Input should be a valid dictionary or object to extract fields from","input":[{"node_'
    'id":"ret0123abcd","rx_lat":10.1227,"rx_lon":20.5752,"tx_lat":10.5,"tx_lon":20.5,"fc_hz":563000000.0,"callsi'
    'gn":"QAAA","source":"archive","outcome":"observed","run_id":"archive-2026-W46","snr_median_db":13.5,"ver'
    'ified_range_p85_km":48.25,"hours_observed":150.0,"adsb_match_rate":0.25,"observed_at":"2026-11-09T00:00:'
    '00+00:00","system_constant_db":-151.25,"range_50pct_km":{"widebody":72.5,"narrowbody":61.0}}]},{"type":"'
    'extra_forbidden","loc":["body","list[function-after[_outcome_matches_source(), TowerOutcome]]",0,"system'
    '_constant_db"],"msg":"Extra inputs are not permitted","input":-151.25},{"type":"extra_forbidden","loc":['
    '"body","list[function-after[_outcome_matches_source(), TowerOutcome]]",0,"range_50pct_km"],"msg":"Extra '
    'inputs are not permitted","input":{"widebody":72.5,"narrowbody":61.0}}]}'
)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A token, published positions under a fixed salt, and performance files under tmp_path."""
    monkeypatch.setenv("TOWER_FINDER_FEEDBACK_TOKEN", "test-token")
    monkeypatch.setenv("TOWER_FINDER_URL", "http://tower-finder.invalid")
    monkeypatch.setenv("NODE_FUZZ_MODE", "on")
    monkeypatch.setenv("NODE_FUZZ_SALT", "tower-outcome-test-salt")
    for name in ("NODE_FUZZ_MIN_KM", "NODE_FUZZ_MAX_KM", "NODE_FUZZ_SITE_KM", "NODE_FUZZ_SITE_AUDIT_KM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(performance_store, "DATA_DIR", tmp_path)
    monkeypatch.setattr(performance_store, "_files", performance_store._files)
    node_sites._reset_for_tests()
    public_location._reset_for_tests()
    yield tmp_path
    node_sites._reset_for_tests()
    public_location._reset_for_tests()


async def _seed(data_dir, *nodes):
    """Each (node id, publication, rx lat, rx lon, window's last day): a node, its config from 10-01 and its file."""
    async with async_session_maker() as session:
        session.add_all(Node(node_id=node[0], node_ref=f"nde{node[0][-8:]}00", publication=node[1]) for node in nodes)
        await session.flush()
        for node_id, _, lat, lon, last_day in nodes:
            config = NodeConfig(
                node_id=node_id, version=1, rx_lat=lat, rx_lon=lon, rx_alt_ft=300.0, tx_lat=10.5, tx_lon=20.5,
                tx_alt_ft=900.0, tx_callsign="QAAA", fc_hz=563e6, fs_hz=2e6, max_range_km=80.0, cpi_s=0.5,
                delay_tolerance_us=2.0, doppler_tolerance_hz=5.0, created_at=datetime(2026, 10, 1, tzinfo=UTC),
            )  # fmt: skip
            session.add(config)
            first_day = (date.fromisoformat(last_day) - timedelta(days=6)).isoformat()
            doc = {
                "schema": 1, "node_id": node_id, "window": [first_day, last_day], "rank_status": None, "tower": {},
                "config_version": 1,
                "opportunities": 2000, "detections": 500, "system_constant_db": -151.25,
                "system_constant_bound": "measured", "snr_median_db": 13.5, "verified_range_p85_km": 48.25,
                "hours_observed": 150.0,
                "range_50pct_km": {"widebody": 72.5, "narrowbody": 61.0, "light_GA": 30.0, "helicopter": None},
                "range_50pct_bound": {
                    "widebody": "measured", "narrowbody": "measured", "light_GA": "window", "helicopter": "evidence",
                },
            }  # fmt: skip
            (data_dir / f"performance_{node_id}.json").write_text(json.dumps(doc))
        await session.commit()
    performance_store.reload()


def _rewrite(data_dir, node_id, **changes):
    path = data_dir / f"performance_{node_id}.json"
    path.write_text(json.dumps(json.loads(path.read_text()) | changes))
    performance_store.reload()


async def _move(node_id, **changes):
    """Supersede the node's config on 11-08 by a version 2 holding ``changes``."""
    moved = datetime(2026, 11, 8, 12, tzinfo=UTC)
    async with async_session_maker() as session:
        old = await session.scalar(select(NodeConfig).where(NodeConfig.node_id == node_id))
        old.superseded_at = moved
        kept = {column.key: getattr(old, column.key) for column in NodeConfig.__table__.columns if column.key != "id"}
        session.add(NodeConfig(**{**kept, "version": 2, "created_at": moved, "superseded_at": None, **changes}))
        await session.commit()


def _client(seen, *answers):
    """A client whose transport records each request and gives the next answer."""
    queue = list(answers)

    def handler(request):
        seen.append(request)
        return queue.pop(0)

    return httpx.Client(transport=httpx.MockTransport(handler))


def _taken():
    return httpx.Response(200, json={"stored": 1, "ignored": 0})


async def test_without_a_token_the_task_returns_before_any_client_or_request(data_dir, monkeypatch):
    monkeypatch.delenv("TOWER_FINDER_FEEDBACK_TOKEN")
    await _seed(data_dir, (_PUBLIC, "public", 10.1234, 20.5678, "2026-11-08"))
    made, seen = [], []
    real_client = httpx.Client

    def recording_client(*args, **kwargs):
        made.append(kwargs)
        return real_client(transport=httpx.MockTransport(lambda request: seen.append(request) or _taken()))

    monkeypatch.setattr(httpx, "Client", recording_client)
    monkeypatch.setattr(httpx, "AsyncClient", recording_client)

    assert await asyncio.wait_for(tower_outcome_post_task(now=lambda: _NOW), 5) is None
    assert (made, seen) == ([], [])


async def test_a_due_week_posts_one_batch_of_the_published_figures_with_the_bearer_token(data_dir):
    await _seed(data_dir, (_PUBLIC, "public", 10.1234, 20.5678, "2026-11-08"))
    seen = []

    run_once(tower_outcome_settings(), lambda: _NOW, _client(seen, _taken()))

    assert [(str(request.url), request.headers["Authorization"]) for request in seen] == [
        ("http://tower-finder.invalid/api/feedback/tower-outcome", "Bearer test-token")
    ]
    assert json.loads(seen[0].content) == [_ROW]


async def test_a_week_on_record_is_not_posted_again(data_dir):
    await _seed(data_dir, (_PUBLIC, "public", 10.1234, 20.5678, "2026-11-08"))
    seen = []
    client = _client(seen, _taken(), _taken())

    run_once(tower_outcome_settings(), lambda: _NOW, client)
    later = run_once(tower_outcome_settings(), lambda: _NOW + timedelta(days=2), client)

    state = json.loads((data_dir / "tower_outcome_posted.json").read_text())
    windows = {_PUBLIC: "2026-11-09T00:00:00+00:00"}
    assert state == {"week": "2026-W46", "at": "2026-11-09T04:30:00+00:00", "windows": windows}
    assert (len(seen), later) == (1, 3600)


async def test_a_window_posted_in_one_week_is_not_posted_again_the_next(data_dir):
    # Posted on the Wednesday, the window is still within 7 days of the next Monday.
    await _seed(data_dir, (_PUBLIC, "public", 10.1234, 20.5678, "2026-11-10"))
    seen = []
    client = _client(seen, _taken(), _taken())

    run_once(tower_outcome_settings(), lambda: _NOW + timedelta(days=2), client)
    run_once(tower_outcome_settings(), lambda: _NOW + timedelta(days=7), client)

    assert len(seen) == 1


async def test_private_synthetic_quiet_and_config_suspect_nodes_are_not_posted(data_dir):
    await _seed(
        data_dir,
        (_PUBLIC, "public", 10.1234, 20.5678, "2026-11-02"),  # Ended 7 days before the post: still this week's
        ("ret5e6f7a8b", "private", 10.25, 20.75, "2026-11-08"),
        ("synth-0a1b2c3d", "public", 10.35, 20.85, "2026-11-08"),
        ("ret9c0d1e2f", "public", 10.45, 20.95, "2026-11-01"),  # 8 days: a node gone quiet
        ("ret3a4b5c6d", "public", 10.65, 21.05, "2026-11-08"),
    )
    _rewrite(data_dir, "ret3a4b5c6d", rank_status="config_suspect")
    seen = []

    run_once(tower_outcome_settings(), lambda: _NOW, _client(seen, _taken()))

    assert [row["node_id"] for row in json.loads(seen[0].content)] == [_PUBLIC]


async def test_an_older_tower_finder_gets_the_batch_again_without_the_newer_keys(data_dir):
    await _seed(data_dir, (_PUBLIC, "public", 10.1234, 20.5678, "2026-11-08"))
    seen = []
    refused = httpx.Response(422, content=_OLDER_422, headers={"content-type": "application/json"})

    run_once(tower_outcome_settings(), lambda: _NOW, _client(seen, refused, _taken()))

    first, second = (json.loads(request.content) for request in seen)
    assert first == [_ROW]
    assert second == [
        {key: value for key, value in _ROW.items() if key not in ("system_constant_db", "range_50pct_km")}
    ]
    assert json.loads((data_dir / "tower_outcome_posted.json").read_text())["week"] == "2026-W46"


async def test_a_posted_row_names_the_tower_its_window_was_fitted_on(data_dir):
    await _seed(data_dir, (_PUBLIC, "public", 10.1234, 20.5678, "2026-11-07"))
    await _move(_PUBLIC, tx_lat=10.9, tx_lon=20.9, tx_callsign="QBBB", fc_hz=479e6)
    seen = []

    run_once(tower_outcome_settings(), lambda: _NOW, _client(seen, _taken()))

    assert json.loads(seen[0].content) == [{**_ROW, "observed_at": "2026-11-08T00:00:00+00:00"}]


async def test_a_node_whose_receiver_moved_after_its_window_is_not_posted(data_dir):
    await _seed(data_dir, (_PUBLIC, "public", 10.1234, 20.5678, "2026-11-07"))
    await _move(_PUBLIC, rx_lat=10.3, rx_lon=20.7)
    seen = []

    run_once(tower_outcome_settings(), lambda: _NOW, _client(seen, _taken()))

    assert seen == []


async def test_a_window_spanning_a_receiver_move_of_over_1_km_under_one_tower_is_not_posted(data_dir):
    await _seed(data_dir, (_PUBLIC, "public", 10.1234, 20.5678, "2026-11-08"))
    await _move(_PUBLIC, rx_lat=10.4, rx_lon=20.9)  # 11-08 12:00, 47 km away, the same tower
    _rewrite(data_dir, _PUBLIC, config_version=2)  # the version the window ended on
    seen = []

    run_once(tower_outcome_settings(), lambda: _NOW, _client(seen, _taken()))

    assert seen == []


async def test_a_receiver_nudged_metres_after_its_window_still_posts_at_the_active_site(data_dir):
    await _seed(data_dir, (_PUBLIC, "public", 10.1234, 20.5678, "2026-11-07"))
    await _move(_PUBLIC, rx_lat=10.1236)  # 22 m north
    seen = []

    run_once(tower_outcome_settings(), lambda: _NOW, _client(seen, _taken()))

    assert json.loads(seen[0].content) == [{**_ROW, "rx_lat": 10.1229, "observed_at": "2026-11-08T00:00:00+00:00"}]


async def test_a_figure_outside_tower_finders_bounds_is_left_out_and_the_batch_still_goes(data_dir, caplog):
    other = "ret1a2b3c4d"
    await _seed(
        data_dir, (_PUBLIC, "public", 10.1234, 20.5678, "2026-11-08"), (other, "public", 10.3, 20.7, "2026-11-08")
    )
    # tower-finder takes snr_median_db in [-100, 200] and each class's range in [0, 2000].
    _rewrite(data_dir, other, snr_median_db=250.0, range_50pct_km={"widebody": 2500.0, "narrowbody": 61.0})
    seen = []

    run_once(tower_outcome_settings(), lambda: _NOW, _client(seen, _taken()))

    whole, cut = json.loads(seen[0].content)
    assert whole == _ROW
    assert ("snr_median_db" in cut, cut["range_50pct_km"]) == (False, {"narrowbody": 61.0})
    assert f"{other} snr_median_db=250.0 is outside" in caplog.text
