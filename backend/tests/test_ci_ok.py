"""`ci-ok` in ci.yml is the one check branch protection is meant to require, so
it is only as good as its `needs`, its conditions and its name. A gate left out
of `needs` is a gate nothing enforces, a condition that lets it skip passes it,
since a skipped check counts as passed, and a run that reports as `ci-ok`
without running the gates replaces the verdict protection reads. These pin all
three by evaluating the workflow's own expressions for each event that starts
it.
"""

import re
from pathlib import Path

import pytest
import yaml

CI = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"

# A condition with no status function is implicitly ANDed with success(), and
# one of the others lets a job run although a need was skipped.
STATUS_FUNCTION = re.compile(r"\b(?:always|success|failure|cancelled)\(\)")
RUNS_PAST_A_SKIPPED_NEED = re.compile(r"\b(?:always|failure|cancelled)\(\)")

PUSH = {"name": "push", "ref": "refs/heads/main", "action": None, "retarget": False}
DISPATCH = {"name": "workflow_dispatch", "ref": "refs/heads/topic", "action": None, "retarget": False}
PR = {"name": "pull_request", "ref": "refs/pull/1/merge", "retarget": False}
EVENTS = {
    "push": PUSH,
    "dispatch": DISPATCH,
    "opened": {**PR, "action": "opened"},
    "synchronize": {**PR, "action": "synchronize"},
    "reopened": {**PR, "action": "reopened"},
    "title-or-body-edit": {**PR, "action": "edited"},
    "retarget": {**PR, "action": "edited", "retarget": True},
}
TITLE_EDIT = EVENTS["title-or-body-edit"]
# The runs whose verdict protection should read: a pull request's code or base moved.
DECIDING = {e for e, v in EVENTS.items() if v["name"] == "pull_request" and v is not TITLE_EDIT}


def _value(expression: str, event: dict, cancelled: bool = False):
    """A workflow expression for one event, in a run where nothing failed.
    Those in ci.yml use only equality on lower-case literals, `!` on a single
    operand, the boolean operators and format(), which read the same in Python
    for these values; a need's result or output is taken as unset."""
    original = expression
    # Python's `not` binds more loosely than `==`, where GitHub's `!` does not.
    unbracketed = re.search(r"!\s*[\w.]+(?:\([^()]*\))?\s*[=!]=", expression)
    assert not unbracketed, f"no reading for `!` in {expression!r}"
    expression = " ".join(expression.split()).removeprefix("${{").removesuffix("}}")
    for field, value in (
        ("github.event.changes.base", {"ref": {"from": "main"}} if event["retarget"] else None),
        ("github.event.action", event["action"]),
        ("github.event_name", event["name"]),
        ("github.workflow", "CI"),
        ("github.run_id", 12345),
        ("github.ref", event["ref"]),
        ("always()", True),
        ("success()", not cancelled),
        ("failure()", False),
        ("cancelled()", cancelled),
        ("null", None),
    ):
        expression = expression.replace(field, repr(value))
    expression = re.sub(r"needs\.[\w-]+\.[\w.-]+", "None", expression)
    expression = re.sub(r"!(?!=)", " not ", expression).replace("&&", " and ").replace("||", " or ")
    try:
        return eval(expression, {"__builtins__": {}, "format": lambda text, *args: text.format(*args)})
    except (NameError, SyntaxError) as exc:
        raise AssertionError(f"no reading for the expression {original!r}") from exc


def _evaluate(condition: str, event: dict, cancelled: bool = False) -> bool:
    return bool(_value(condition, event, cancelled))


@pytest.fixture(scope="module")
def workflow() -> dict:
    return yaml.safe_load(CI.read_text())


@pytest.fixture(scope="module")
def jobs(workflow) -> dict:
    return workflow["jobs"]


