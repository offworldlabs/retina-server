"""services/owner_views.py: what an owner is shown of one of their own nodes.

The analytics tests run with the fuzz on, because that is the setting under
which the owner's view and the public one differ. They feed the analytics
manager and node_bias numpy scalars through their own recorders, which store
whatever numbers they are handed: every other payload built from those stores
is serialised with OPT_SERIALIZE_NUMPY for that reason, and this view promises
plain JSON instead, so a test fed only Python floats would prove nothing.
"""

import asyncio
import math
from datetime import UTC, datetime, timedelta

import numpy as np
import orjson
import pytest
from sqlalchemy import delete

from config.constants import NODE_FUZZ_MIN_KM_DEFAULT
from core import state
from core.nodes import NodeEvent, NodeReport
from core.users import async_session_maker
from routes.node_schemas import HeartbeatRequest
from services import node_bias, node_config, owner_views, publication
from services import public_location as pl
from services.geo import haversine_km
from services.node_config_store import upsert_config
from services.node_report_store import record_report
from services.polled_registration import _actor as admin_actor
from tests.node_helpers import register_test_node
from tests.ownership_helpers import own_registered_node, register_node_row

# A tenure from before anything these tests record. What each view holds is
# pinned here; how the tenure bounds it, by tests/test_owner_tenure.py.
_ALWAYS = datetime(2000, 1, 1, tzinfo=UTC)

_CALLER = "6f1c2b9e-3d4a-4c5b-8e7f-0a1b2c3d4e5f"
_PREVIOUS_OWNER = "0b7e9d2c-1a3f-4e6d-9c8b-7a6f5e4d3c2b"

_NODE = "ret0123abcd"
_RX_LAT, _RX_LON = 51.421337, -0.910233
_TX_LAT, _TX_LON = 51.371012, -0.880145
_GEOMETRY = {
    "rx_lat": _RX_LAT,
    "rx_lon": _RX_LON,
    "rx_alt_ft": 120.0,
    "tx_lat": _TX_LAT,
    "tx_lon": _TX_LON,
    "tx_alt_ft": 900.0,
    "fc_hz": 570_000_000.0,
    "fs_hz": 2_000_000.0,
    "max_range_km": 50.0,
}

# validate_config's input, as tests/conftest.py transcribes it.
_WIRE_CONFIG = {
    **_GEOMETRY,
    "tx_callsign": "Crystal Palace",
    "beam_width_deg": 41.0,
    "beam_azimuth_deg": None,
    "cpi_s": 0.5,
    "delay_tolerance_us": 6.67,
    "doppler_tolerance_hz": 5.0,
}

_OWNER_SUMMARY_KEYS = {
    "node_id",
    "node_ref",
    "trust",
    "detection_area",
    "metrics",
    "reputation",
    "coverage_map",
    "empirical_coverage",
    "node_bias",
}

# Rounding the published coordinate to 4 decimals moves it by up to ~8 m.
_ROUNDING_SLACK_KM = 0.02


@pytest.fixture(autouse=True)
def _fuzz_on(monkeypatch):
    monkeypatch.setenv("NODE_FUZZ_MODE", "on")
    monkeypatch.setenv("NODE_FUZZ_SALT", "test-salt-for-owner-views")
    monkeypatch.delenv("NODE_FUZZ_MIN_KM", raising=False)
    monkeypatch.delenv("NODE_FUZZ_MAX_KM", raising=False)
    pl._reset_for_tests()
    yield
    pl._reset_for_tests()


@pytest.fixture(autouse=True)
def _no_reports_left():
    """Delete the node_reports rows a test wrote.

    conftest's _clean_db does not truncate node_reports, and the table carries a
    foreign key to nodes, so a row left here would fail the next test's
    DELETE FROM nodes.
    """
    yield

    async def _delete():
        async with async_session_maker() as session:
            await session.execute(delete(NodeReport))
            await session.commit()

    asyncio.run(_delete())


def _plain(value):
    return orjson.loads(orjson.dumps(value, option=orjson.OPT_SERIALIZE_NUMPY))


