"""The deploy scripts live inline in three workflows (production in ci.yml,
staging in staging-deploy-verify.yml, the test droplet in deploy-test.yml) and
nothing but review keeps them in step. These read the scripts as they stand
and pin what must hold in every copy.
"""

import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"

DEPLOYS = [
    pytest.param("ci.yml", "deploy-production", id="production"),
    pytest.param("staging-deploy-verify.yml", "deploy", id="staging"),
    pytest.param("deploy-test.yml", "deploy-test", id="test"),
]

# Any spelling of taking a container down: compose or plain docker, with or
# without flags between the command and the verb.
STOPS = re.compile(
    r"\b(?:docker-compose|docker\s+compose|docker\s+container|docker)\b.*\b(?:down|stop|rm|kill|restart)\b"
)


def _job(workflow: str, job: str) -> dict:
    jobs = yaml.safe_load((WORKFLOWS / workflow).read_text())["jobs"]
    assert job in jobs, f"{workflow} has no job {job!r}"
    return jobs[job]


def _script(workflow: str, job: str) -> list[str]:
    """The ssh-action script of a job, as bash lines."""
    scripts = [step["with"]["script"] for step in _job(workflow, job)["steps"] if "script" in step.get("with", {})]
    assert len(scripts) == 1, f"{workflow}:{job} has {len(scripts)} script blocks"
    return scripts[0].splitlines()


def _commands(lines: list[str]) -> list[str]:
    """The lines that can run: comments and blanks dropped."""
    return [line for line in lines if line.strip() and not line.lstrip().startswith("#")]


@pytest.mark.parametrize(("workflow", "job"), DEPLOYS)
def test_the_stack_stays_up_until_the_new_image_exists(workflow, job):
    # `up --build` builds before it replaces anything, so a failed build leaves
    # the previous build serving. A `down` (or a stop, in any spelling) ahead
    # of it turns every build failure into an outage with nothing to restore it.
    commands = _commands(_script(workflow, job))
    swaps = [i for i, line in enumerate(commands) if re.search(r"\bdocker compose up -d --build\b", line)]
    assert swaps
    # Ahead of the first swap, specifically. Once the new image is serving, a
    # deploy may take down what the stack no longer runs — staging removes the
    # fleet container a profile-gated service leaves behind — and that stops
    # nothing the build failure above would have needed.
    assert not [line for line in commands[: swaps[0]] if STOPS.search(line)]


# ── Housekeeping ─────────────────────────────────────────────────────────────
# Every build orphans the image it replaces, and nothing on the boxes removes
# them: no prune cron, no timer. Left alone they accumulate one per deploy
# until they trip the disk pre-flight, which refuses to deploy under 2GB free.

PRUNE = r"^docker image prune\b"
# Any spelling that switches the prune from "dangling" to "unreferenced by a
# container": -a, --all, and clusters like -af.
PRUNE_ALL = re.compile(r"\s(?:--all\b|-[a-z]*a[a-z]*\b)")


@pytest.mark.parametrize(("workflow", "job"), DEPLOYS)
def test_orphaned_images_are_pruned_every_deploy(workflow, job):
    assert [line for line in _commands(_script(workflow, job)) if re.search(PRUNE, line)]


@pytest.mark.parametrize(("workflow", "job"), DEPLOYS)
def test_the_image_prune_takes_only_what_nothing_names(workflow, job):
    # Dangling-only. `-a` prunes whatever no container references, and the
    # saved rollback image is exactly that, so it would delete the way back.
    # These boxes also carry images for other stacks (tower-finder-service).
    for line in _commands(_script(workflow, job)):
        if re.search(PRUNE, line):
            assert not PRUNE_ALL.search(line), line


# ── The deploy-failure rollback's marker ─────────────────────────────────────
# The rollback job keys on `.deploy-in-progress`, and its decisions are only
# right if the deploy writes the marker after pre-deploy.sh has taken the
# rollback point, before anything moves, with no refusal in between, and clears
# it only once the new container answers.

ROLLBACKS = [
    pytest.param(
        "ci.yml",
        "deploy-production",
        "rollback-production-on-deploy-failure",
        "retina-prod",
        "${{ github.run_id }}",
        id="production",
    ),
    pytest.param(
        "staging-deploy-verify.yml", "deploy", "rollback", "retina-staging", "${{ github.run_id }}", id="staging"
    ),
    pytest.param(
        "deploy-test.yml", "deploy-test", "rollback-test-on-deploy-failure", "retina-test", "${RUN_ID}", id="test"
    ),
]
MARKED_DEPLOYS = [pytest.param(p.values[0], p.values[1], id=p.id) for p in ROLLBACKS]
IDENTIFIED_DEPLOYS = [pytest.param(p.values[0], p.values[1], p.values[3], id=p.id) for p in ROLLBACKS]

SNAPSHOT = "^" + re.escape("bash deploy/pre-deploy.sh") + "$"
MARKER_WRITE = re.escape("> ${{ env.APP_DIR }}/.deploy-in-progress") + "$"
MARKER_CLEAR = "^" + re.escape("rm -f ${{ env.APP_DIR }}/.deploy-in-progress") + "$"
TREE_MOVE = r"^git reset --hard\b"
SWAP = r"^docker compose up -d --build\b.* server$"