def _needs(job: dict) -> list[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else needs


def _runs(jobs: dict, name: str, event: dict) -> bool:
    job = jobs[name]
    condition = job.get("if")
    if condition is not None and not _evaluate(condition, event):
        return False
    if condition is not None and RUNS_PAST_A_SKIPPED_NEED.search(condition):
        return True
    return all(_runs(jobs, need, event) for need in _needs(job))


def _steps_run(job: dict, event: dict, cancelled: bool) -> list[str]:
    ran = []
    for step in job["steps"]:
        condition = step.get("if", "success()")
        if not STATUS_FUNCTION.search(condition):
            condition = f"success() && ({condition})"
        if _evaluate(condition, event, cancelled):
            ran.append(step["name"])
    return ran


def _pull_request_jobs(jobs: dict) -> set[str]:
    """Every job some pull_request event runs, ci-ok aside."""
    events = [e for e in EVENTS.values() if e["name"] == "pull_request"]
    return {name for name in jobs if name != "ci-ok" and any(_runs(jobs, name, e) for e in events)}


def test_ci_ok_needs_every_job_a_pull_request_runs(jobs):
    assert sorted(_needs(jobs["ci-ok"])) == sorted(_pull_request_jobs(jobs))


@pytest.mark.parametrize("event", EVENTS)
def test_every_gate_runs_unless_the_event_is_a_title_or_body_edit(jobs, event):
    """ci-ok takes a skipped gate for a fault, so a gate must run on every
    event it judges the gates for, and none may run on an edit that changes no
    code: there, one failing would redden a PR whose code nobody touched."""
    for gate in _needs(jobs["ci-ok"]):
        assert _runs(jobs, gate, EVENTS[event]) is (EVENTS[event] is not TITLE_EDIT), gate


def test_the_deploy_waits_on_exactly_the_gates_ci_ok_needs(jobs):
    """Every deploy starts at staging. A gate it did not wait on would guard PRs
    and not main."""
    assert set(_needs(jobs["staging"])) & _pull_request_jobs(jobs) == set(_needs(jobs["ci-ok"]))


def test_ci_ok_never_skips(jobs):
    assert jobs["ci-ok"]["if"] == "always()"


@pytest.mark.parametrize("event", EVENTS)
def test_only_a_run_that_judged_the_pull_request_reports_as_ci_ok(jobs, event):
    """Protection reads the newest check of the name it requires. A title or
    body edit judges nothing, and a dispatch or push judges no merge, so any of
    them reporting as `ci-ok` would displace the verdict that counts."""
    name = _value(jobs["ci-ok"]["name"], EVENTS[event])
    assert (name == "ci-ok") is (event in DECIDING), name
    assert name.startswith("ci-ok")


@pytest.mark.parametrize("cancelled", [False, True], ids=["finished", "cancelled"])
@pytest.mark.parametrize("event", EVENTS)
def test_the_gates_are_judged_wherever_ci_ok_is_a_verdict(jobs, event, cancelled):
    """Even in a cancelled run, whose cancelled gates then fail it: a ci-ok
    whose every step skipped would pass."""
    ran = _steps_run(jobs["ci-ok"], EVENTS[event], cancelled)
    if EVENTS[event] is not TITLE_EDIT:
        assert ran == ["Every gate passed"]
    else:
        assert "Every gate passed" not in ran


@pytest.mark.parametrize("event", EVENTS)
def test_only_the_runs_that_judge_a_pull_request_share_its_group(workflow, event):
    """A group holds one run in progress and one pending, and a newcomer
    cancels or replaces them. An edit sharing the PR's group would cancel a
    run in progress, or replace a push or retarget waiting to start; a push to
    main sharing any group could be cancelled mid-deploy."""
    group = _value(workflow["concurrency"]["group"], EVENTS[event])
    assert (group == _value(workflow["concurrency"]["group"], EVENTS["synchronize"])) is (event in DECIDING), group
    assert ("12345" in group) is (event not in DECIDING), group
    cancels = _evaluate(workflow["concurrency"]["cancel-in-progress"], EVENTS[event])
    if EVENTS[event] is not TITLE_EDIT:  # alone in its group, so either way
        assert cancels is (event in DECIDING)
