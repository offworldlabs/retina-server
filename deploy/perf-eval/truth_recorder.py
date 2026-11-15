#!/usr/bin/env python3
"""Record ADSBHub traffic around the real nodes, apart from the radar pipeline.

The node-performance evaluation (backend/services/performance_truth.py) needs a
denominator of aircraft that were there whether or not a radar saw them. The
server's own ADS-B archive cannot give one where its ADSBHub poller is off (the
test environment): its rows are the nodes' own tags, and a tag exists only
because a detection was matched. This recorder is that independent source. It
polls adsb-service (ADSBHub traffic at adsb.retina.fm) over a disc around each
cluster of real receivers and writes every answer, raw, to hourly gzip JSONL
files. Nothing in it reads a detection, a tag or the server's state, so an
aircraft's presence in these files cannot depend on the radar.

Standard library only, so it runs on the host under systemd without the
server's virtualenv or image.

Layout under RETINA_PERF_ROOT (default /var/lib/retina-perf):

    truth/adsbhub/YYYY-MM-DD/HH.jsonl.gz   rows of two kinds, see below
    truth/regions/<utc stamp>.json         the regions polled from that moment
    truth/STATUS.json                      last cycle's counters, for monitoring
    registry/users.db                      a consistent copy of the node registry

Row kinds (one JSON object per line, keys kept short):

    {"k":"p", "g":region, "n":now_ms, "r":recv_ms, "st":http status, "na":aircraft
     in answer, "nw":rows written, "d":duration_ms, "err":text when failed}
    {"k":"a", "g":region, "n":now_ms, "r":recv_ms, "c":captured_ms, "h":hex,
     "f":flight, "la":lat, "lo":lon, "ab":alt_baro, "gs":ground speed kt,
     "tr":track deg, "vr":baro_rate ft/min, "sp":seen_pos s, "se":seen s,
     "nic", "rc", "rs":rssi, "m":messages, "ml":1 if MLAT, "tb":1 if TIS-B,
     "ty":message type, and "cat", "sq", "ag" (alt_geom) when the answer has them}

``now`` is the answer's own build time, ``seen_pos`` the position's age at that
time, so ``c`` = now - seen_pos is the capture time of the position, bounded by
our receipt time as backend/clients/adsb_service.py bounds it. A row is written
once per capture: an answer repeating a fix already written for that region and
hex is not written again, so the file holds one row per distinct position.

Regions are built from the registry: every node whose id is not synthetic, whose
receiver is placed and that the server has seen within NODE_RECENT_DAYS. Nodes
within CLUSTER_KM of each other share a region; its radius covers the furthest
receiver range a detection inside the processing window can have, W/2 + the
transmitter baseline, plus margin. A region's centre is a mean of TRUE receiver
positions, so everything under truth/ is private derived data: never publish it,
never copy it into the repository or a public archive.

The service allows 2 requests a second per address. Requests are spaced
REQUEST_SPACING_S apart and a 429 is honoured for its Retry-After, capped. The
synthetic fleet on the same host polls the same service, so the cadence here
stays modest: POLL_INTERVAL_S between cycles, one request per region per cycle.
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import math
import os
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta

log = logging.getLogger("truth_recorder")

USER_AGENT = "retina-perf-truth-recorder/0.1"
KM_PER_NM = 1.852
C_KM_PER_US = 0.299792458
DEFAULT_WINDOW_KM = 59.96  # the registry's usual max_range_km
DEFAULT_BASELINE_KM = 60.0  # when the transmitter is unplaced
MAX_BASELINE_KM = 150.0
# A ``now`` further than this from receipt is garbage; receipt stands in for it.
MAX_NOW_SKEW_S = 300.0
AIRCRAFT_FIELDS = (
    ("flight", "f"),
    ("alt_baro", "ab"),
    ("gs", "gs"),
    ("track", "tr"),
    ("baro_rate", "vr"),
    ("seen_pos", "sp"),
    ("seen", "se"),
    ("nic", "nic"),
    ("rc", "rc"),
    ("rssi", "rs"),
    ("messages", "m"),
    ("type", "ty"),
    ("category", "cat"),
    ("squawk", "sq"),
    ("alt_geom", "ag"),
)


def env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180.0
    a = (
        0.5
        - math.cos((lat2 - lat1) * p) / 2
        + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2
    )
    return 12742.0 * math.asin(math.sqrt(max(0.0, min(1.0, a))))


def is_placed(lat, lon) -> bool:
    return (
        isinstance(lat, int | float)
        and isinstance(lon, int | float)
        and math.isfinite(lat)
        and math.isfinite(lon)
        and abs(lat) <= 90.0
        and abs(lon) <= 180.0
        and not (abs(lat) < 1e-6 and abs(lon) < 1e-6)
    )


# ── Registry ─────────────────────────────────────────────────────────────────


def refresh_registry_copy(source: str, copy: str) -> bool:
    """Copy the live registry with sqlite's backup API and leave the copy out of WAL mode.

    The copy is what this recorder and the evaluation read: the live file is in
    WAL mode and a read-only open of it on another mount fails.
    """
    tmp = copy + ".tmp"
    try:
        src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10.0)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
            dst = sqlite3.connect(tmp)
            try:
                src.backup(dst)
                dst.execute("pragma journal_mode=delete")
                dst.commit()
            finally:
                dst.close()
        finally:
            src.close()
        os.replace(tmp, copy)
        return True
    except (sqlite3.Error, OSError) as exc:
        log.warning("registry copy failed: %s", exc)
        return False


def registry_nodes(copy: str, recent_days: float, now: datetime) -> list[dict]:
    """Real, placed nodes seen recently, with their current config row."""
    con = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT c.node_id, c.rx_lat, c.rx_lon, c.tx_lat, c.tx_lon, c.max_range_km, n.last_seen_at, n.status "
            "FROM node_configs c JOIN nodes n ON n.node_id = c.node_id WHERE c.superseded_at IS NULL"
        ).fetchall()
    finally:
        con.close()
    cutoff = now - timedelta(days=recent_days)
    nodes = []
    for row in rows:
        node_id = row["node_id"]
        if node_id.startswith("synth") or row["status"] == "retired":
            continue
        if not is_placed(row["rx_lat"], row["rx_lon"]):
            continue
        seen = row["last_seen_at"]
        if seen:
            try:
                seen_at = datetime.fromisoformat(str(seen)).replace(tzinfo=UTC)
            except ValueError:
                seen_at = None
            if seen_at is not None and seen_at < cutoff:
                continue
        nodes.append(dict(row))
    return nodes


def reach_km(node: dict) -> float:
    """The furthest a target inside the processing window can be from the receiver.

    delay < W means Rt + Rr - L < W, and Rt >= Rr - L, so Rr < L + W/2.
    """
    window_km = node.get("max_range_km") or DEFAULT_WINDOW_KM
    if is_placed(node.get("tx_lat"), node.get("tx_lon")):
        baseline = haversine_km(node["rx_lat"], node["rx_lon"], node["tx_lat"], node["tx_lon"])
        baseline = min(baseline, MAX_BASELINE_KM)
    else:
        baseline = DEFAULT_BASELINE_KM
    return float(window_km) / 2.0 + baseline


def build_regions(nodes: list[dict], cluster_km: float, margin_km: float, min_nm: int, max_nm: int) -> list[dict]:
    clusters: list[list[dict]] = []
    for node in sorted(nodes, key=lambda n: n["node_id"]):
        for members in clusters:
            lat = sum(m["rx_lat"] for m in members) / len(members)
            lon = sum(m["rx_lon"] for m in members) / len(members)
            if haversine_km(lat, lon, node["rx_lat"], node["rx_lon"]) <= cluster_km:
                members.append(node)
                break
        else:
            clusters.append([node])
    regions = []
    for members in clusters:
        lat = sum(m["rx_lat"] for m in members) / len(members)
        lon = sum(m["rx_lon"] for m in members) / len(members)
        radius_km = max(haversine_km(lat, lon, m["rx_lat"], m["rx_lon"]) + reach_km(m) for m in members) + margin_km
        radius_nm = int(min(max_nm, max(min_nm, math.ceil(radius_km / KM_PER_NM))))
        regions.append(
            {
                "name": "+".join(sorted(m["node_id"] for m in members)),
                "lat": round(lat, 5),
                "lon": round(lon, 5),
                "radius_nm": radius_nm,
                "members": sorted(m["node_id"] for m in members),
            }
        )
    return regions


# ── Polling ──────────────────────────────────────────────────────────────────


class RateLimited(Exception):
    def __init__(self, retry_after_s: float):
        super().__init__(f"429, retry after {retry_after_s:.0f}s")
        self.retry_after_s = retry_after_s


def fetch_point(base_url: str, region: dict, timeout_s: float) -> tuple[dict, float, float]:
    """One point answer, with the receipt time and request duration."""
    url = f"{base_url}/v2/point/{region['lat']}/{region['lon']}/{region['radius_nm']}"
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310 - fixed https host
            body = resp.read()
            recv_s = time.time()
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            try:
                retry = float(exc.headers.get("Retry-After", "") or 5.0)
            except ValueError:
                retry = 5.0
            raise RateLimited(retry if math.isfinite(retry) and retry >= 0 else 5.0) from exc
        raise
    return json.loads(body), recv_s, time.monotonic() - started


def num(v) -> bool:
    return isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v)


def aircraft_rows(body: dict, region: dict, recv_s: float, last: dict) -> tuple[list[dict], int]:
    """The answer's aircraft as rows, less the fixes already written for this region."""
    recv_ms = int(recv_s * 1000.0)
    now_ms = body.get("now")
    base_ms = recv_ms
    if num(now_ms) and abs(recv_ms - now_ms) <= MAX_NOW_SKEW_S * 1000.0:
        base_ms = int(min(now_ms, recv_ms))
    rows = []
    ac = body.get("ac") or []
    for a in ac:
        if not isinstance(a, dict):
            continue
        hexn = str(a.get("hex") or "").strip().lower()
        lat, lon, seen_pos = a.get("lat"), a.get("lon"), a.get("seen_pos")
        if not hexn or not num(lat) or not num(lon) or not num(seen_pos) or not is_placed(lat, lon):
            continue
        captured_ms = int(base_ms - seen_pos * 1000.0)
        key = (region["name"], hexn)
        signature = (captured_ms // 100, round(lat, 5), round(lon, 5))
        if last.get(key) == signature:
            continue
        last[key] = signature
        row = {
            "k": "a",
            "g": region["name"],
            "n": now_ms if num(now_ms) else None,
            "r": recv_ms,
            "c": captured_ms,
            "h": hexn,
            "la": lat,
            "lo": lon,
        }
        for src, dst in AIRCRAFT_FIELDS:
            v = a.get(src)
            if v is None or v == "" or isinstance(v, list | dict):
                continue
            row[dst] = v.strip() if isinstance(v, str) else v
        if a.get("mlat"):
            row["ml"] = 1
        if a.get("tisb"):
            row["tb"] = 1
        rows.append(row)
    return rows, len(ac)


class HourlyWriter:
    """Buffered gzip JSONL, one file per UTC hour of receipt, appended as gzip members."""

    def __init__(self, root: str, flush_s: float, flush_lines: int):
        self.root = root
        self.flush_s = flush_s
        self.flush_lines = flush_lines
        self.buffer: dict[str, list[str]] = {}
        self.pending = 0
        self.last_flush = time.monotonic()
        self.written = 0

    def path_for(self, recv_ms: int) -> str:
        moment = datetime.fromtimestamp(recv_ms / 1000.0, tz=UTC)
        return os.path.join(self.root, moment.strftime("%Y-%m-%d"), moment.strftime("%H") + ".jsonl.gz")

    def add(self, row: dict) -> None:
        self.buffer.setdefault(self.path_for(row["r"]), []).append(json.dumps(row, separators=(",", ":")))
        self.pending += 1
        if self.pending >= self.flush_lines or time.monotonic() - self.last_flush >= self.flush_s:
            self.flush()

    def flush(self) -> None:
        for path, lines in self.buffer.items():
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with gzip.open(path, "ab", compresslevel=6) as fh:
                fh.write(("\n".join(lines) + "\n").encode())
            self.written += len(lines)
        self.buffer.clear()
        self.pending = 0
        self.last_flush = time.monotonic()


def write_json_atomic(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


class Recorder:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.truth_root = os.path.join(args.root, "truth")
        self.writer = HourlyWriter(os.path.join(self.truth_root, "adsbhub"), args.flush_s, 4000)
        self.registry_copy = os.path.join(args.root, "registry", "users.db")
        self.regions: list[dict] = []
        self.regions_key = None
        self.regions_refreshed = 0.0
        self.last: dict[tuple, tuple] = {}
        self.stop = False
        self.counters = {"cycles": 0, "requests": 0, "ok": 0, "failed": 0, "rate_limited": 0, "rows": 0}
        self.last_ok_by_region: dict[str, int] = {}

    def refresh_regions(self) -> None:
        now = datetime.now(UTC)
        if self.args.registry_source:
            refresh_registry_copy(self.args.registry_source, self.registry_copy)
        try:
            nodes = registry_nodes(self.registry_copy, self.args.node_recent_days, now)
        except sqlite3.Error as exc:
            log.warning("registry unreadable (%s); keeping %d region(s)", exc, len(self.regions))
            self.regions_refreshed = time.monotonic()
            return
        regions = build_regions(nodes, self.args.cluster_km, self.args.margin_km, self.args.min_nm, self.args.max_nm)
        key = json.dumps(regions, sort_keys=True)
        if key != self.regions_key:
            self.regions, self.regions_key = regions, key
            stamp = now.strftime("%Y-%m-%dT%H%M%SZ")
            write_json_atomic(
                os.path.join(self.truth_root, "regions", f"{stamp}.json"),
                {"since_ms": int(now.timestamp() * 1000), "regions": regions},
            )
            log.info(
                "regions: %s",
                "; ".join(f"{r['name']} {r['radius_nm']} nm ({len(r['members'])} node)" for r in regions) or "none",
            )
        self.regions_refreshed = time.monotonic()

    def poll(self, region: dict) -> float:
        """Poll one region, write its rows; the extra wait a 429 asked for, or 0."""
        self.counters["requests"] += 1
        try:
            body, recv_s, took = fetch_point(self.args.base_url, region, self.args.timeout_s)
        except RateLimited as exc:
            self.counters["rate_limited"] += 1
            self.writer.add({"k": "p", "g": region["name"], "r": int(time.time() * 1000), "st": 429, "err": str(exc)})
            return min(exc.retry_after_s, self.args.max_retry_after_s)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError, ValueError) as exc:
            self.counters["failed"] += 1
            status = getattr(exc, "code", 0) or 0
            self.writer.add(
                {"k": "p", "g": region["name"], "r": int(time.time() * 1000), "st": int(status), "err": str(exc)[:200]}
            )
            log.warning("%s: %s", region["name"], exc)
            return 0.0
        rows, n_ac = aircraft_rows(body, region, recv_s, self.last)
        recv_ms = int(recv_s * 1000.0)
        now_ms = body.get("now")
        self.writer.add(
            {
                "k": "p",
                "g": region["name"],
                "n": now_ms if num(now_ms) else None,
                "r": recv_ms,
                "st": 200,
                "na": n_ac,
                "nw": len(rows),
                "d": int(took * 1000.0),
                "lat": region["lat"],
                "lon": region["lon"],
                "nm": region["radius_nm"],
            }
        )
        for row in rows:
            self.writer.add(row)
        self.counters["ok"] += 1
        self.counters["rows"] += len(rows)
        self.last_ok_by_region[region["name"]] = recv_ms
        return 0.0

    def write_status(self) -> None:
        write_json_atomic(
            os.path.join(self.truth_root, "STATUS.json"),
            {
                "updated_ms": int(time.time() * 1000),
                "counters": self.counters,
                "regions": [r["name"] for r in self.regions],
                "last_ok_ms": self.last_ok_by_region,
                "rows_written": self.writer.written,
            },
        )

    def run(self) -> int:
        def _stop(signum, frame):
            self.stop = True

        signal.signal(signal.SIGTERM, _stop)
        signal.signal(signal.SIGINT, _stop)
        self.refresh_regions()
        while not self.stop:
            started = time.monotonic()
            if time.monotonic() - self.regions_refreshed >= self.args.region_refresh_s:
                self.refresh_regions()
            # Forget fixes of aircraft not seen for a while, so the dedup map stays bounded.
            if len(self.last) > 50_000:
                self.last.clear()
            extra_wait = 0.0
            for i, region in enumerate(self.regions):
                if self.stop:
                    break
                if i:
                    time.sleep(self.args.spacing_s)
                extra_wait = self.poll(region)
                if extra_wait:
                    log.warning("rate limited; waiting %.0fs before the next cycle", extra_wait)
                    break
            self.counters["cycles"] += 1
            self.writer.flush()
            self.write_status()
            if self.args.once:
                break
            elapsed = time.monotonic() - started
            time.sleep(max(0.0, self.args.interval_s - elapsed) + extra_wait)
        self.writer.flush()
        self.write_status()
        log.info("stopped: %s", self.counters)
        return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--root", default=os.environ.get("RETINA_PERF_ROOT", "/var/lib/retina-perf"))
    p.add_argument(
        "--registry-source",
        default=os.environ.get("RETINA_REGISTRY_SOURCE", ""),
        help="the live users.db to copy before each region refresh; empty reads the existing copy under --root",
    )
    p.add_argument("--base-url", default=os.environ.get("ADSB_SERVICE_URL", "https://adsb.retina.fm"))
    p.add_argument("--interval-s", type=float, default=env_float("POLL_INTERVAL_S", 10.0))
    p.add_argument("--spacing-s", type=float, default=env_float("REQUEST_SPACING_S", 0.6))
    p.add_argument("--timeout-s", type=float, default=env_float("REQUEST_TIMEOUT_S", 8.0))
    p.add_argument("--max-retry-after-s", type=float, default=env_float("MAX_RETRY_AFTER_S", 60.0))
    p.add_argument("--region-refresh-s", type=float, default=env_float("REGION_REFRESH_S", 300.0))
    p.add_argument("--node-recent-days", type=float, default=env_float("NODE_RECENT_DAYS", 7.0))
    p.add_argument("--cluster-km", type=float, default=env_float("CLUSTER_KM", 60.0))
    p.add_argument("--margin-km", type=float, default=env_float("MARGIN_KM", 10.0))
    p.add_argument("--min-nm", type=int, default=int(env_float("MIN_REGION_NM", 30)))
    p.add_argument("--max-nm", type=int, default=int(env_float("MAX_REGION_NM", 120)))
    p.add_argument("--flush-s", type=float, default=env_float("FLUSH_S", 30.0))
    p.add_argument("--once", action="store_true", help="one cycle, then exit (smoke test)")
    p.add_argument("--regions-only", action="store_true", help="print the regions and exit")
    return p


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stderr)
    args = build_parser().parse_args(argv)
    recorder = Recorder(args)
    if args.regions_only:
        recorder.refresh_regions()
        print(json.dumps(recorder.regions, indent=1))
        return 0
    return recorder.run()


if __name__ == "__main__":
    sys.exit(main())
