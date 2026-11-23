"""One node kind per solve (services/solver_input_purity.py).

The synthetic fleet and real hardware must never share a solve: the test
droplet's fleet sits in the real nodes' metro and sees the same live-seeded
aircraft, and a mixed solve reads on the real-only map as more real-node
evidence than exists.  Real wins when two real nodes are in the candidate,
else the synthetic subset stays; single-kind inputs pass through as the same
object; every per-node field follows the surviving nodes.
"""

from core import state
from services import solver_input_purity as purity

REAL_A, REAL_B, REAL_C = "hw-a", "hw-b", "hw-c"
SYN_1, SYN_2, SYN_3 = "synth-GVL-0001", "synth-GVL-0002", "synth-GVL-SCAT-0003"


def _m(nid, delay=10.0, doppler=5.0, t_s=0.0):
    return {"node_id": nid, "delay_us": delay, "doppler_hz": doppler, "snr": 12.0, "t_s": t_s}


def _input(node_ids, **extra):
    s_in = {
        "initial_guess": {"lat": 34.85, "lon": -82.4, "alt_km": 3.0},
        "measurements": [_m(n) for n in node_ids],
        "n_nodes": len(node_ids),
        "timestamp_ms": 1_000,
    }
    s_in.update(extra)
    return s_in


class TestSurvivors:
    def test_all_real_is_unchanged(self):
        assert purity.single_kind_survivors({REAL_A, REAL_B}) == {REAL_A, REAL_B}

    def test_all_synthetic_is_unchanged(self):
        assert purity.single_kind_survivors({SYN_1, SYN_2, SYN_3}) == {SYN_1, SYN_2, SYN_3}

    def test_two_real_nodes_win_over_any_number_of_synthetic(self):
        assert purity.single_kind_survivors({REAL_A, REAL_B, SYN_1, SYN_2, SYN_3}) == {REAL_A, REAL_B}

    def test_one_real_node_cannot_solve_alone_so_the_fleet_keeps_the_candidate(self):
        assert purity.single_kind_survivors({REAL_A, SYN_1, SYN_2}) == {SYN_1, SYN_2}

    def test_every_test_prefix_counts_as_synthetic(self):
        # tcp_handler.SYNTHETIC_NODE_PREFIXES: the fleet, both e2e families,
        # and the backend suite's own nodes.
        assert purity.single_kind_survivors({REAL_A, REAL_B, "e2e-1", "realnode-2", "test-3"}) == {REAL_A, REAL_B}


