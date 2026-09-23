"""`web-build` runs each workspace's npm scripts from a matrix, and which entry
runs which script is decided by step conditions. A mistake there fails nothing:
a script that runs in no entry goes unchecked, and one that runs in two quietly
lengthens the run. These pin the assignment.
"""

import json
import re
from collections import Counter
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
WORKSPACES = json.loads((ROOT / "package.json").read_text())["workspaces"]


@pytest.fixture(scope="module")
def job() -> dict:
    return yaml.safe_load(CI.read_text())["jobs"]["web-build"]


def _entries(job) -> list[dict]:
    matrix = job["strategy"]["matrix"]
    assert set(matrix) == {"include"}, "these checks read the matrix as a list of include entries"
    return matrix["include"]


def _runs(step: dict, entry: dict) -> bool:
    condition = step.get("if")
    if condition is None:
        return True
    found = re.fullmatch(r"matrix\.part (==|!=) '(\w+)'", condition)
    assert found, f"no reading for the step condition {condition!r}"
    op, value = found.groups()
    return (entry.get("part") == value) == (op == "==")


def _scripts_run(job, entry) -> list[str]:
    scripts = []
    for step in job["steps"]:
        # `npm test` is npm's own shorthand for `npm run test`.
        found = re.match(r"npm (?:run )?(\w+) -w \$\{\{ matrix\.workspace \}\}", step.get("run", ""))
        if found and _runs(step, entry):
            scripts.append(found.group(1))
    return scripts


def test_every_workspace_has_an_entry(job):
    assert sorted({entry["workspace"] for entry in _entries(job)}) == sorted(WORKSPACES)


@pytest.mark.parametrize("workspace", WORKSPACES)
def test_each_script_a_workspace_defines_runs_in_exactly_one_entry(job, workspace):
    defined = json.loads((ROOT / workspace / "package.json").read_text())["scripts"]
    ran = Counter(
        script for entry in _entries(job) if entry["workspace"] == workspace for script in _scripts_run(job, entry)
    )
    for script in ("lint", "typecheck", "test", "build"):
        if script in defined:
            assert ran[script] == 1, f"{workspace}'s {script} runs in {ran[script]} entries"


def test_the_dashboard_is_split_into_its_tests_and_its_checks(job):
    parts = sorted(e.get("part") for e in _entries(job) if e["workspace"] == "dashboard")
    assert parts == ["checks", "tests"]


def test_each_half_of_a_split_runs_only_its_own_scripts(job):
    """Counting alone misses the two halves trading places, which keeps every
    script running once while the slow tests land back on the checks entry."""
    for entry in _entries(job):
        scripts = _scripts_run(job, entry)
        if entry.get("part") == "tests":
            assert scripts == ["test"], f"{entry['workspace']} {entry['part']} runs {scripts}"
        elif entry.get("part") == "checks":
            assert scripts and "test" not in scripts, f"{entry['workspace']} {entry['part']} runs {scripts}"


def test_a_split_entry_is_named_for_its_part(job):
    """The label is the job's name in the checks list, so it must say which half ran."""
    for entry in _entries(job):
        if "part" in entry:
            assert entry.get("label") == f"{entry['workspace']} {entry['part']}"
