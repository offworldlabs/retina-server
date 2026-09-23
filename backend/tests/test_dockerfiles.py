"""Invariants the two images must share."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _uv_version(dockerfile: str) -> list[str]:
    return re.findall(r"^ARG UV_VERSION=(\S+)$", (ROOT / dockerfile).read_text(), re.MULTILINE)


def test_both_images_sync_with_the_same_uv():
    """Both read backend/uv.lock, so an older uv in one would fail on a lock the other accepts."""
    server, fleet = _uv_version("Dockerfile"), _uv_version("Dockerfile.fleet")
    assert len(server) == 1 and server == fleet, f"Dockerfile pins uv {server}, Dockerfile.fleet {fleet}"
