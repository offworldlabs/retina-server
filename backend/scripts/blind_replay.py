"""Replay PRIVATE real-data captures with all ADS-B withheld from estimation.

Run from backend: python -m scripts.blind_replay CAPTURE.jsonl --output report.json
Truth labels are assigned from measured delay/Doppler AFTER solving, never from
the output position. Ambiguous and stale references remain unscored. The report
is conditional on captured radar detections; it is not airspace-wide recall.
"""

import argparse
import bisect
import copy
import hashlib
import json
import math
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import yaml
from retina_analytics.association import InterNodeAssociator, predict_observation
from retina_analytics.constants import haversine_km, offset_latlon_m
from retina_geolocator import multinode_solver as solver
from retina_tracker import config as tracker_config
from retina_tracker.track import TrackState
from retina_tracker.tracker import Tracker

SOURCE_HASHES = {
    "replay": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    "solver": hashlib.sha256(Path(solver.__file__).read_bytes()).hexdigest(),
}


def blind_detections(frame: dict, min_snr: float) -> list[dict]:
    """Allowlist at the estimator boundary: identity and truth cannot pass."""
    return [
        {"delay": float(d), "doppler": float(f), "snr": float(s)}
        for d, f, s in zip(frame.get("delay", []), frame.get("doppler", []), frame.get("snr", []))
        if all(isinstance(v, (int, float)) and math.isfinite(v) for v in (d, f, s)) and s >= min_snr
    ]


def track_views(tracker, now_ms, history_n=40):
    views = []
    for track in tracker.tracks:
        if track.state_status == TrackState.TENTATIVE:
            continue
        hist = track.get_recent_detections(history_n)
        if len(hist) < 2 or now_ms - hist[-1]["timestamp"] > 8000:
            continue
        views.append(
            {
                "track_id": track.id,
                "history": [
                    {"t_s": h["timestamp"] / 1000, "delay_us": h["delay"], "doppler_hz": h["doppler"], "snr": h["snr"]}
                    for h in hist
                ],
            }
        )
    return views


def solve_candidate(candidate, configs, altitudes=(3.0, 7.0, 11.0), max_nfev=200):
    """Only radar-derived guesses, measurements and history enter the solver."""
    epochs = candidate.get("cv_epochs") or []
    if len(epochs) < 4 or epochs[-1]["t_s"] - epochs[0]["t_s"] < 12:
        return None, "insufficient_history"
    # Do not copy the candidate wholesale: even a future associator returning
    # truth metadata cannot feed it through this boundary accidentally.
    fit = {
        "initial_guess": dict(candidate["initial_guess"]),
        "initial_velocity": dict(candidate.get("initial_velocity") or {}),
        "epochs": [
            {
                "t_s": e["t_s"],
                "measurements": [
                    {k: m[k] for k in ("node_id", "delay_us", "doppler_hz", "snr") if k in m} for m in e["measurements"]
                ],
            }
            for e in epochs
        ],
        "timestamp_ms": int(epochs[-1]["t_s"] * 1000),
    }
    results = []
    for altitude in altitudes:
        fit["initial_guess"]["alt_km"] = altitude
        kwargs = {"max_nfev": max_nfev} if max_nfev != 200 else {}
        result = solver.fit_constant_velocity(fit, configs, **kwargs)
        if (
            result
            and result.get("success")
            and result.get("optimizer_success", True)
            and all(math.isfinite(result[k]) for k in ("lat", "lon", "alt_m", "chi2_per_dof"))
        ):
            results.append(result)
    if not results:
        return None, "no_convergence"
    best = min(results, key=lambda r: r["chi2_per_dof"])
    if candidate["n_nodes"] >= 3:
        # CV history confirms a pair; solve all available nodes at one epoch.
        # Delay-rate comes directly from measured Doppler, not ADS-B velocity.
        epoch_s = candidate["timestamp_ms"] / 1000
        measurements = []
        for m in candidate["measurements"]:
            fc = configs[m["node_id"]].get("fc_hz", configs[m["node_id"]].get("FC"))
            dt = epoch_s - m.get("t_s", epoch_s)
            measurements.append(
                {
                    "node_id": m["node_id"],
                    "delay_us": m["delay_us"] - m["doppler_hz"] / fc * 1e6 * dt,
                    "doppler_hz": m["doppler_hz"],
                    "snr": m.get("snr", 0),
                }
            )
        radar_input = {
            "initial_guess": {"lat": best["lat"], "lon": best["lon"], "alt_km": best["alt_m"] / 1000},
            "initial_velocity": {"vel_east_ms": best["vel_east"], "vel_north_ms": best["vel_north"]},
            "measurements": measurements,
            "timestamp_ms": candidate["timestamp_ms"],
        }
        result = solver.solve_multinode(radar_input, configs, free_altitude=True)
        if not result or not result.get("success") or not result.get("optimizer_success", True):
            return None, "no_convergence"
        result["chi2_per_dof"] = best["chi2_per_dof"]
        result["cv_n_nodes"] = len(best["contributing_node_ids"])
        result["horizontal_sigma_km"] = best.get("horizontal_sigma_km")
        return result, "converged"
    return best, "converged"


