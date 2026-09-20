"""Opt-in, bounded PRIVATE capture for reproducible real-data validation.

Unlike the public detection archive this includes true receiver geometry and
must stay under data/runtime, never coverage_data/archive. Only allowlisted
measurement fields are written; authentication and custody material are not.
"""

import asyncio
import json
import logging
import os
import queue
import time
from pathlib import Path

from core import state
from services.tasks.executor import task_executor

_queue = queue.Queue(maxsize=5000)
_enabled = False
_counters = {"frames": 0, "dropped": 0, "bytes": 0, "errors": 0}
_budget = 512 * 1024 * 1024
_FRAME_FIELDS = ("timestamp", "delay", "doppler", "snr", "adsb_hex", "seq", "boot_id", "config_version")
_TAG_FIELDS = (
    "hex",
    "icao",
    "lat",
    "lon",
    "alt_baro",
    "alt_geom",
    "alt",
    "gs",
    "track",
    "last_seen_ms",
    "seen_pos",
    "timestamp",
    "timestamp_ms",
    "position_timestamp",
    "type",
    "reference_eligible",
    "altitude_source",
)
_TRUTH_FIELDS = (
    "lat",
    "lon",
    "alt_m",
    "vel_east",
    "vel_north",
    "timestamp_ms",
    "source",
    "precision_eligible",
    "reference_eligible",
    "type",
    "time_basis",
    "altitude_source",
    "nic",
    "nac_p",
    "rc",
    "position_timestamp",
)
_CONFIG_FIELDS = (
    "rx_lat",
    "rx_lon",
    "rx_alt_ft",
    "tx_lat",
    "tx_lon",
    "tx_alt_ft",
    "fc_hz",
    "fs_hz",
    "FC",
    "Fs",
    "beam_azimuth_deg",
    "beam_width_deg",
    "max_range_km",
    "max_bistatic_range_km",
    "doppler_min",
    "doppler_max",
)


def offer(node_id: str, frame: dict) -> None:
    if not _enabled or state.node_world(node_id) != "real" or frame.get("_signature_valid") is False:
        return
    cfg = state.node_associator.node_configs.get(node_id, {})
    clean = {k: frame[k] for k in _FRAME_FIELDS if k in frame}
    if isinstance(frame.get("adsb"), list):
        clean["adsb"] = [
            {k: tag[k] for k in _TAG_FIELDS if k in tag} if isinstance(tag, dict) else None for tag in frame["adsb"]
        ]
    # Serialization snapshots the lists before a caller can mutate them. This
    # is a small, bounded frame; disk I/O runs exclusively on the writer thread.
    try:
        row = json.dumps(
            {
                "kind": "frame",
                "received_s": time.time(),
                "node_id": node_id,
                "config": {k: cfg[k] for k in _CONFIG_FIELDS if k in cfg},
                "frame": clean,
            },
            allow_nan=False,
        )
    except (ValueError, TypeError):
        _counters["dropped"] += 1
        return
    try:
        _queue.put_nowait(row)
    except queue.Full:
        _counters["dropped"] += 1


def status() -> dict:
    return {**_counters, "enabled": _enabled, "queue_depth": _queue.qsize(), "max_bytes": _budget}


def _write_batch(path: Path, truth: dict, budget: int) -> bool:
    global _enabled
    if _counters["bytes"] >= budget:
        _enabled = False
        return False
    rows = []
    while len(rows) < 1000:
        try:
            rows.append(_queue.get_nowait())
        except queue.Empty:
            break
    if not rows and not truth:
        return False
    n_frames = len(rows)
    if truth:
        rows.append(json.dumps({"kind": "truth", "received_s": time.time(), "aircraft": truth}, allow_nan=False))
    data = ("\n".join(rows) + "\n").encode()
    if _counters["bytes"] + len(data) > budget:
        _counters["dropped"] += n_frames
        _enabled = False
        return False
    filename = path / (time.strftime("%Y%m%d-%H", time.gmtime()) + ".jsonl")
    fd = os.open(filename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "ab") as stream:
        stream.write(data)
    _counters["frames"] += n_frames
    _counters["bytes"] += len(data)
    return True


async def capture_task():
    global _enabled, _budget
    if os.getenv("REAL_DATA_CAPTURE", "0") != "1":
        return
    path = Path(__file__).resolve().parents[1] / "data" / "runtime" / "real-validation"
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    try:
        limit_mib = int(os.getenv("REAL_DATA_CAPTURE_MAX_MIB", "512"))
        if not 1 <= limit_mib <= 4096:
            raise ValueError("capture budget must be between 1 and 4096 MiB")
    except ValueError:
        _counters["errors"] += 1
        logging.exception("Invalid private capture budget")
        return
    budget = _budget = limit_mib * 1024 * 1024
    _counters["bytes"] = sum(p.stat().st_size for p in path.glob("*.jsonl"))
    _enabled = _counters["bytes"] < budget
    last_truth = {}
    try:
        async with task_executor("real-capture") as run:
            while _enabled:
                await asyncio.sleep(2)
                try:
                    snapshot = state._adsb_for_seeding("real")
                    changed = {
                        h: {k: rec[k] for k in _TRUTH_FIELDS if k in rec}
                        for h, rec in snapshot.items()
                        if rec.get("timestamp_ms") != last_truth.get(h)
                    }
                    if await run(_write_batch, path, changed, budget):
                        last_truth = {h: rec.get("timestamp_ms") for h, rec in snapshot.items()}
                except (OSError, ValueError):
                    _counters["errors"] += 1
                    _enabled = False
                    logging.exception("Private real-data capture stopped after a write error")
    finally:
        _enabled = False
