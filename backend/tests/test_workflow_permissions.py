"""Every workflow states the token scope it runs with, and pulls no image by `:latest`.

A job with no `permissions:` gets the repository's default token, which only
an org admin can read. A workflow-wide block is the read-only baseline; a job
that needs more widens its own, naming each scope.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.y*ml"))
BASELINE = {"contents": "read"}
LATEST = re.compile(r"[\w./-]+:latest\b")


def test_there_are_workflows_to_check():
    assert len(WORKFLOWS) > 1


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_job_runs_with_a_declared_token_scope(path):
    workflow = yaml.safe_load(path.read_text())
    if "permissions" in workflow:
        assert workflow["permissions"] == BASELINE, (
            f"the workflow-wide block is {workflow['permissions']!r}; keep it {BASELINE!r} and widen per job"
        )
        return
    undeclared = [name for name, job in workflow["jobs"].items() if "permissions" not in job]
    assert not undeclared, (
        f"{undeclared} run with the repository's default token. Declare "
        "`permissions: contents: read` at the top of the workflow, and widen per job."
    )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_job_takes_every_scope(path):
    workflow = yaml.safe_load(path.read_text())
    wholesale = [name for name, job in workflow["jobs"].items() if job.get("permissions") in ("write-all", "read-all")]
    assert not wholesale, f"{wholesale} take every scope at once; name the scopes the job needs"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_image_floats_on_latest(path):
    code = [line for line in path.read_text().splitlines() if not line.lstrip().startswith("#")]
    floating = [line.strip() for line in code if LATEST.search(line)]
    assert not floating, f"{floating} pull whatever :latest names on the day; pin a digest"