class TestRestrictSolverInput:
    def test_single_kind_input_is_the_same_object(self):
        s_in = _input([REAL_A, REAL_B, REAL_C])
        assert purity.restrict_solver_input(s_in) is s_in
        s_syn = _input([SYN_1, SYN_2])
        assert purity.restrict_solver_input(s_syn) is s_syn
        assert state.solver_input_kind_split == 0

    def test_mixed_input_keeps_the_real_nodes_and_counts(self):
        s_in = _input([REAL_A, SYN_1, REAL_B, SYN_2, SYN_3])
        out = purity.restrict_solver_input(s_in)
        assert out is not s_in
        assert [m["node_id"] for m in out["measurements"]] == [REAL_A, REAL_B]
        assert out["n_nodes"] == 2
        assert state.solver_input_kind_split == 1
        # Pure: the caller's input is untouched.
        assert s_in["n_nodes"] == 5 and len(s_in["measurements"]) == 5

    def test_one_real_plus_fleet_keeps_the_fleet(self):
        out = purity.restrict_solver_input(_input([SYN_1, REAL_A, SYN_2]))
        assert [m["node_id"] for m in out["measurements"]] == [SYN_1, SYN_2]
        assert out["n_nodes"] == 2

    def test_one_of_each_is_no_solve_at_all(self):
        assert purity.restrict_solver_input(_input([REAL_A, SYN_1])) is None
        assert state.solver_input_kind_split == 1

    def test_track_provenance_and_pairs_follow_the_survivors(self):
        s_in = _input(
            [REAL_A, REAL_B, SYN_1],
            track_ids_by_node={REAL_A: ["ta"], REAL_B: ["tb"], SYN_1: ["ts"]},
            track_ids=["ta", "tb", "ts"],
            track_pair_ids=[("ta", "ts"), ("ta", "tb")],
        )
        out = purity.restrict_solver_input(s_in)
        assert out["track_ids_by_node"] == {REAL_A: ["ta"], REAL_B: ["tb"]}
        assert out["track_ids"] == ["ta", "tb"]
        # The pair naming the dropped node's track would claim a track this
        # solve no longer uses (solver._claim_tracks).
        assert out["track_pair_ids"] == [("ta", "tb")]

    def test_cv_epochs_lose_the_other_kind_and_epochs_left_with_one_node(self):
        epochs = [
            {"t_s": 0.0, "measurements": [_m(REAL_A), _m(REAL_B), _m(SYN_1)]},
            {"t_s": 1.0, "measurements": [_m(REAL_A), _m(SYN_1)]},
            {"t_s": 2.0, "measurements": [_m(REAL_A), _m(REAL_B)]},
        ]
        out = purity.restrict_solver_input(_input([REAL_A, REAL_B, SYN_1], cv_epochs=epochs, n_epochs=3))
        assert [e["t_s"] for e in out["cv_epochs"]] == [0.0, 2.0]
        assert all({m["node_id"] for m in e["measurements"]} == {REAL_A, REAL_B} for e in out["cv_epochs"])
        assert out["n_epochs"] == 2
        # The caller's epochs are not mutated.
        assert len(epochs[0]["measurements"]) == 3

    def test_pool_spares_keep_only_the_surviving_kind(self):
        spare = [
            {**_m(REAL_C), "track_id": "tc"},
            {**_m(SYN_2), "track_id": "t2"},
            {**_m(SYN_3), "track_id": "t3"},
        ]
        s_in = _input([REAL_A, REAL_B, SYN_1], pool_measurements=spare, pool_n_nodes=6)
        out = purity.restrict_solver_input(s_in)
        assert [m["node_id"] for m in out["pool_measurements"]] == [REAL_C]
        # Measured survivors plus the spares that are left: 2 + 1.
        assert out["pool_n_nodes"] == 3

    def test_pool_spares_follow_a_synthetic_survivor_set_too(self):
        spare = [{**_m(REAL_B), "track_id": "tb"}, {**_m(SYN_3), "track_id": "t3"}]
        out = purity.restrict_solver_input(_input([REAL_A, SYN_1, SYN_2], pool_measurements=spare, pool_n_nodes=5))
        assert [m["node_id"] for m in out["pool_measurements"]] == [SYN_3]
        assert out["pool_n_nodes"] == 3


class TestRestrictClaims:
    def test_single_kind_claims_are_the_same_object(self):
        claims = {REAL_A: {"ts_ms": 1}, REAL_B: {"ts_ms": 2}}
        assert purity.restrict_claims(claims) is claims
        assert state.known_claims_kind_split == 0

    def test_mixed_claims_keep_the_real_nodes(self):
        claims = {REAL_A: {"ts_ms": 1}, SYN_1: {"ts_ms": 2}, REAL_B: {"ts_ms": 3}, SYN_2: {"ts_ms": 4}}
        assert set(purity.restrict_claims(claims)) == {REAL_A, REAL_B}
        assert state.known_claims_kind_split == 1

    def test_a_lone_real_claim_leaves_the_fleet_its_hex(self):
        claims = {REAL_A: {"ts_ms": 1}, SYN_1: {"ts_ms": 2}, SYN_2: {"ts_ms": 3}}
        assert set(purity.restrict_claims(claims)) == {SYN_1, SYN_2}

    def test_one_of_each_comes_back_short_for_the_caller_to_drop(self):
        assert set(purity.restrict_claims({REAL_A: {"ts_ms": 1}, SYN_1: {"ts_ms": 2}})) == {SYN_1}