def _analyse(node_id: str, geometry: dict | None = None) -> None:
    """Register `node_id` as an entry point does, and give it analytics with numpy in them."""
    register_test_node(node_id, {"node_id": node_id, **(_GEOMETRY if geometry is None else geometry)})
    state.node_analytics.record_detection_frame(
        node_id,
        {
            "delay": np.array([12.5, 30.25, 41.75]),
            "doppler": np.array([-40.0, 85.5, 12.0]),
            "snr": np.array([14.2, 9.8, 11.1]),
            "timestamp": 1_760_000_000_000,
        },
    )
    # Signed residuals through the claim lane's own entry point, as numpy.float64.
    for i in range(12):
        node_bias.record_claim_residual(
            node_id, f"a{i:05x}", np.float64(0.8) + 0.01 * i, np.float64(-2.5), 1_760_000_000_000 + i * 1000
        )
    area = state.node_analytics.detection_areas.get(node_id)
    if area is None:
        return
    for i in range(5):
        area.record_verified_detection(np.float64(_RX_LAT + 0.1 + i * 0.01), np.float64(_RX_LON + 0.1), f"abc{i:03d}")
    # Thirty calibration points over six adjacent bearings: enough for the
    # evidence-only polygon to draw a lobe with the receiver as its apex.
    coverage = state.node_analytics.empirical_coverages[node_id]
    for step in range(6):
        rad = math.radians(30.0 + 5.0 * step)
        for i in range(5):
            range_km = 10.0 + i
            lat = _RX_LAT + range_km * math.cos(rad) / 111.19
            lon = _RX_LON + range_km * math.sin(rad) / (111.19 * math.cos(math.radians(_RX_LAT)))
            coverage.add_point(lat, lon)


def _blocks(summary: dict) -> list[dict]:
    return [summary[key] for key in sorted(_OWNER_SUMMARY_KEYS - {"node_id", "node_ref"}) if summary[key] is not None]


# ── Analytics ────────────────────────────────────────────────────────────────