def _index(lines: list[str], pattern: str) -> int:
    hits = [i for i, line in enumerate(lines) if re.search(pattern, line)]
    assert len(hits) == 1, f"expected one line matching {pattern!r}, found {len(hits)}"
    return hits[0]


@pytest.mark.parametrize(("workflow", "deploy"), MARKED_DEPLOYS)
def test_marker_is_written_after_the_rollback_point_and_before_anything_moves(workflow, deploy):
    lines = _script(workflow, deploy)
    assert _index(lines, SNAPSHOT) < _index(lines, MARKER_WRITE) < _index(lines, TREE_MOVE) < _index(lines, SWAP)


@pytest.mark.parametrize(("workflow", "deploy"), MARKED_DEPLOYS)
def test_nothing_between_the_marker_write_and_the_tree_move_can_refuse(workflow, deploy):
    # A refusal there would be read by the rollback job as a half-finished
    # deploy. Refusals belong above the snapshot, where the stack is untouched.
    # deploy-test.yml injects one there on purpose, to prove the rollback acts.
    lines = _script(workflow, deploy)
    window = lines[_index(lines, MARKER_WRITE) + 1 : _index(lines, TREE_MOVE)]
    injecting = False
    refusals = []
    for line in window:
        if re.match(r'if \[ "\$FAIL_AT" = ', line):
            injecting = True
        elif line == "fi":
            injecting = False
        elif not injecting and re.search(r"\bexit [1-9]", line):
            refusals.append(line)
    assert not refusals


@pytest.mark.parametrize(("workflow", "deploy", "host"), IDENTIFIED_DEPLOYS)
def test_a_stale_marker_refuses_the_deploy_before_the_rollback_point_is_taken(workflow, deploy, host):
    # Snapshotting over a box left mid-deploy would capture the broken build as
    # the rollback point, so the refusal has to come first; identity first of
    # all, since it is the one check that does not depend on the box's state.
    lines = _script(workflow, deploy)
    identity = _index(lines, re.escape(f'"$(hostname)" != "{host}"'))
    refusal = _index(lines, "^" + re.escape("if [ -f .deploy-in-progress ]; then") + "$")
    assert identity < refusal < _index(lines, SNAPSHOT)


@pytest.mark.parametrize(("workflow", "deploy"), MARKED_DEPLOYS)
def test_the_image_prune_runs_after_the_rollback_point_is_saved(workflow, deploy):
    # pre-deploy.sh is what puts a tag on the outgoing image. Prune ahead of it
    # and that image is still untagged, so the prune takes the rollback point.
    lines = _script(workflow, deploy)
    assert _index(lines, SNAPSHOT) < _index(lines, PRUNE)


@pytest.mark.parametrize(("workflow", "deploy"), MARKED_DEPLOYS)
def test_marker_is_cleared_only_once_the_app_answers(workflow, deploy):
    lines = _script(workflow, deploy)
    assert _index(lines, MARKER_CLEAR) > _index(lines, r"(?i)health check failed after")


@pytest.mark.parametrize(("workflow", "deploy", "rollback", "host", "run_id"), ROLLBACKS)
def test_rollback_job_reads_back_what_the_deploy_wrote(workflow, deploy, rollback, host, run_id):
    deploy_lines = _script(workflow, deploy)
    rollback_lines = _script(workflow, rollback)
    written = deploy_lines[_index(deploy_lines, MARKER_WRITE) - 1]
    assert f'echo "run {run_id} started ' in written
    _index(rollback_lines, "^" + re.escape(f'  "run {run_id} "*) ;;') + "$")
    _index(rollback_lines, re.escape(f'"$(hostname)" = "{host}"'))
    condition = _job(workflow, rollback)["if"]
    assert f"needs.{deploy}.result == 'failure'" in condition
    assert f"needs.{deploy}.result == 'cancelled'" in condition
    assert "always()" in condition


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/topic", "refs/pull/417/merge"])
@pytest.mark.parametrize("event", ["push", "pull_request", "workflow_dispatch"])
@pytest.mark.parametrize("result", ["failure", "cancelled", "success", "skipped"])
def test_production_rollback_requires_a_failed_or_cancelled_main_push(ref, event, result):
    # A cancelled PR run can report a skipped deploy as cancelled. Evaluate
    # the real job condition: the remote marker check is too late to prevent
    # an unauthorized workflow event from opening a production SSH session.
    condition = " ".join(_job("ci.yml", "rollback-production-on-deploy-failure")["if"].split())
    for field, value in (
        ("github.ref", ref),
        ("github.event_name", event),
        ("needs.deploy-production.result", result),
    ):
        condition = condition.replace(field, repr(value))
    # This expression uses only equality and boolean operators, whose
    # semantics are the same for these concrete strings in Actions/Python.
    condition = condition.replace("always()", "True").replace("&&", "and").replace("||", "or")
    allowed = eval(condition, {"__builtins__": {}})
    assert allowed is (ref == "refs/heads/main" and event == "push" and result in {"failure", "cancelled"})
