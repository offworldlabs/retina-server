#!/usr/bin/env python3
"""Does calibration-from-claims actually attribute points to the right aircraft?

The question this bench exists to answer.  Empirical-coverage calibration used
to come from the emit loop's ADS-B-tagged detections, and since KNOWN_LANE_MODE
defaulted to "binding" (#240) that path is either dead (synthetic nodes: every
detection is claimed, so the tracker never sees a tagged one) or adversely
selected (real nodes: only the binds claiming REFUSED get through).  Measured on
the test deployment 2026-09-13, 5-41% of the points eight nodes held lay outside
their own declared wedge, and the simulator emits a detection only INSIDE the
wedge — so every one of those is a bind to the wrong aircraft.

The replacement (services/known_claiming._calibration_from_claim) records from
the claim lane under five rules much stricter than claiming itself.  This bench
measures whether those rules buy what they cost, against simulator truth:

  * points per node-minute            — the yield;
  * wrong-hex share                   — recorded points whose claimed hex is
                                        not the aircraft that produced the
                                        detection (frame["adsb"][i]["hex"]
                                        before the tags are stripped: exact
                                        truth, not a nearest-neighbour guess);
  * out-of-wedge share                — recorded points outside the node's
                                        declared azimuth +- width/2, which for
                                        a simulated node is ground truth.

and the same two shares for the OLD rule, in both the forms it can honestly be
written as:

  * "greedy"  — every detection associate_detections_to_adsb tags, recorded at
                the tag's REPORTED position.  This is literally the old source:
                that greedy pass is what put the hex on the detection that set
                track.last_detection_adsb_hex, which is what the emit loop
                gated on.  It has no Hungarian one-to-one, no world gate and no
                visibility gate.
  * "claims"  — every claim the lane makes, recorded the same way: claiming's
                own gates (global assignment, world, visibility) and nothing
                else.  The strictly fairer comparison for the new rule, since
                the new rule is a filter on exactly this population.

Blind (the default) strips the per-detection ADS-B tags the way a real receiver
would see the frame, so only path 2 (and then path H) can claim; --tagged keeps
them and exercises path 1.  The two are genuinely different populations — see
the precedence note in claim_known_targets — so both are reported.

Usage:
    python backend/scripts/calibration_attribution_bench.py
    python backend/scripts/calibration_attribution_bench.py --tagged
    python backend/scripts/calibration_attribution_bench.py --seconds 600 --nodes 20
"""

import argparse
import os
import statistics
import sys
import time
from collections import Counter

os.environ.setdefault("RETINA_ENV", "test")
os.environ.setdefault("RADAR_API_KEY", "bench-key")
# The lane under measurement.  Set before core.state is imported: the mode is
# read once at import time, exactly as it is in the server.
os.environ.setdefault("KNOWN_LANE_MODE", "binding")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from retina_analytics.association import associate_detections_to_adsb  # noqa: E402
from retina_simulation.generator import coverage_cells, generate_fleet  # noqa: E402
from retina_simulation.orchestrator import _cells_to_metrocells  # noqa: E402
from retina_simulation.world import NodeConfig, SimulationWorld, waypoints_for_metro  # noqa: E402

from core import state  # noqa: E402
from services import known_claiming as kc  # noqa: E402
from services.geo import bearing_deg  # noqa: E402
from services.id_utils import normalize_hex_key  # noqa: E402
from services.node_registration import register_node_blocking  # noqa: E402
from services.tcp_handler import is_synthetic_node  # noqa: E402

_REJECT_REASONS = ("hold", "stale_fix", "residual", "contested", "immature")


def build_scene(seed, n_nodes, metro, min_aircraft, max_aircraft, metro_traffic_frac):
    """Fleet + world, the way FleetOrchestrator._build_world does it.

    Copied in shape from association_bench.build_scene, minus the layout knobs
    this bench has no opinion on: the metro_cells / frac_metro_traffic pair is
    kept because it is what funnels traffic through the shared core, and a
    wrong-hex bind needs two aircraft close enough to be confusable at all.
    """
    fleet = generate_fleet(
        n_nodes=n_nodes,
        metro=metro,
        n_cluster=n_nodes,
        n_clusters=1,
        use_tower_api=False,
        seed=seed,
        layout="ring",
    )
    cells = coverage_cells(n_cluster=n_nodes, n_clusters=1, metro=metro)
    world = SimulationWorld(
        center_lat=sum(c["rx_lat"] for c in fleet) / len(fleet),
        center_lon=sum(c["rx_lon"] for c in fleet) / len(fleet),
        waypoints=waypoints_for_metro(metro),
    )
    world.metro_cells = _cells_to_metrocells(cells)
    world.frac_metro_traffic = metro_traffic_frac
    for nd in fleet:
        world.add_node(
            NodeConfig(
                node_id=nd["node_id"],
                rx_lat=nd["rx_lat"],
                rx_lon=nd["rx_lon"],
                rx_alt_ft=nd["rx_alt_ft"],
                tx_lat=nd["tx_lat"],
                tx_lon=nd["tx_lon"],
                tx_alt_ft=nd["tx_alt_ft"],
                fc_hz=nd["fc_hz"],
                fs_hz=nd["fs_hz"],
                beam_azimuth_deg=nd.get("beam_azimuth_deg"),
                beam_width_deg=nd["beam_width_deg"],
                max_range_km=nd["max_range_km"],
                max_bistatic_range_km=nd.get("max_bistatic_range_km"),
            )
        )
    world.min_aircraft = min_aircraft
    world.max_aircraft = max_aircraft
    return fleet, world


