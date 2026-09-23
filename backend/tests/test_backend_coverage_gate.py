"""The coverage threshold is enforced in a different place from where it is
measured: the shards of `backend-tests` each override it away, and the separate
`backend-coverage` job applies it to the three combined. That split is invisible
in either half on its own, and a mistake in it does not fail anything — it just
reports a figure over part of the suite, or over none. These pin the seams.
"""

import re
import tomllib
from pathlib import Path

import pytest
import yaml

BACKEND = Path(__file__).resolve().parents[1]
CI = BACKEND.parent / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def jobs() -> dict:
    return yaml.safe_load(CI.read_text())["jobs"]


@pytest.fixture(scope="module")
def pytest_step(jobs) -> str:
    runs = [step["run"] for step in jobs["backend-tests"]["steps"] if step.get("run", "").startswith("pytest ")]
    assert len(runs) == 1, f"backend-tests has {len(runs)} pytest steps"
    return runs[0]


def _threshold(flag: str, text: str) -> int:
    found = re.search(rf"{re.escape(flag)}[= ](\d+)", text)
    assert found, f"no {flag} in {text!r}"
    return int(found.group(1))


def test_the_shards_do_not_apply_the_threshold_to_their_own_third(pytest_step):
    assert _threshold("--cov-fail-under", pytest_step) == 0


def _declared_threshold() -> int:
    """The number addopts names, which every other check here is measured
    against. Zero is not a lower threshold but no threshold at all: pytest-cov
    skips the check when the value is not above zero, so a 0 here and a 0 in the
    combine job would agree with each other and enforce nothing."""
    addopts = tomllib.loads((BACKEND / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["addopts"]
    declared = [_threshold("--cov-fail-under", opt) for opt in addopts if opt.startswith("--cov-fail-under=")]
    assert len(declared) == 1, f"addopts names {len(declared)} coverage thresholds"
    assert declared[0] > 0, "addopts sets --cov-fail-under=0, which enforces nothing"
    return declared[0]


def test_addopts_still_enforces_the_threshold_for_a_local_run():
    assert _declared_threshold() > 0


def test_the_combine_job_enforces_the_same_threshold_addopts_names(jobs):
    declared = _declared_threshold()
    reports = [step["run"] for step in jobs["backend-coverage"]["steps"] if "coverage report" in step.get("run", "")]
    assert len(reports) == 1, f"backend-coverage has {len(reports)} report steps"
    assert _threshold("--fail-under", reports[0]) == declared


def test_every_shard_that_runs_is_a_shard_the_combine_job_waits_for(jobs, pytest_step):
    shards = jobs["backend-tests"]["strategy"]["matrix"]["shard"]
    assert _threshold("--splits", pytest_step) == len(shards)
    counted = [step["run"] for step in jobs["backend-coverage"]["steps"] if "fragments" in step.get("run", "")]
    assert len(counted) == 1, f"backend-coverage has {len(counted)} fragment counts"
    assert f"-ne {len(shards)} " in counted[0]


def test_a_shards_data_file_survives_the_artifact_round_trip(jobs):
    """Coverage writes dotfiles and upload-artifact@v4 drops those by default,
    which empties the download rather than failing the upload."""
    uploads = [step for step in jobs["backend-tests"]["steps"] if "upload-artifact" in step.get("uses", "")]
    assert len(uploads) == 1, f"backend-tests has {len(uploads)} uploads"
    assert uploads[0]["with"]["include-hidden-files"] is True
    assert jobs["backend-tests"]["env"]["COVERAGE_FILE"].startswith(".coverage.")


def test_a_deploy_waits_on_the_threshold_and_not_only_on_the_tests(jobs):
    assert "backend-coverage" in jobs["staging"]["needs"]


def test_the_combine_job_reads_with_the_coverage_the_shards_wrote_with(jobs):
    """A data file's format belongs to the version that wrote it, and the two
    pins live in different files: the shards get theirs from uv.lock through
    pytest-cov, the combine job installs its own."""
    installs = [step["run"] for step in jobs["backend-coverage"]["steps"] if "coverage==" in step.get("run", "")]
    assert len(installs) == 1, f"backend-coverage installs coverage {len(installs)} times"
    reader = re.search(r"coverage==(\S+)", installs[0]).group(1)
    lock = tomllib.loads((BACKEND / "uv.lock").read_text())["package"]
    writer = [package["version"] for package in lock if package["name"] == "coverage"]
    assert writer == [reader], f"combine job reads with coverage {reader}, shards write with {writer}"


def test_a_tmp_path_fixture_is_omitted_from_the_report():
    """Without this the combine job dies on a path only the shard runner had.
    Checked against a real filename rather than by matching the string, because
    a glob's `*` does not cross a separator and the near misses look right."""
    from coverage.files import GlobMatcher

    omit = tomllib.loads((BACKEND / "pyproject.toml").read_text())["tool"]["coverage"]["report"]["omit"]
    fixture = "/tmp/pytest-of-runner/pytest-0/test_a_comment_beside_a_change0/app.py"
    assert GlobMatcher(omit, "omit").match(fixture), f"{omit} does not omit {fixture}"
