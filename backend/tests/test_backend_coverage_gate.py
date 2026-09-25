"""The coverage threshold is enforced in a different place from where it is
measured: the shards of `backend-tests` each override it away, and the separate
`backend-coverage` job applies it to the three combined. That split is invisible
in either half on its own, and a mistake in it does not fail anything — it just
reports a figure over part of the suite, or over none. These pin the seams.

The threshold itself is `fail_under` in pyproject's [tool.coverage.report], the
one place the combine job and `just test-ci` read it from.
"""

import os
import re
import subprocess
import sys
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


def test_coverage_run_from_backend_reads_a_threshold():
    """Found the way `coverage report` and pytest-cov find it, from backend/, so
    a key in the wrong table or a config file that takes precedence over
    pyproject.toml is read as they would read it. In a child process, because
    discovery follows the working directory and this one's is shared with
    whatever threads earlier tests left running. Zero is not a lower threshold
    but no threshold at all: both skip the check when it is not above zero."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COVERAGE_", "COV_CORE_"))}
    found = subprocess.run(
        [sys.executable, "-c", "import coverage; print(coverage.Coverage().get_option('report:fail_under'))"],
        cwd=BACKEND,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    declared = float(found.stdout)
    assert declared > 0, "coverage finds no fail_under from backend/, so nothing enforces a threshold"


def test_a_run_that_does_not_ask_for_coverage_measures_nothing():
    """Coverage in addopts would measure the whole backend on a run of one file
    and fail the threshold however its tests went."""
    addopts = tomllib.loads((BACKEND / "pyproject.toml").read_text())["tool"]["pytest"]["ini_options"]["addopts"]
    assert not [opt for opt in addopts if opt.startswith("--cov")], f"addopts asks for coverage: {addopts}"


def test_the_combine_job_reads_the_threshold_from_backend(jobs):
    """Where the test above found it, and with no --fail-under of its own to
    override it."""
    job = jobs["backend-coverage"]
    assert job["defaults"]["run"]["working-directory"] == "backend"
    assert "COVERAGE_RCFILE" not in job.get("env", {}), "the job points coverage at another config"
    reports = [step for step in job["steps"] if "coverage report" in step.get("run", "")]
    assert len(reports) == 1, f"backend-coverage has {len(reports)} report steps"
    assert "working-directory" not in reports[0], "the report step reads its config from somewhere else"
    assert "COVERAGE_RCFILE" not in reports[0].get("env", {}), "the report step reads its config from somewhere else"
    assert "rcfile" not in reports[0]["run"], "the report step reads its config from somewhere else"
    assert "fail-under" not in reports[0]["run"], "the report step overrides the declared threshold"


def test_every_shard_that_runs_is_a_shard_the_combine_job_waits_for(jobs, pytest_step):
    matrix = jobs["backend-tests"]["strategy"]["matrix"]
    # strategy.job-total counts every combination, so a second axis would
    # multiply --splits while --group still only spans the shards.
    assert set(matrix) == {"shard"}, f"backend-tests' matrix has axes {sorted(matrix)}"
    shards = matrix["shard"]
    assert shards == list(range(1, len(shards) + 1)), f"--group takes 1..N, the matrix has {shards}"
    assert re.search(r"--splits \$\{\{ strategy\.job-total \}\}", pytest_step), "--splits is not the matrix size"
    assert re.search(r"--group \$\{\{ matrix\.shard \}\}", pytest_step), "--group is not the shard"
    counted = [step["run"] for step in jobs["backend-coverage"]["steps"] if "fragments" in step.get("run", "")]
    assert len(counted) == 1, f"backend-coverage has {len(counted)} fragment counts"
    assert _threshold("-ne", counted[0]) == len(shards)


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
    fixtures = [
        # test_deploy_scope.py: a throwaway repo's top-level app.py, run serially.
        "/tmp/pytest-of-runner/pytest-0/test_a_comment_beside_a_change0/app.py",
        # test_migrations.py: a copied core/ two levels down, under an xdist worker.
        "/tmp/pytest-of-runner/pytest-0/popen-gw2/test_rollback_ahead_sentinel_m0/old_image/core/nodes.py",
    ]
    for fixture in fixtures:
        assert GlobMatcher(omit, "omit").match(fixture), f"{omit} does not omit {fixture}"
