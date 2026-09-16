"""A workflow edit must be linted by something.

The deploy, staging and smoke jobs only run on push to main, so a green pull
request on a workflow change proves close to nothing, and a merge to main
deploys straight to production. actionlint is the only thing standing between a
malformed workflow and that path, and it reaches CI solely because the Lint
(pre-commit) step runs this config — so deleting one line here silently retires
the check. Asserted rather than left to a comment, as with the shared smoke
tally in test_smoke_tally_shared.py.

test_deploy_workflows.py is not a substitute: it asserts deploy *semantics* on
files it can already parse, so a workflow that is invalid but well-formed YAML
passes it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_CONFIG = _REPO / ".pre-commit-config.yaml"
_WORKFLOWS = _REPO / ".github" / "workflows"


@pytest.fixture(scope="module")
def hooks() -> list[dict]:
    config = yaml.safe_load(_CONFIG.read_text())
    return [hook for repo in config["repos"] for hook in repo["hooks"]]


def test_actionlint_is_configured(hooks) -> None:
    ids = [hook["id"] for hook in hooks]
    assert "actionlint" in ids, (
        f"no actionlint hook in .pre-commit-config.yaml (found {ids}). Nothing "
        "else validates a workflow edit, and CI picks this up only from here."
    )


def test_the_actionlint_repo_is_pinned(hooks) -> None:
    """An unpinned rev makes the gate's behaviour depend on the day it ran."""
    config = yaml.safe_load(_CONFIG.read_text())
    for repo in config["repos"]:
        if any(hook["id"] == "actionlint" for hook in repo["hooks"]):
            assert repo["rev"].startswith("v"), repo["rev"]
            return
    pytest.fail("actionlint hook found but its repo has no rev")


def test_there_are_workflows_for_it_to_check() -> None:
    """A rename of the directory would leave the hook passing over nothing."""
    found = sorted(p.name for p in _WORKFLOWS.glob("*.yml"))
    assert found, f"no workflows under {_WORKFLOWS}"