class TruthIndex:
    """Evaluation-only position history, keyed and aged at observation time."""

    def __init__(self, truth_rows):
        grouped = defaultdict(dict)
        for row in truth_rows:
            for hexn, rec in row.get("aircraft", {}).items():
                stamp = rec.get("timestamp_ms", rec.get("last_seen_ms", 0))
                if stamp:
                    grouped[hexn][stamp] = rec
        self.rows = {h: sorted(rows.items()) for h, rows in grouped.items()}
        self.times = {h: [r[0] for r in rows] for h, rows in self.rows.items()}

    def at(self, timestamp_ms, max_age_s=10):
        out = {}
        for hexn, rows in self.rows.items():
            i = bisect.bisect_left(self.times[hexn], timestamp_ms)
            candidates = rows[max(0, i - 1) : i + 1]
            if not candidates:
                continue
            stamp, rec = min(candidates, key=lambda r: abs(r[0] - timestamp_ms))
            dt = (timestamp_ms - stamp) / 1000
            if abs(dt) > max_age_s:
                continue
            if not all(
                isinstance(rec.get(k), (int, float)) and math.isfinite(rec[k])
                for k in ("lat", "lon", "alt_m", "vel_east", "vel_north")
            ):
                continue
            lat, lon = offset_latlon_m(rec["lat"], rec["lon"], rec["vel_east"] * dt, rec["vel_north"] * dt)
            out[hexn] = {**rec, "lat": lat, "lon": lon, "age_s": abs(dt)}
        return out


