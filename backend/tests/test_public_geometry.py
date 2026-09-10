"""services/public_geometry.py, and the routes that publish through it.

The module's own docstring carries the rule and the per-field reasoning; these
assert it, and that every unauthenticated surface actually applies it.
"""

import time

import orjson
import pytest
from fastapi.testclient import TestClient

from core import state
from services.public_geometry import _RECEIVER_RELATIVE, without_receiver_geometry
from services.tasks.analytics_refresh import _publish_mlat_verification


class TestWhatIsWithheld:
    def test_a_beam_margin_goes_and_its_envelope_stays(self):
        """The margin varies with the true geometry; the envelope is the
        per-node constant /api/radar/analytics already publishes."""
        entry = {
            "node_id": "ret1a2b3c4d",
            "range_km": 6.2,
            "bearing_off_deg": 73.9,
            "bistatic_km": 41.0,
            "max_range_km": 59.9,
            "half_width_deg": 21.0,
            "rule": "bearing",
        }
        assert without_receiver_geometry(entry) == {
            "node_id": "ret1a2b3c4d",
            "max_range_km": 59.9,
            "half_width_deg": 21.0,
            "rule": "bearing",
        }

    def test_the_learned_fov_read_outs_go(self):
        out = without_receiver_geometry({"fov_limit_km": 40.0, "fov_state": "closed", "keep": 1})
        assert out == {"keep": 1}

    def test_the_shadow_verdicts_go(self):
        out = without_receiver_geometry({"fov_verdict": [{"today_pass": True}], "keep": 1})
        assert out == {"keep": 1}

    def test_the_contamination_verdict_goes(self):
        out = without_receiver_geometry({"foreign_node_ids": ["ret1a2b3c4d"], "contaminated": True, "keep": 1})
        assert out == {"keep": 1}

    def test_the_per_node_delays_go(self):
        out = without_receiver_geometry({"measured_delay_us": 120.0, "delay_match_us": 0.4, "keep": 1})
        assert out == {"keep": 1}

    def test_the_furthest_detections_go(self):
        out = without_receiver_geometry({"furthest_detections": [{"distance_km": 71.2}], "keep": 1})
        assert out == {"keep": 1}


class TestHowItWalks:
    def test_a_withheld_field_goes_at_any_depth(self):
        payload = {"a": [{"b": {"c": [{"range_km": 1.0, "keep": 2}]}}]}
        assert without_receiver_geometry(payload) == {"a": [{"b": {"c": [{"keep": 2}]}}]}

    def test_a_tuple_comes_back_as_a_list(self):
        """Which is what orjson would have serialised it as anyway."""
        assert without_receiver_geometry({"a": ({"range_km": 1.0, "keep": 2},)}) == {"a": [{"keep": 2}]}

    def test_the_input_is_not_edited(self):
        payload = {"range_km": 1.0, "keep": 2}
        without_receiver_geometry(payload)
        assert payload == {"range_km": 1.0, "keep": 2}

    def test_a_payload_with_nothing_withheld_is_unchanged(self):
        payload = {"max_range_km": 59.9, "tracks": [{"hex": "abc123"}]}
        assert without_receiver_geometry(payload) == payload


class TestNodeScoped:
    def test_the_nodes_own_solve_goes_when_scoped(self):
        out = without_receiver_geometry({"solver_lat": 34.9, "solver_lon": -82.4, "keep": 1}, node_scoped=True)
        assert out == {"keep": 1}

    def test_it_stays_when_not_scoped(self):
        """A multinode position has no single receiver behind it."""
        payload = {"solver_lat": 34.9, "solver_lon": -82.4, "keep": 1}
        assert without_receiver_geometry(payload) == payload

    def test_scoping_still_withholds_everything_else(self):
        out = without_receiver_geometry({"range_km": 1.0, "solver_lat": 34.9, "keep": 1}, node_scoped=True)
        assert out == {"keep": 1}


# ── The surfaces ──────────────────────────────────────────────────────────────
# Route-level, because the helper being correct proves nothing about whether a
# route calls it: every one of these fails if its call site is dropped.

_NODE_ID = "retdeadbeef"
_HEX = "mn0123456789"

# The receiver-relative fields as the solver actually spells them on a record
# (services/tasks/solver.py) and on a verification track (analytics_refresh.py).
_WITHHELD_ON_A_SOLVE = {
    "range_km": 6.2,
    "bearing_off_deg": 73.9,
    "bistatic_km": 41.0,
    "fov_limit_km": 40.0,
    "fov_state": "closed",
    "foreign_node_ids": ["ret1a2b3c4d"],
    "contaminated": True,
}


def _client() -> TestClient:
    from main import app

    return TestClient(app)


def _solve_record(**over) -> dict:
    rec = {
        "ts_ms": int(time.time() * 1000),
        "solve_key": "mn-dark-0123456789",
        "solver_hex": _HEX,
        "outcome": "published",
        "raw_lat": 34.85,
        "raw_lon": -82.39,
        "n_nodes": 2,
        "beam_failures": [{"node_id": "ret1a2b3c4d", "max_range_km": 59.9, **_WITHHELD_ON_A_SOLVE}],
        **_WITHHELD_ON_A_SOLVE,
    }
    rec.update(over)
    return rec


def _names(value) -> set[str]:
    """Every key appearing anywhere in a decoded payload."""
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _names(v)}
    if isinstance(value, list):
        return {k for v in value for k in _names(v)}
    return set()