def push_adsb(world, ts_ms):
    """Feed state.adsb_aircraft exactly as /api/sim/adsb/push does.

    Same record shape, same derived fields, same "world" tag — the tag matters:
    claiming's world gate drops a candidate whose world is not the claiming
    node's, and a synthetic fleet whose pushes were untagged would be measuring
    a gate that never fires in production.  Silent transponders are omitted, so
    their aircraft fall out of the candidate population the way they do live.
    """
    n = 0
    for ac in world.aircraft:
        if not ac.has_adsb or ac.adsb_silent:
            continue
        hexn = normalize_hex_key(ac.adsb_hex or "")
        if not hexn:
            continue
        rec = {
            "hex": hexn,
            "flight": ac.adsb_callsign or "",
            "lat": ac.lat,
            "lon": ac.lon,
            "alt_baro": round(ac.alt_km * 1000.0 / 0.3048),
            "gs": round(ac.speed_km_s * 1000.0 * 1.94384, 1),
            "track": round(ac.heading_deg, 1),
            "last_seen_ms": ts_ms,
            "world": "sim",
        }
        rec.update(state.adsb_derived_fields(rec))
        state.adsb_aircraft[hexn] = rec
        n += 1
    return n


def detection_truth(frame):
    """The true hex behind each detection index, or None.

    Read straight off the un-stripped frame: generate_detections_for_node
    appends one adsb slot per detection in the same order, carrying the
    aircraft's own hex, and None for a dark aircraft, a silent transponder or
    clutter.  None is not "unknown" — it is "no transponder produced this
    echo", so any hex claimed on it is wrong by construction.
    """
    tags = frame.get("adsb") or []
    out = []
    for i in range(len(frame.get("delay") or [])):
        t = tags[i] if i < len(tags) else None
        out.append(normalize_hex_key(t.get("hex") or "") if isinstance(t, dict) else None)
    return out


def in_declared_wedge(cfg, lat, lon):
    """Is (lat, lon) inside this node's declared azimuth +- width/2?

    Ground truth for a simulated node: _aircraft_in_detection_cone applies the
    same bearing test before the simulator emits a detection at all, so a
    recorded point outside the wedge cannot have come from an aircraft this
    node saw.  Range is deliberately not re-tested — a dead-reckoned position
    can sit a little past the range limit without the attribution being wrong,
    and the bearing half is where the measured failure showed up.
    """
    az = cfg.get("beam_azimuth_deg")
    width = cfg.get("beam_width_deg")
    if az is None or not width:
        return True
    b = bearing_deg(cfg["rx_lat"], cfg["rx_lon"], lat, lon)
    return abs((b - az + 180.0) % 360.0 - 180.0) <= width / 2.0


class Tally:
    """One rule's scorecard."""

    def __init__(self, label):
        self.label = label
        self.points = 0
        self.wrong_hex = 0
        self.out_of_wedge = 0
        self.per_node = Counter()

    def add(self, node_id, hexn, true_hex, cfg, lat, lon):
        self.points += 1
        self.per_node[node_id] += 1
        if hexn != true_hex:
            self.wrong_hex += 1
        if not in_declared_wedge(cfg, lat, lon):
            self.out_of_wedge += 1

    def report(self, node_minutes):
        if not self.points:
            return f"  {self.label:<20} 0 points"
        return (
            f"  {self.label:<20} {self.points:>6} points  "
            f"{self.points / node_minutes:>7.2f}/node-min  "
            f"wrong-hex {100.0 * self.wrong_hex / self.points:>5.1f}%  "
            f"out-of-wedge {100.0 * self.out_of_wedge / self.points:>5.1f}%  "
            f"nodes {len(self.per_node)}"
        )