class TestOwnerSummary:
    async def test_a_private_nodes_owner_sees_the_truth_the_public_copy_moves(self):
        await register_node_row(_NODE, "private")
        ref = await own_registered_node(_NODE, _CALLER)
        _analyse(_NODE)
        assert publication.is_private(_NODE)
        raw = state.node_analytics.get_node_summary(_NODE)

        owner = owner_views.owner_summary(_NODE)
        public = pl.public_node_summary(_NODE, raw)

        assert owner is not None
        assert owner["node_id"] == _NODE
        assert owner["node_ref"] == ref
        # The receiver exactly as configured, where the public copy is displaced.
        assert owner["detection_area"]["rx"] == {"lat": _RX_LAT, "lon": _RX_LON}
        public_rx = public["detection_area"]["rx"]
        assert haversine_km(_RX_LAT, _RX_LON, public_rx["lat"], public_rx["lon"]) >= (
            NODE_FUZZ_MIN_KM_DEFAULT - _ROUNDING_SLACK_KM
        )
        # Kept here, dropped there.
        furthest = owner["detection_area"]["furthest_detections"]
        assert len(furthest) == 5
        assert furthest == _plain(raw["detection_area"]["furthest_detections"])
        assert "furthest_detections" not in public["detection_area"]
        # The uncertainty radius qualifies a fuzzed coordinate, and there is none.
        assert "location_uncertainty_km" in public_rx
        assert "location_uncertainty_km" not in orjson.dumps(owner).decode()
        # The coverage polygon stays on the receiver too.
        polygon = owner["empirical_coverage"]["polygon"]
        assert polygon
        assert polygon == _plain(raw["empirical_coverage"]["polygon"])
        assert public["empirical_coverage"]["polygon"] != polygon

    async def test_it_carries_exactly_the_contracts_keys(self):
        ref = await own_registered_node(_NODE, _CALLER)
        _analyse(_NODE)

        owner = owner_views.owner_summary(_NODE)

        assert owner is not None
        assert set(owner) == _OWNER_SUMMARY_KEYS
        assert owner["node_ref"] == ref
        assert all(isinstance(owner[key], dict) for key in _OWNER_SUMMARY_KEYS - {"node_id", "node_ref"})

    async def test_the_node_is_named_once_at_the_top_as_the_public_route_names_it(self):
        await own_registered_node(_NODE, _CALLER)
        _analyse(_NODE)

        owner = owner_views.owner_summary(_NODE)

        assert owner is not None
        assert all("node_id" not in block for block in _blocks(owner))
        # A copy: the manager's own blocks still name their node.
        assert state.node_analytics.get_node_summary(_NODE)["trust"]["node_id"] == _NODE

    async def test_numpy_from_the_recorders_comes_out_as_plain_json(self):
        await own_registered_node(_NODE, _CALLER)
        _analyse(_NODE)
        raw = state.node_analytics.get_node_summary(_NODE)
        raw_bias = node_bias.node_summary(_NODE)
        assert raw_bias is not None
        # The premise: without the conversion these blocks are not JSON at all.
        for block in (raw["detection_area"], raw["metrics"], raw_bias):
            with pytest.raises(orjson.JSONEncodeError):
                orjson.dumps(block)

        owner = owner_views.owner_summary(_NODE)

        assert owner is not None
        assert orjson.loads(orjson.dumps(owner)) == owner
        assert type(owner["node_bias"]["bias_delay_us"]) is float
        assert owner["node_bias"]["bias_delay_us"] == float(raw_bias["bias_delay_us"])
        assert type(owner["metrics"]["avg_snr"]) is float
        assert owner["detection_area"]["observed_delay_range_us"] == [12.5, 41.75]

    async def test_a_block_the_manager_does_not_hold_is_null(self):
        # A node registered without a receiver position: the manager keeps its
        # trust, metrics, reputation and coverage map, and has no detection
        # area or polygon to give it.
        await own_registered_node(_NODE, _CALLER)
        _analyse(_NODE, {**_GEOMETRY, "rx_lat": None, "rx_lon": None})

        owner = owner_views.owner_summary(_NODE)

        assert owner is not None
        assert set(owner) == _OWNER_SUMMARY_KEYS
        assert owner["detection_area"] is None
        assert owner["empirical_coverage"] is None
        for key in ("trust", "metrics", "reputation", "coverage_map", "node_bias"):
            assert isinstance(owner[key], dict), key

    async def test_no_analytics_yet_is_none(self):
        await own_registered_node(_NODE, _CALLER)

        assert owner_views.owner_summary(_NODE) is None

    async def test_a_node_without_a_handle_still_answers_with_a_null_ref(self):
        # Analytics but no registry row: what an owned node looks like for the
        # moments before the ref cache next refreshes.
        _analyse(_NODE)

        owner = owner_views.owner_summary(_NODE)

        assert owner is not None
        assert owner["node_ref"] is None
        assert owner["detection_area"]["rx"] == {"lat": _RX_LAT, "lon": _RX_LON}

    async def test_a_neighbour_is_named_by_its_ref_and_never_by_its_id(self, penalties_on):
        neighbour, stranger = "ret0a1b2c3d", "ret0badcafe"
        await own_registered_node(_NODE, _CALLER)
        neighbour_ref = await register_node_row(neighbour)
        _analyse(_NODE)
        # The stranger has analytics but no registry row, so no handle at all.
        register_test_node(stranger, {"node_id": stranger, **_GEOMETRY})
        reputation = state.node_analytics.reputations[_NODE]
        reputation.evaluate_neighbour_consistency(0.0, 0.9, neighbour_id=neighbour)
        reputation.evaluate_neighbour_consistency(0.0, 0.9, neighbour_id=stranger)
        assert len(reputation.penalties) == 2

        body = orjson.dumps(owner_views.owner_summary(_NODE)).decode()

        assert neighbour not in body
        assert stranger not in body
        assert f"Inconsistent with trusted neighbour {neighbour_ref}" in body
        assert "Inconsistent with trusted neighbour [unpublished node]" in body

    async def test_a_private_neighbour_is_not_named_even_by_its_ref(self, penalties_on):
        """Somebody else's private node is theirs to see: its ref would tell
        this owner it exists and that it sits beside their node."""
        neighbour = "ret0a1b2c3d"
        await own_registered_node(_NODE, _CALLER)
        await register_node_row(_NODE, "private")
        neighbour_ref = await register_node_row(neighbour, "private")
        publication._reset_for_tests()
        _analyse(_NODE)
        state.node_analytics.reputations[_NODE].evaluate_neighbour_consistency(0.0, 0.9, neighbour_id=neighbour)

        owner = owner_views.owner_summary(_NODE)
        body = orjson.dumps(owner).decode()

        assert owner is not None
        assert neighbour not in body
        assert neighbour_ref not in body
        assert "Inconsistent with trusted neighbour [unpublished node]" in body
        # The node's own privacy does not hide it from its owner.
        assert owner["node_ref"] is not None