@pytest.fixture
def clean_state():
    solves, known = list(state.mlat_solve_history), list(state.mlat_solve_history_known)
    skips = list(state.solver_resolve_skips_recent)
    node_bytes = dict(state.latest_node_verification_bytes)
    mlat_bytes = state.latest_mlat_verification_bytes
    state.mlat_solve_history.clear()
    state.mlat_solve_history_known.clear()
    state.solver_resolve_skips_recent.clear()
    state.latest_node_verification_bytes.clear()
    yield
    state.mlat_solve_history.clear()
    state.mlat_solve_history.extend(solves)
    state.mlat_solve_history_known.clear()
    state.mlat_solve_history_known.extend(known)
    state.solver_resolve_skips_recent.clear()
    state.solver_resolve_skips_recent.extend(skips)
    state.latest_node_verification_bytes.clear()
    state.latest_node_verification_bytes.update(node_bytes)
    state.latest_mlat_verification_bytes = mlat_bytes


@pytest.mark.usefixtures("clean_state")
class TestTheRoutesApplyIt:
    def test_the_whole_window_dump_withholds(self):
        state.mlat_solve_history.append(_solve_record())
        data = _client().get("/api/test/mlat-history?all=1").json()
        assert data["n_records"] == 1
        assert _names(data) & set(_WITHHELD_ON_A_SOLVE) == set()

    def test_one_markers_solves_withhold(self):
        state.mlat_solve_history.append(_solve_record())
        data = _client().get(f"/api/test/mlat-history?hex={_HEX}").json()
        assert data["n_solves"] == 1
        assert _names(data) & set(_WITHHELD_ON_A_SOLVE) == set()

    def test_the_nearby_rejects_withhold(self):
        """A second payload key on the same response, and the one the flat
        per-record pass reached only because someone remembered it."""
        state.mlat_solve_history.append(_solve_record())
        state.mlat_solve_history.append(_solve_record(outcome="rejected_rms", solver_hex="mn9999999999"))
        data = _client().get(f"/api/test/mlat-history?hex={_HEX}").json()
        assert data["rejects_nearby"]["n"] == 1
        assert _names(data) & set(_WITHHELD_ON_A_SOLVE) == set()

    def test_the_resolve_skips_dump_withholds(self):
        state.solver_resolve_skips_recent.append(
            {"ts_ms": int(time.time() * 1000), "lane": "dark", **_WITHHELD_ON_A_SOLVE}
        )
        data = _client().get("/api/test/mlat-history?kind=resolve_skips").json()
        assert data["n_records"] == 1
        assert _names(data) & set(_WITHHELD_ON_A_SOLVE) == set()

    def test_a_nodes_verification_withholds_its_own_solve_too(self):
        """Node-scoped: the track position IS that node's single-node solve."""
        state.latest_node_verification_bytes[_NODE_ID] = orjson.dumps(
            {
                "node_id": _NODE_ID,
                "n_matched": 1,
                "tracks": [
                    {
                        "hex": "a1b2c3",
                        "measured_delay_us": 120.0,
                        "delay_match_us": 0.4,
                        "solver_lat": 34.9,
                        "solver_lon": -82.4,
                        "truth_lat": 34.91,
                        "truth_lon": -82.41,
                        "position_error_km": 1.2,
                    }
                ],
            }
        )
        data = _client().get(f"/api/test/node/{_NODE_ID}/verification").json()
        track = data["tracks"][0]
        assert set(track) == {"hex", "truth_lat", "truth_lon", "position_error_km"}

    def test_an_unknown_node_still_answers_empty(self):
        assert _client().get("/api/test/node/retnosuchxx/verification").json() == {}

    def test_the_multinode_verification_withholds_on_write(self):
        """Stripped onto the store rather than at the route, because both of
        that store's readers are unauthenticated."""
        _publish_mlat_verification(
            {
                "n_matched": 1,
                "tracks": [{"truth_hex": "a1b2c3", "max_bistatic_angle_deg": 151.3, "solver_lat": 34.9}],
            }
        )
        data = orjson.loads(state.latest_mlat_verification_bytes)
        # solver_lat stays: a multinode position, not one node's receiver.
        assert data["tracks"][0] == {"truth_hex": "a1b2c3", "solver_lat": 34.9}


class TestTheDetectionAreaSummary:
    """node_detection_range runs the pass over DetectionAreaState.summary(),
    whose keys come from the retina_analytics submodule rather than this repo.
    The pass matches on leaf key name, so a field added there under one of the
    withheld names would vanish from the public payload with nothing saying so.
    This pins the overlap, so an upgrade that collides fails here instead.
    """

    def _area(self):
        from retina_analytics.detection_area import DetectionAreaState

        area = DetectionAreaState(node_id="ret1a2b3c4d", rx_lat=34.9, rx_lon=-82.4)
        area.update(120.0, 30.0)
        area.record_verified_detection(35.4, -82.9, "a1b2c3")
        return area

    def test_only_furthest_detections_collides(self):
        assert set(self._area().summary()) & _RECEIVER_RELATIVE == {"furthest_detections"}

    def test_so_the_pass_drops_that_and_nothing_else(self):
        summary = self._area().summary()
        assert summary["furthest_detections"], "the fixture must exercise the field being dropped"
        assert without_receiver_geometry(summary) == {k: v for k, v in summary.items() if k != "furthest_detections"}
