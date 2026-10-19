"""Real windowed custody chains for the backend tests, built the library's way.

    node = ChainNode(tmp_path, "test-cust-1")
    node.register_body                  # body for POST /api/custody/register
    entry, batch = node.next_window()   # one closed, signed window
    entry, batch = node.next_window(skip=1)   # after a restart one window late: a gap
    entry = node.sign(batch, n_detections=1)  # an entry over any batch string

`entry` is `HashChainEntry.to_dict()` and `batch` the canonical frame batch as
the str a node sends. Each node has its own key and chain directory under
`tmp_path`, so two nodes in one test do not share a key. The clock is fake and
epoch-aligned, so windows are deterministic; `timestamp_utc` is wall time, as
the library writes it.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path

from retina_custody.canonical import canonicalize
from retina_custody.crypto_backend import SoftwareCryptoBackend
from retina_custody.hash_chain import HashChainBuilder

WINDOW_S = 60
T0 = 1_800_000_000  # a multiple of WINDOW_S
NODE_CONFIG = {"rx_lat": 1.0, "rx_lon": 2.0, "tx_lat": 1.5, "tx_lon": 2.5, "fc_hz": 195_000_000}


class FakeClock:
    def __init__(self, t: float = T0 + 10):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


def frame(i: int, t_s: float = T0) -> dict:
    """One detection frame, shaped like the data of a TCP DETECTION message."""
    return {
        "timestamp": int(t_s * 1000) + i,
        "delay": [10.0 + i, 22.5],
        "doppler": [-35.0, 12.0 + i],
        "snr": [14.2, 9.8],
    }


class ChainNode:
    """One node's key and windowed chain."""

    def __init__(self, tmp_path: Path, node_id: str):
        self.node_id = node_id
        self._root = tmp_path / node_id
        self._root.mkdir(parents=True)
        self.crypto = SoftwareCryptoBackend(key_file=str(self._root / "key.json"))
        self.clock = FakeClock()
        self.builder = self._start()
        self._frames_sent = 0

    def _start(self) -> HashChainBuilder:
        # A new builder over the same directory resumes the chain from its log.
        return HashChainBuilder(
            self.node_id,
            self.crypto,
            NODE_CONFIG,
            chain_dir=str(self._root / "chain"),
            window_s=WINDOW_S,
            clock=self.clock,
        )

    @property
    def register_body(self) -> dict:
        return {
            "node_id": self.node_id,
            "public_key_pem": self.crypto.get_public_key_pem(),
            "fingerprint": self.crypto.get_public_key_fingerprint(),
            "serial_number": self.crypto.get_serial_number(),
            "signing_mode": self.crypto.signing_mode,
        }

    def next_window(self, n_frames: int = 3, skip: int = 0) -> tuple[dict, str]:
        """Close the next window over `n_frames` frames.

        With `skip`, the node is down for that many windows first and then
        restarts, the library's legitimate gap: the chain resumes from its log,
        and the next window starts later than the last one ended.
        """
        if skip:
            self.clock.advance(skip * WINDOW_S)
            self.builder = self._start()
        self.builder.close_if_due()  # opens a window if none is open
        for _ in range(n_frames):
            self.builder.add_detection(frame(self._frames_sent, self.clock()))
            self._frames_sent += 1
        self.clock.advance(WINDOW_S)
        closed = self.builder.close_if_due()
        assert closed is not None
        entry, batch = closed
        return entry.to_dict(), batch.decode("ascii")

    def sign(self, batch: str, *, n_detections: int, prev_hash: str = "genesis") -> dict:
        """A correctly hashed and signed entry committing to `batch`, whatever
        it holds, for the first window after the clock's current one."""
        start = (int(self.clock()) // WINDOW_S + 1) * WINDOW_S
        data = {
            "node_id": self.node_id,
            "window_start": _iso(start),
            "window_end": _iso(start + WINDOW_S),
            "prev_hash": prev_hash,
            "detections_hash": hashlib.sha256(batch.encode("utf-8")).hexdigest(),
            "n_detections": n_detections,
            "node_config_hash": hashlib.sha256(canonicalize(NODE_CONFIG)).hexdigest(),
            "firmware_version": "1.0.0-synthetic",
            "timestamp_utc": _iso(start + WINDOW_S),
        }
        preimage = canonicalize(data)
        return data | {
            "entry_hash": hashlib.sha256(preimage).hexdigest(),
            "signature": self.crypto.sign_hex(preimage),
            "signing_mode": self.crypto.signing_mode,
        }


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