class TestOwnerSummaries:
    async def test_keyed_by_ref_private_included_and_only_nodes_with_analytics(self):
        public_node, quiet_node = "ret1a2b3c4d", "ret2b3c4d5e"
        await register_node_row(_NODE, "private")
        private_ref = await own_registered_node(_NODE, _CALLER)
        public_ref = await own_registered_node(public_node, _CALLER)
        await own_registered_node(quiet_node, _CALLER)
        _analyse(_NODE)
        _analyse(public_node)

        summaries = owner_views.owner_summaries([_NODE, public_node, quiet_node])

        assert set(summaries) == {private_ref, public_ref}
        assert summaries[private_ref]["node_id"] == _NODE
        assert summaries[public_ref]["node_id"] == public_node
        # Unmoved, as the per-node view is.
        assert summaries[private_ref]["detection_area"]["rx"] == {"lat": _RX_LAT, "lon": _RX_LON}
        assert orjson.loads(orjson.dumps(summaries)) == summaries

    async def test_a_node_without_a_handle_is_left_out(self):
        _analyse(_NODE)

        assert owner_views.owner_summaries([_NODE]) == {}

    def test_nothing_owned_is_nothing(self):
        assert owner_views.owner_summaries([]) == {}


# ── The heartbeat report ─────────────────────────────────────────────────────


async def _beat(node_id: str, now: datetime, **beat) -> None:
    async with async_session_maker() as session:
        async with session.begin():
            await record_report(session, node_id, HeartbeatRequest.model_validate(beat), now)


class TestLatestReport:
    _T1 = datetime(2026, 10, 12, 9, 30, 15, 123456, tzinfo=UTC)
    _T2 = _T1 + timedelta(minutes=1)
    _T3 = _T2 + timedelta(minutes=1)

    async def test_none_before_the_first_beat(self):
        await own_registered_node(_NODE, _CALLER)

        assert await owner_views.latest_report(_NODE, since=_ALWAYS) is None

    async def test_the_last_beat_with_errors_from_every_beat(self):
        await own_registered_node(_NODE, _CALLER)
        boot = {"boot_id": "b00t0001", "config_version": None}
        await _beat(_NODE, self._T1, state="starting", uptime_s=5, errors=["sdr not found"], **boot)
        await _beat(_NODE, self._T2, state="streaming", uptime_s=65, **boot)
        await _beat(
            _NODE,
            self._T3,
            **{**boot, "config_version": 3},
            state="streaming",
            uptime_s=125,
            health={"cpu_pct": 12.5, "disk_free_mb": 2048, "temp_c": 48.0, "blah2": "up"},
            versions={"retina_node": "1.4.2"},
            errors=["sdr overrun"],
        )

        report = await owner_views.latest_report(_NODE, since=_ALWAYS)

        assert report == {
            "received_at": "2026-10-12T09:32:15Z",
            # When it last changed state, not when it last spoke.
            "state": "streaming",
            "state_since": "2026-10-12T09:31:15Z",
            "uptime_s": 125,
            "config_version": 3,
            "health": {"cpu_pct": 12.5, "disk_free_mb": 2048, "temp_c": 48.0, "blah2": "up", "adsb": None},
            "versions": {"owl_os": None, "retina_node": "1.4.2", "blah2_image": None, "retina_tracker": None},
            "errors": [
                {"at": "2026-10-12T09:30:15Z", "message": "sdr not found"},
                {"at": "2026-10-12T09:32:15Z", "message": "sdr overrun"},
            ],
        }
        assert orjson.loads(orjson.dumps(report)) == report

    async def test_a_beat_without_health_or_versions_reads_as_empty_records(self):
        await own_registered_node(_NODE, _CALLER)
        await _beat(_NODE, self._T1, state="starting", uptime_s=5, boot_id="b00t0001", config_version=None)

        report = await owner_views.latest_report(_NODE, since=_ALWAYS)

        assert report is not None
        assert report["health"] == {}
        assert report["versions"] == {}
        assert report["errors"] == []
        assert report["state_since"] == "2026-10-12T09:30:15Z"

    async def test_another_nodes_report_is_not_this_ones(self):
        other = "ret9f8e7d6c"
        await own_registered_node(_NODE, _CALLER)
        await register_node_row(other)
        await _beat(other, self._T1, state="streaming", uptime_s=5, boot_id="b00t0002", config_version=None)

        assert await owner_views.latest_report(_NODE, since=_ALWAYS) is None


