"""PRIVATE node residuals and observed coverage from captured ADS-B identities.

This is detection-conditioned evidence, not a probability-of-detection map.
Unobserved cells must not be interpreted as blind spots. No solved position is
used to select a reference or construct the observed footprint.
"""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from retina_analytics.association import InterNodeAssociator, _point_in_beam, predict_observation
from retina_analytics.constants import bearing_deg, haversine_km

from scripts.blind_replay import TruthIndex, load_captures


def distribution(values):
    if not values:
        return {"n": 0}
    values = np.asarray(values)
    median = float(np.median(values))
    return {
        "n": len(values),
        "median": median,
        "p05": float(np.percentile(values, 5)),
        "p95": float(np.percentile(values, 95)),
        "mad_sigma": float(np.median(abs(values - median)) * 1.4826),
    }


def node_report(frames, truth):
    associator = InterNodeAssociator(adsb_seed_mode="off", claim_mode="off")
    stats = defaultdict(Counter)
    samples, lags = defaultdict(list), defaultdict(list)
    ranges, altitudes = defaultdict(list), defaultdict(list)
    cells = defaultdict(lambda: defaultdict(lambda: {"samples": 0, "aircraft": set()}))
    last_config = {}
    for row in frames:
        nid, frame, config = row["node_id"], row["frame"], row["config"]
        if last_config.get(nid) != config:
            associator.register_node(nid, config)
            last_config[nid] = config
        geo = associator.node_geometries.get(nid)
        stats[nid]["frames"] += 1
        lags[nid].append(row["received_s"] - frame["timestamp"] / 1000)
        refs = truth.at(frame["timestamp"])
        for i, (delay, doppler, snr) in enumerate(
            zip(frame.get("delay", []), frame.get("doppler", []), frame.get("snr", []))
        ):
            stats[nid]["detections"] += 1
            labels, tags = frame.get("adsb_hex") or [], frame.get("adsb") or []
            label = labels[i] if i < len(labels) else None
            if not label and i < len(tags) and isinstance(tags[i], dict):
                label = tags[i].get("hex") or tags[i].get("icao")
            if not isinstance(label, str) or not label:
                continue
            stats[nid]["identity_tagged"] += 1
            label = label.lower()
            ref = refs.get(label)
            if not ref or geo is None:
                continue
            pred_d, pred_f = predict_observation(
                geo, ref["lat"], ref["lon"], ref["alt_m"] / 1000, ref["vel_east"], ref["vel_north"]
            )
            rd, rf = delay - pred_d, doppler - pred_f
            stats[nid]["fresh_reference"] += 1
            agreed = abs(rd) <= 3 and abs(rf) <= 20
            stats[nid]["physics_agreed"] += agreed
            stats[nid]["inside_declared_or_default_beam"] += _point_in_beam(ref["lat"], ref["lon"], geo)
            samples[nid].append((rd, rf, snr, label, ref.get("source", "node")))
            if agreed:
                distance = haversine_km(geo.rx_lat, geo.rx_lon, ref["lat"], ref["lon"])
                azimuth = bearing_deg(geo.rx_lat, geo.rx_lon, ref["lat"], ref["lon"])
                ranges[nid].append(distance)
                altitudes[nid].append(ref["alt_m"])
                key = (int(azimuth // 15), int(distance // 5), int(ref["alt_m"] // 1000))
                cell = cells[nid][key]
                cell["samples"] += 1
                cell["aircraft"].add(label)
    nodes, calibration = {}, {}
    for nid, count in stats.items():
        rows = samples[nid]
        delays, dopplers = [r[0] for r in rows], [r[1] for r in rows]
        aircraft = len({r[3] for r in rows})
        nodes[nid] = {
            **count,
            "unique_aircraft": aircraft,
            "delay_residual_us": distribution(delays),
            "doppler_residual_hz": distribution(dopplers),
            "delivery_lag_s": distribution(lags[nid]),
            "observed_range_km": distribution(ranges[nid]),
            "observed_altitude_m": distribution(altitudes[nid]),
            "reference_sources": dict(Counter(r[4] for r in rows)),
            "beam_declared": last_config[nid].get("beam_width_deg") is not None,
            "observed_cells": [
                {
                    "azimuth_min_deg": a * 15,
                    "range_min_km": r * 5,
                    "altitude_min_m": z * 1000,
                    "samples": cell["samples"],
                    "unique_aircraft": len(cell["aircraft"]),
                }
                for (a, r, z), cell in sorted(cells[nid].items())
            ],
        }
        if len(rows) >= 100 and aircraft >= 5:
            calibration[nid] = {
                "delay_bias_us": float(np.median(delays)),
                "doppler_bias_hz": float(np.median(dopplers)),
            }
    return {
        "nodes": nodes,
        "suggested_calibration": calibration,
        "trained_from_ms": min((r["frame"]["timestamp"] for r in frames), default=0),
        "trained_until_ms": max((r["frame"]["timestamp"] for r in frames), default=0),
        "coverage_basis": "Observed, identity-tagged radar detections agreeing with fresh ADS-B within 3 us / 20 Hz; cells 15 deg x 5 km x 1 km. No airspace-wide recall denominator.",
        "reference_limitations": "Feed timing can include upstream latency; altitude may be barometric. Coarse validation, not precision survey truth.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("captures", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    frames, truth = load_captures(args.captures)
    report = node_report(frames, TruthIndex(truth))
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False))
    print(json.dumps({"nodes": len(report["nodes"]), "calibratable_nodes": len(report["suggested_calibration"])}))


if __name__ == "__main__":
    main()
