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
    assert [line for line in commands if re.search(r"\bdocker compose up -d --build\b", line)]
    assert not [line for line in commands if STOPS.search(line)]


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
