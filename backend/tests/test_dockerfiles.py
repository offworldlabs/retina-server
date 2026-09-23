"""Invariants the two images must share, and share with CI."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SETUP_UV = ROOT / ".github" / "actions" / "setup-uv" / "action.yml"


def _uv_version(dockerfile: str) -> list[str]:
    return re.findall(r"^ARG UV_VERSION=(\S+)$", (ROOT / dockerfile).read_text(), re.MULTILINE)


def test_both_images_sync_with_the_same_uv():
    """Both read backend/uv.lock, so an older uv in one would fail on a lock the other accepts."""
    server, fleet = _uv_version("Dockerfile"), _uv_version("Dockerfile.fleet")
    assert len(server) == 1 and server == fleet, f"Dockerfile pins uv {server}, Dockerfile.fleet {fleet}"


def test_ci_installs_uv_only_through_the_pinned_action():
    """setup-uv with no version takes the latest release, so the pin binds CI only through the action."""
    direct = [
        f"{path.name}:{name}"
        for path in sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
        for name, job in yaml.safe_load(path.read_text())["jobs"].items()
        for step in job.get("steps", [])
        if step.get("uses", "").startswith("astral-sh/setup-uv")
    ]
    assert not direct, f"{direct} install uv directly; use ./.github/actions/setup-uv"


def _read_version(workspace: Path, dockerfile: str) -> tuple[int, str]:
    """Run the action's version step as GitHub runs a bash step, against a stand-in Dockerfile."""
    step = next(s for s in yaml.safe_load(SETUP_UV.read_text())["runs"]["steps"] if s.get("id") == "uv")
    (workspace / "Dockerfile").write_text(dockerfile)
    output = workspace / "output"
    output.touch()
    env = {**os.environ, "GITHUB_WORKSPACE": str(workspace), "GITHUB_OUTPUT": str(output)}
    result = subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]], env=env)
    return result.returncode, output.read_text()


def test_the_action_reads_the_dockerfile_version(tmp_path):
    assert _read_version(tmp_path, "FROM x\nARG UV_VERSION=0.12.5\n") == (0, "version=0.12.5\n")


@pytest.mark.parametrize(
    "dockerfile",
    [
        pytest.param("FROM x\n", id="missing"),
        pytest.param("ARG UV_VERSION=\n", id="empty"),
        pytest.param("ARG UV_VERSION=latest\n", id="not-a-version"),
        pytest.param("ARG UV_VERSION=0.12.5\nARG UV_VERSION=0.12.5\n", id="twice"),
    ],
)
def test_an_unreadable_version_fails_the_step(tmp_path, dockerfile):
    """An empty version would leave setup-uv on its latest."""
    status, output = _read_version(tmp_path, dockerfile)
    assert status != 0 and output == ""