# ── Configuration history ────────────────────────────────────────────────────


async def _configure(node_id: str, *frequencies: float) -> list[dict]:
    """Write one configuration version per frequency, as registration and PUT do."""
    written = []
    for fc_hz in frequencies:
        config = node_config.validate_config({**_WIRE_CONFIG, "fc_hz": fc_hz})
        async with async_session_maker() as session:
            async with session.begin():
                await upsert_config(session, node_id, config)
        written.append(config)
    return written


class TestConfigHistory:
    async def test_newest_first_with_what_superseded_each(self):
        await own_registered_node(_NODE, _CALLER)
        written = await _configure(_NODE, 570e6, 571e6, 572e6)

        history = await owner_views.config_history(_NODE, 50, since=_ALWAYS)

        assert [v["version"] for v in history] == [3, 2, 1]
        assert history[0]["superseded_at"] is None
        for version in history[1:]:
            assert version["superseded_at"].endswith("Z")
        for version in history:
            assert version["created_at"].endswith("Z")
        for version, config in zip(history, reversed(written), strict=True):
            assert {k: v for k, v in version.items() if k not in ("version", "created_at", "superseded_at")} == config
        assert orjson.loads(orjson.dumps(history)) == history

    async def test_it_carries_exactly_the_contracts_keys(self):
        await own_registered_node(_NODE, _CALLER)
        await _configure(_NODE, 570e6)

        (version,) = await owner_views.config_history(_NODE, 50, since=_ALWAYS)

        assert set(version) == {
            "version",
            "created_at",
            "superseded_at",
            "fc_hz",
            "fs_hz",
            "rx_lat",
            "rx_lon",
            "rx_alt_ft",
            "tx_lat",
            "tx_lon",
            "tx_alt_ft",
            "tx_callsign",
            "beam_width_deg",
            "beam_azimuth_deg",
            "max_range_km",
            "cpi_s",
            "delay_tolerance_us",
            "doppler_tolerance_hz",
        }

    async def test_limit_keeps_the_newest(self):
        await own_registered_node(_NODE, _CALLER)
        await _configure(_NODE, 570e6, 571e6, 572e6)

        assert [v["version"] for v in await owner_views.config_history(_NODE, 2, since=_ALWAYS)] == [3, 2]
        assert [v["version"] for v in await owner_views.config_history(_NODE, 1, since=_ALWAYS)] == [3]
        assert await owner_views.config_history(_NODE, 0, since=_ALWAYS) == []
        # SQLite reads a negative LIMIT as no limit at all.
        assert await owner_views.config_history(_NODE, -1, since=_ALWAYS) == []

    async def test_only_this_nodes_versions(self):
        other = "ret9f8e7d6c"
        await own_registered_node(_NODE, _CALLER)
        await register_node_row(other)
        await _configure(other, 570e6, 571e6)

        assert await owner_views.config_history(_NODE, 50, since=_ALWAYS) == []


# ── Events ───────────────────────────────────────────────────────────────────