def run(
    seed,
    seconds,
    dt,
    frame_interval,
    n_nodes,
    metro,
    min_aircraft,
    max_aircraft,
    metro_traffic_frac,
    tagged,
    hold_gap_s=None,
):
    import random

    from retina_analytics.manager import NodeAnalyticsManager

    random.seed(seed)
    state._reset_for_tests()
    kc._reset_for_tests()
    # The production rollback lever, exposed because holds-on and holds-off
    # are two genuinely different populations on a blind node: path H outranks
    # path 2, so with holds ON nearly every claim after the first on a link is
    # a hold, judged by rule 1's refreshed-hold branch, and with holds OFF the
    # same link is path 2's alone.  Both are worth measuring; holds ON is the
    # hardware-receiver case.
    hold_gap_before = kc.KNOWN_HOLD_MAX_GAP_S
    if hold_gap_s is not None:
        kc.KNOWN_HOLD_MAX_GAP_S = hold_gap_s
    # A fresh in-memory analytics manager per leg: the module-level one is
    # wired to backend/coverage_data, so registering there would both load a
    # deployment's persisted bins into the bench and write the bench's points
    # back out.  storage_dir="" is the library's "nothing persisted" mode.
    state.node_analytics = NodeAnalyticsManager(storage_dir="", fov_mode="off")
    state.adsb_aircraft.clear()
    state.known_claims.clear()
    state.known_track_holds.clear()
    state.multinode_tracks.clear()

    fleet, world = build_scene(seed, n_nodes, metro, min_aircraft, max_aircraft, metro_traffic_frac)
    cfgs = {}
    for nd in fleet:
        assert is_synthetic_node(nd["node_id"]), nd["node_id"]
        # The real registration door, so the node reads as positioned, carries
        # a geometry, and gets an EmpiricalCoverageState for points to land in.
        with state.connected_nodes_lock:
            state.connected_nodes[nd["node_id"]] = {"config": dict(nd), "is_synthetic": True}
        register_node_blocking(nd["node_id"], dict(nd))
        cfgs[nd["node_id"]] = nd

    # The new rule's verdict, straight from the decision function, so the bench
    # scores the hex and the POSITION the rule actually accepted rather than
    # re-deriving either.  Wrapping beats reading the bins: a bin records a
    # range, not which aircraft it was attributed to.
    accepted: list = []
    _decide = kc._calibration_from_claim

    def _spy(node_id, hexn, fix, extra, d_meas, f_meas, *rest):
        ok = _decide(node_id, hexn, fix, extra, d_meas, f_meas, *rest)
        if ok:
            pos = extra.get(kc._CAL_DR_KEY) or (fix.get("lat"), fix.get("lon"))
            accepted.append((node_id, hexn, d_meas, f_meas, pos))
        return ok

    kc._calibration_from_claim = _spy
    try:
        new = Tally("new rule")
        old = Tally("old rule (claims)")
        greedy = Tally("old rule (greedy)")
        node_ids = sorted(cfgs)
        n = len(node_ids)
        next_send = {nid: i * (frame_interval / n) for i, nid in enumerate(node_ids)}

        t = 0.0
        frames = 0
        claims_total = 0
        while t < seconds:
            world.step(dt, mode="adsb")
            t += dt
            ts_ms = int(t * 1000)
            push_adsb(world, ts_ms)
            due = [nid for nid in node_ids if next_send[nid] <= t]
            for nid in due:
                next_send[nid] += frame_interval
                frame = world.generate_detections_for_node(nid, ts_ms)
                if not frame.get("delay"):
                    continue
                truth = detection_truth(frame)
                if not tagged:
                    # What a real receiver sends: no truth label stapled to
                    # each detection.  See association_bench._strip_adsb.
                    frame = {k: v for k, v in frame.items() if k != "adsb"}
                frames += 1
                # (delay, doppler) -> detection index, the handle a claim gives
                # back: it carries the measurement verbatim.
                by_meas = {}
                for i, d in enumerate(frame["delay"]):
                    by_meas.setdefault((float(d), float(frame["doppler"][i])), i)

                # The literal old source, run on the same frame: the greedy
                # pass whose tags set track.last_detection_adsb_hex.  Fed the
                # unfiltered cache snapshot, because that is what
                # process_one_frame hands it — no world gate, no visibility
                # gate, no one-to-one.
                for i, tag in enumerate(
                    associate_detections_to_adsb(
                        state.node_associator.node_geometries[nid],
                        frame["delay"],
                        frame["doppler"],
                        state._adsb_for_seeding(),
                        ts_ms,
                    )
                    or []
                ):
                    if not isinstance(tag, dict) or tag.get("lat") is None:
                        continue
                    greedy.add(
                        nid,
                        normalize_hex_key(tag.get("hex") or ""),
                        truth[i] if i < len(truth) else None,
                        cfgs[nid],
                        tag["lat"],
                        tag["lon"],
                    )

                # Cleared per frame so the registry holds exactly this frame's
                # claims.  Nothing else in this bench reads it — the known lane
                # (its only consumer) is not run.
                state.known_claims.clear()
                accepted.clear()
                kc.claim_known_targets(nid, frame)

                def _true_hex(d_meas, f_meas, by_meas=by_meas, truth=truth):
                    i = by_meas.get((float(d_meas), float(f_meas)))
                    return truth[i] if i is not None and i < len(truth) else None

                for hexn, dq in state.known_claims.items():
                    for rec in dq:
                        claims_total += 1
                        fix = rec.get("adsb_fix") or {}
                        lat, lon = fix.get("lat"), fix.get("lon")
                        if lat is None or lon is None:
                            continue
                        # The OLD rule: every claim the lane makes, recorded at
                        # the claim's REPORTED fix — which is what the emit
                        # path recorded, and with no gate of its own beyond
                        # claiming's.
                        old.add(nid, hexn, _true_hex(rec["delay_us"], rec["doppler_hz"]), cfgs[nid], lat, lon)

                for node_id, hexn, d_meas, f_meas, pos in accepted:
                    if pos and pos[0] is not None:
                        new.add(node_id, hexn, _true_hex(d_meas, f_meas), cfgs[node_id], pos[0], pos[1])
    finally:
        kc._calibration_from_claim = _decide
        kc.KNOWN_HOLD_MAX_GAP_S = hold_gap_before

    node_minutes = n * seconds / 60.0
    return {
        "new": new,
        "old": old,
        "greedy": greedy,
        "frames": frames,
        "claims": claims_total,
        "node_minutes": node_minutes,
        "rejects": {r: getattr(state, f"calibration_claims_rejected_{r}") for r in _REJECT_REASONS},
        "recorded": state.calibration_points_recorded,
        "hold_claims": state.known_hold_claims,
        "follow_claims": state.known_follow_claims,
        "aircraft": len(world.aircraft),
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--seconds", type=float, default=300.0, help="simulated seconds per leg")
    ap.add_argument("--dt", type=float, default=1.0)
    ap.add_argument("--frame-interval", type=float, default=1.0)
    ap.add_argument("--nodes", type=int, default=20)
    ap.add_argument("--metro", default="gvl")
    ap.add_argument("--min-aircraft", type=int, default=25)
    ap.add_argument("--max-aircraft", type=int, default=40)
    ap.add_argument("--metro-traffic-frac", type=float, default=0.7)
    ap.add_argument(
        "--tagged",
        action="store_true",
        help="keep the per-detection ADS-B tags (path 1); default is blind (path 2/H)",
    )
    ap.add_argument("--both", action="store_true", help="run blind and tagged legs back to back")
    ap.add_argument(
        "--hold-gap",
        type=float,
        default=None,
        help="override KNOWN_HOLD_MAX_GAP_S for the run; 0 disables path H, which isolates "
        "path 2's own attribution quality from the refreshed holds' (see rule 1)",
    )
    args = ap.parse_args()

    legs = [False, True] if args.both else [args.tagged]
    for tagged in legs:
        t0 = time.time()
        r = run(
            args.seed,
            args.seconds,
            args.dt,
            args.frame_interval,
            args.nodes,
            args.metro,
            args.min_aircraft,
            args.max_aircraft,
            args.metro_traffic_frac,
            tagged,
            args.hold_gap,
        )
        label = "TAGGED (path 1)" if tagged else "BLIND (path 2/H)"
        if args.hold_gap is not None:
            label += f"  KNOWN_HOLD_MAX_GAP_S={args.hold_gap:g}"
        print(
            f"\n=== {label}  seed {args.seed}  {args.nodes} nodes  {args.seconds:.0f} s  "
            f"{r['aircraft']} aircraft  {r['frames']} frames  {r['claims']} claims  "
            f"({time.time() - t0:.1f}s wall) ==="
        )
        print(f"  node-minutes: {r['node_minutes']:.1f}")
        print(r["greedy"].report(r["node_minutes"]))
        print(r["old"].report(r["node_minutes"]))
        print(r["new"].report(r["node_minutes"]))
        print("  rejects: " + "  ".join(f"{k}={v}" for k, v in r["rejects"].items()))
        print(f"  hold_claims={r['hold_claims']}  follow_claims={r['follow_claims']}")
        if r["new"].per_node:
            per = sorted(r["new"].per_node.values())
            print(
                f"  per-node points: median {statistics.median(per):.0f}  "
                f"min {per[0]}  max {per[-1]}  nodes with any {len(per)}/{args.nodes}"
            )


if __name__ == "__main__":
    main()