class DetectionLabels:
    """Evaluation-only labels joined back to the exact captured measurement.

    Constructed after replay. Identities are never attached to tracker inputs,
    histories, association candidates or solver inputs.
    """

    def __init__(self, frames, calibration=None):
        self.labels = defaultdict(set)
        coincident = defaultdict(set)
        for row in frames:
            nid, frame = row["node_id"], row["frame"]
            correction = (calibration or {}).get(nid, {})
            labels, tags = frame.get("adsb_hex") or [], frame.get("adsb") or []
            for i, (d, f) in enumerate(zip(frame.get("delay", []), frame.get("doppler", []))):
                label = labels[i] if i < len(labels) else None
                if not label and i < len(tags) and isinstance(tags[i], dict):
                    label = tags[i].get("hex") or tags[i].get("icao")
                if isinstance(label, str) and label:
                    key = self.key(
                        nid,
                        frame["timestamp"],
                        d - correction.get("delay_bias_us", 0),
                        f - correction.get("doppler_bias_hz", 0),
                    )
                    self.labels[key].add(label.lower())
                    coincident[(label.lower(), frame["timestamp"] // 5000)].add(nid)
        self.opportunities = {(label, bin5 // 12) for (label, bin5), nodes in coincident.items() if len(nodes) >= 2}

    @staticmethod
    def key(nid, timestamp_ms, delay, doppler):
        return nid, round(timestamp_ms), round(delay, 6), round(doppler, 6)

    def for_candidate(self, candidate):
        labels, labeled_nodes = set(), set()
        for m in candidate["measurements"]:
            key = self.key(
                m["node_id"], m.get("t_s", candidate["timestamp_ms"] / 1000) * 1000, m["delay_us"], m["doppler_hz"]
            )
            found = self.labels.get(key, set())
            labels.update(found)
            if found:
                labeled_nodes.add(m["node_id"])
        if len(labels) > 1:
            return None, "identity_conflict"
        if len(labels) == 1 and len(labeled_nodes) >= 2:
            return next(iter(labels)), "node_identity_consensus"
        return None, "measurement_space"


def reference_for(candidate, geometries, truth, delay_gate=3.0, doppler_gate=20.0):
    """Measurement-space identity, independent of solved position and success.

    Every contributing node must agree; the best alternative must be clearly
    worse. No match is deliberately distinct from a failed or inaccurate solve.
    """
    matches = []
    for hexn, rec in truth.items():
        scores = []
        for m in candidate["measurements"]:
            geo = geometries[m["node_id"]]
            dt = m.get("t_s", candidate["timestamp_ms"] / 1000) - candidate["timestamp_ms"] / 1000
            lat, lon = offset_latlon_m(rec["lat"], rec["lon"], rec["vel_east"] * dt, rec["vel_north"] * dt)
            d, f = predict_observation(geo, lat, lon, rec["alt_m"] / 1000, rec["vel_east"], rec["vel_north"])
            a, b = abs(d - m["delay_us"]) / delay_gate, abs(f - m["doppler_hz"]) / doppler_gate
            if a > 1 or b > 1:
                break
            scores.append(a * a + b * b)
        else:
            if scores:
                matches.append((sum(scores) / len(scores), hexn, rec))
    matches.sort(key=lambda item: item[0])
    if not matches:
        return None, "no_reference_match"
    if len(matches) > 1 and matches[1][0] < max(0.1, 2 * matches[0][0]):
        return None, "ambiguous_reference"
    return matches[0][2], matches[0][1]


def replay(
    frame_rows,
    *,
    min_snr=4.0,
    sigma_delay=0.1,
    sigma_doppler=2.0,
    assoc_interval=30.0,
    process_noise_doppler=20.0,
    max_candidates=10000,
    history_size=20,
    unknown_beam="declared",
    progress=False,
    calibration=None,
    max_nfev=200,
):
    """Pure radar stage. No truth object or provider is accepted by this API."""
    import retina_tracker

    cfg_path = Path(retina_tracker.__file__).parent / "config.yaml"
    cfg = yaml.safe_load(cfg_path.read_text())
    cfg["adsb"]["enabled"] = False
    cfg["adsb"]["priority"] = False
    cfg["process_noise"]["doppler"] = process_noise_doppler
    tracker_config.set_config(cfg)
    solver._SIGMA_DELAY_US = sigma_delay
    solver._SIGMA_DOPPLER_HZ = sigma_doppler
    associator = InterNodeAssociator(
        assoc_interval_s=0, adsb_seed_mode="off", claim_mode="off", cv_fit=None, max_pairs_per_round=64
    )
    configs, trackers, last_assoc = {}, {}, {}
    raw_configs = {}
    counts = Counter()
    records = []
    started = time.monotonic()
    for row in sorted(frame_rows, key=lambda r: (r["frame"]["timestamp"], r["node_id"])):
        nid, frame, config = row["node_id"], row["frame"], row["config"]
        if config != raw_configs.get(nid):
            raw_configs[nid] = config
            config = dict(config)
            if unknown_beam == "omni" and config.get("beam_width_deg") is None:
                config["beam_width_deg"] = 360.0
            associator.register_node(nid, config)
            configs[nid] = config
            trackers[nid] = Tracker(detection_window=history_size)
        tracker = trackers[nid]
        detections = blind_detections(frame, min_snr)
        correction = (calibration or {}).get(nid, {})
        for detection in detections:
            detection["delay"] -= correction.get("delay_bias_us", 0)
            detection["doppler"] -= correction.get("doppler_bias_hz", 0)
        counts["frames"] += 1
        counts["detections"] += len(detections)
        tracker.process_frame(detections, frame["timestamp"])
        views = track_views(tracker, frame["timestamp"], history_size)
        # Library cadence is wall-clock-based. Replay gates in capture time
        # and still updates each node's history between association rounds.
        if frame["timestamp"] - last_assoc.get(nid, 0) < assoc_interval * 1000:
            associator._pending_tracks[nid] = views
            continue
        last_assoc[nid] = frame["timestamp"]
        round_ = associator.submit_tracks_round(nid, views, frame["timestamp"])
        candidates = associator.format_track_pairs_for_solver(round_.pairs) if round_.pairs else []
        for candidate in candidates:
            counts[f"candidate_n{candidate['n_nodes']}"] += 1
            counts[f"pool_n{candidate.get('pool_n_nodes') or candidate['n_nodes']}"] += 1
            if len(records) >= max_candidates:
                counts["candidate_budget_dropped"] += 1
                continue
            result, outcome = solve_candidate(candidate, configs, max_nfev=max_nfev)
            counts[outcome] += 1
            records.append({"candidate": copy.deepcopy(candidate), "result": result, "outcome": outcome})
            if progress and len(records) % 100 == 0:
                print(
                    json.dumps({"elapsed_s": round(time.monotonic() - started), **counts}), file=sys.stderr, flush=True
                )
    return records, dict(counts), associator.node_geometries


def evaluate(records, geometries, truth, *, chi2_max=2.0, labels=None, max_horizontal_sigma=None):
    counts = Counter(attempts=len(records))
    errors, by_n = [], defaultdict(Counter)
    attempted_windows, accepted_windows, accurate_windows = set(), set(), set()
    scored = []
    for rec in records:
        candidate, result = rec["candidate"], rec["result"]
        refs = truth.at(candidate["timestamp_ms"])
        identity, label_basis = labels.for_candidate(candidate) if labels else (None, "measurement_space")
        if label_basis == "identity_conflict":
            ref, label = None, "identity_conflict"
        elif identity:
            ref, label = refs.get(identity), identity
            if ref is None:
                label = "identity_reference_stale_or_missing"
        else:
            ref, label = reference_for(candidate, geometries, refs)
        n = str(candidate["n_nodes"])
        by_n[n]["attempts"] += 1
        accepted = bool(
            result
            and result["chi2_per_dof"] <= chi2_max
            and not result.get("z_saturated", False)
            and 50 < result["alt_m"] < 20000
            and result.get("rms_delay", 0) <= 2
            and result.get("rms_doppler", 0) <= 20
            and (
                max_horizontal_sigma is None
                or (
                    result.get("horizontal_sigma_km") is not None
                    and result["horizontal_sigma_km"] <= max_horizontal_sigma
                )
            )
        )
        counts["converged"] += result is not None
        counts["accepted"] += accepted
        by_n[n]["accepted"] += accepted
        row = {
            "timestamp_ms": candidate["timestamp_ms"],
            "n_nodes": candidate["n_nodes"],
            "outcome": rec["outcome"],
            "accepted": accepted,
            "reference": label,
            "reference_basis": label_basis,
            "result": result,
            "position_error_km": None,
        }
        if ref is None:
            counts[label] += 1
            if label == "identity_conflict":
                counts["accepted_identity_conflicts"] += accepted
        else:
            counts["reference_eligible"] += 1
            counts["reference_eligible_accepted"] += accepted
            counts[f"{label_basis}_eligible"] += 1
            counts[f"{label_basis}_accepted"] += accepted
            window = (label, candidate["timestamp_ms"] // 60000)
            if labels and window in labels.opportunities:
                attempted_windows.add(window)
                if accepted:
                    accepted_windows.add(window)
            if result:
                # Score at the result epoch, which can differ from the frame's
                # association epoch by the track histories' temporal skew.
                dt = (result["timestamp_ms"] - candidate["timestamp_ms"]) / 1000
                lat, lon = offset_latlon_m(ref["lat"], ref["lon"], ref["vel_east"] * dt, ref["vel_north"] * dt)
                err = haversine_km(result["lat"], result["lon"], lat, lon)
                row["position_error_km"] = err
                row["altitude_error_m"] = abs(result["alt_m"] - ref["alt_m"])
                if accepted:
                    errors.append(err)
                    counts["accepted_within_1km"] += err <= 1
                    counts["accepted_within_5km"] += err <= 5
                    if labels and window in labels.opportunities and err <= 5:
                        accurate_windows.add(window)
        scored.append(row)
    eligible = counts["reference_eligible"]
    return {
        "counts": dict(counts),
        "by_n_nodes": dict(by_n),
        "reference_solve_rate": counts["reference_eligible_accepted"] / eligible if eligible else None,
        "accepted_error_km": {
            "n": len(errors),
            "median": float(np.median(errors)) if errors else None,
            "p95": float(np.percentile(errors, 95)) if errors else None,
        },
        "identity_window_funnel": {
            "definition": "Aircraft-minute with tagged radar detections at >=2 nodes in the same 5-second bin; captures only, not airspace recall",
            "opportunities": len(labels.opportunities) if labels else None,
            "attempted": len(attempted_windows),
            "accepted": len(accepted_windows),
            "accepted_within_5km": len(accurate_windows),
        },
        "records": scored,
    }


def load_captures(paths, start_ms=0, end_ms=2**63 - 1):
    frames, truth_rows = [], []
    for path in paths:
        with path.open() as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    if not line.endswith("\n"):
                        continue  # an active capture can have an incomplete last line
                    raise
                if row.get("kind") == "frame" and start_ms <= row["frame"]["timestamp"] <= end_ms:
                    frames.append(row)
                elif row.get("kind") == "truth":
                    truth_rows.append(row)
    return frames, truth_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--min-snr", type=float, default=4)
    parser.add_argument("--sigma-delay", type=float, default=0.1)
    parser.add_argument("--sigma-doppler", type=float, default=2)
    parser.add_argument("--assoc-interval", type=float, default=30)
    parser.add_argument("--process-noise-doppler", type=float, default=20)
    parser.add_argument("--max-candidates", type=int, default=10000)
    parser.add_argument("--max-nfev", type=int, default=200)
    parser.add_argument("--history-size", type=int, default=20)
    parser.add_argument("--unknown-beam", choices=("declared", "omni"), default="declared")
    parser.add_argument("--progress", action="store_true")
    parser.add_argument("--calibration", type=Path, help="Frozen reference_report from an EARLIER capture")
    parser.add_argument(
        "--records-output", type=Path, help="Optional private estimator records for repeatable rescoring"
    )
    parser.add_argument(
        "--max-horizontal-sigma", type=float, help="Optional local uncertainty gate in km; does not use truth"
    )
    parser.add_argument("--start-ms", type=int, default=0)
    parser.add_argument("--end-ms", type=int, default=2**63 - 1)
    args = parser.parse_args()
    if min(args.sigma_delay, args.sigma_doppler, args.assoc_interval, args.history_size, args.max_nfev) <= 0:
        parser.error("noise, association interval and history size must be positive")
    frames, truth_rows = load_captures(args.captures, args.start_ms, args.end_ms)
    calibration = None
    if args.calibration:
        trained = json.loads(args.calibration.read_text())
        if frames and min(r["frame"]["timestamp"] for r in frames) <= trained["trained_until_ms"]:
            parser.error("calibration and evaluation captures must not overlap")
        calibration = trained["suggested_calibration"]
        for correction in calibration.values():
            for key, limit in (("delay_bias_us", 5), ("doppler_bias_hz", 20)):
                value = correction.get(key, 0)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > limit:
                    parser.error("calibration bias exceeds the permitted range")
    records, counts, geometries = replay(
        frames,
        min_snr=args.min_snr,
        sigma_delay=args.sigma_delay,
        sigma_doppler=args.sigma_doppler,
        assoc_interval=args.assoc_interval,
        process_noise_doppler=args.process_noise_doppler,
        max_candidates=args.max_candidates,
        history_size=args.history_size,
        unknown_beam=args.unknown_beam,
        progress=args.progress,
        calibration=calibration,
        max_nfev=args.max_nfev,
    )
    if args.records_output:
        args.records_output.write_text(json.dumps(records, allow_nan=False))
    report = evaluate(
        records,
        geometries,
        TruthIndex(truth_rows),
        labels=DetectionLabels(frames, calibration),
        max_horizontal_sigma=args.max_horizontal_sigma,
    )
    report["replay"] = counts
    report["source_sha256"] = SOURCE_HASHES
    report["parameters"] = {k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items() if k != "captures"}
    report["evaluation_mode"] = (
        "blind_tracking_association_and_solve; exact_detection_identities_or_measurement_space_labels_after_solve"
    )
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps({k: v for k, v in report.items() if k != "records"}, indent=2))


if __name__ == "__main__":
    main()