# One row per shape a writer stores, oldest first, with the role each must read
# as to _CALLER.
_EVENTS = [
    # services/polled_registration.register: the owner at the time, by user id.
    ("registered", 1, _PREVIOUS_OWNER, "owner"),
    # routes/admin_polled_radars.py's own f-string.
    ("graduated", 1, "admin:someone@example.com", "admin"),
    # services/polled_registration._actor, for connect and withdraw.
    ("returned_to_probation", 2, admin_actor({"email": "staff@example.com"}), "admin"),
    # The owner edits, by the caller's own id.
    ("renamed", None, _CALLER, "you"),
    ("made_private", None, _CALLER, "you"),
    # A background job, by whatever name it chose.
    ("returned_to_probation", 3, "poller", "system"),
    # services/polled_radars.remove_polled_radar: the owner by user id.
    ("removed", 3, _PREVIOUS_OWNER, "owner"),
    ("made_public", None, "", "system"),
]


async def _record_events(node_id: str) -> None:
    start = datetime(2026, 10, 12, 8, 0, 0, tzinfo=UTC)
    async with async_session_maker() as session:
        async with session.begin():
            for i, (kind, epoch, actor, _role) in enumerate(_EVENTS):
                session.add(
                    NodeEvent(
                        node_id=node_id, kind=kind, epoch=epoch, actor=actor, occurred_at=start + timedelta(minutes=i)
                    )
                )
                # One flush per row, so ids follow the list order.
                await session.flush()


class TestOwnerEvents:
    async def test_newest_first_with_each_actor_reduced_to_a_role(self):
        await own_registered_node(_NODE, _CALLER)
        await _record_events(_NODE)

        events = await owner_views.owner_events(_NODE, 100, _CALLER, since=_ALWAYS)

        assert events == [
            {"kind": kind, "at": f"2026-10-12T08:{i:02d}:00Z", "actor": role, "epoch": epoch}
            for i, (kind, epoch, _actor, role) in reversed(list(enumerate(_EVENTS)))
        ]

    async def test_no_stored_actor_reaches_the_output(self):
        await own_registered_node(_NODE, _CALLER)
        await _record_events(_NODE)

        body = orjson.dumps(await owner_views.owner_events(_NODE, 100, _CALLER, since=_ALWAYS)).decode()

        for _kind, _epoch, actor, _role in _EVENTS:
            if actor:
                assert actor not in body
        assert "example.com" not in body
        assert "admin:" not in body

    async def test_the_same_rows_read_from_another_owners_side(self):
        await own_registered_node(_NODE, _PREVIOUS_OWNER)
        await _record_events(_NODE)

        events = await owner_views.owner_events(_NODE, 100, _PREVIOUS_OWNER, since=_ALWAYS)

        roles = [e["actor"] for e in reversed(events)]
        assert roles == ["you", "admin", "admin", "owner", "owner", "system", "you", "system"]

    async def test_limit_keeps_the_newest(self):
        await own_registered_node(_NODE, _CALLER)
        await _record_events(_NODE)

        events = await owner_views.owner_events(_NODE, 3, _CALLER, since=_ALWAYS)

        assert [e["kind"] for e in events] == ["made_public", "removed", "returned_to_probation"]
        assert await owner_views.owner_events(_NODE, 0, _CALLER, since=_ALWAYS) == []
        assert await owner_views.owner_events(_NODE, -1, _CALLER, since=_ALWAYS) == []

    async def test_only_this_nodes_events(self):
        other = "ret9f8e7d6c"
        await own_registered_node(_NODE, _CALLER)
        await register_node_row(other)
        await _record_events(other)

        assert await owner_views.owner_events(_NODE, 100, _CALLER, since=_ALWAYS) == []

    @pytest.mark.parametrize(
        ("actor", "role"),
        [
            (None, "system"),
            ("", "system"),
            (_CALLER, "you"),
            ("admin:", "admin"),
            (f"admin:{_CALLER}", "admin"),
            (_PREVIOUS_OWNER, "owner"),
            # The anonymous administrator of a bypass deployment owns by this id.
            ("00000000-0000-0000-0000-000000000000", "owner"),
            ("scheduler", "system"),
            # Not how any writer spells a user id.
            (_PREVIOUS_OWNER.upper(), "system"),
        ],
    )
    def test_actor_roles(self, actor, role):
        assert owner_views._actor_role(actor, _CALLER) == role
