"""The deploy scripts live inline in three workflows (production in ci.yml,
staging in staging-deploy-verify.yml, the test droplet in deploy-test.yml) and
nothing but review keeps them in step. These read the scripts as they stand
and pin what must hold in every copy.
"""

import functools
import re
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


@functools.cache
def _workflow(workflow: str) -> dict:
    return yaml.safe_load((WORKFLOWS / workflow).read_text())


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
    jobs = _workflow(workflow)["jobs"]
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
BUILDER_PRUNE = r"^docker builder prune\b"
# Every command that can prune images, wherever it sits on a line, up to the
# next separator or trailing comment. `system prune` takes images too.
IMAGE_PRUNES = re.compile(r"\bdocker\s+(?:image|system)\s+prune\b[^;&|#]*")
# Any spelling that switches the prune from "dangling" to "unreferenced by a
# container": -a, --all, and clusters like -af.
PRUNE_ALL = re.compile(r"\s(?:--all\b|-[a-z]*a[a-z]*\b)")


@pytest.mark.parametrize(("workflow", "job"), DEPLOYS)
def test_orphaned_images_are_pruned_every_deploy(workflow, job):
    assert [line for line in _commands(_script(workflow, job)) if re.search(PRUNE, line)]


@pytest.mark.parametrize(("workflow", "job"), DEPLOYS)
def test_no_prune_takes_an_image_something_names(workflow, job):
    # Dangling-only. `-a` prunes whatever no container references, and the
    # saved rollback image is exactly that, so it would delete the way back.
    # These boxes also carry images for other stacks (tower-finder-service).
    # Comments out before continuations are joined: bash does not continue a
    # comment that ends in a backslash.
    script = "\n".join(_commands(_script(workflow, job))).replace("\\\n", " ")
    prunes = [m.group() for line in script.splitlines() for m in IMAGE_PRUNES.finditer(line)]
    assert prunes
    assert not [prune for prune in prunes if PRUNE_ALL.search(prune)]


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
def test_the_build_cache_is_trimmed_after_the_images_that_pin_it(workflow, deploy):
    # A cached layer an image still holds is not the cache's to free. Trim the
    # cache first and the outgoing images keep it; their prune then releases
    # it with nothing left in the deploy to sweep it up. Both ahead of the
    # build, which is what needs the room.
    lines = _script(workflow, deploy)
    assert _index(lines, SNAPSHOT) < _index(lines, PRUNE) < _index(lines, BUILDER_PRUNE) < _index(lines, SWAP)


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


def _allowed(workflow: str, job: str, values: dict[str, str], *, cancelled: bool = False) -> bool:
    """A job's `if:`, evaluated with each context field replaced by a value."""
    condition = " ".join(_job(workflow, job)["if"].split())
    for field, value in values.items():
        # Whole fields only: github.ref must not also rewrite github.ref_name.
        condition = re.sub(rf"(?<![\w.-]){re.escape(field)}(?![\w-])", repr(value), condition)
    # The rest is equality and boolean operators over concrete strings, whose
    # semantics are the same in Actions and Python. Any other function is left
    # undefined, and so fails the test rather than guessing.
    condition = condition.replace("!cancelled()", repr(not cancelled)).replace("always()", "True")
    assert not re.search(r"!(?!=)", condition), f"no Python equivalent for a negation in {condition!r}"
    condition = condition.replace("&&", " and ").replace("||", " or ")
    return eval(condition, {"__builtins__": {}})


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/topic", "refs/pull/417/merge"])
@pytest.mark.parametrize("event", ["push", "pull_request", "workflow_dispatch"])
@pytest.mark.parametrize("result", ["failure", "cancelled", "success", "skipped"])
@pytest.mark.parametrize("cancelled", [False, True])
def test_production_rollback_requires_a_failed_or_cancelled_main_push(ref, event, result, cancelled):
    # A cancelled PR run can report a skipped deploy as cancelled. Evaluate
    # the real job condition: the remote marker check is too late to prevent
    # an unauthorized workflow event from opening a production SSH session.
    # Cancelling the run does not stop it: a cancel mid-deploy leaves the box
    # as half-deployed as a crash does.
    allowed = _allowed(
        "ci.yml",
        "rollback-production-on-deploy-failure",
        {"github.ref": ref, "github.event_name": event, "needs.deploy-production.result": result},
        cancelled=cancelled,
    )
    assert allowed is (ref == "refs/heads/main" and event == "push" and result in {"failure", "cancelled"})


# ── Third-party code never shares a job with a droplet key ───────────────────
# A package manager runs install scripts from its registry, an action runs its
# author's code, and a container image is someone else's filesystem. Any of them
# can tamper with the environment a later step hands a root key to, so a job
# that runs one must not be able to read a key. Rollbacks that follow such a job
# run in a job of their own.

# A tripwire for the honest mistake, not a sandbox: flags may sit between a
# tool and its verb, and a script a step calls is not followed.
THIRD_PARTY_CODE = re.compile(
    r"\b(?:(?:npm|pip3?|apt(?:-get)?|gem|cargo|go|bun|poetry)\b[^\n;&|]*\binstall"
    r"|npm\b[^\n;&|]*\b(?:ci|i|exec)|npx|yarn|pnpm|pipx|uvx|apk\s+add|curl\b[^\n;&]*\|\s*(?:ba)?sh"
    r"|uv\b[^\n;&|]*\b(?:sync|add|run|pip|tool)|pre-commit"
    r"|docker(?:-compose|\b[^\n;&|]*\b(?:run|build|pull|compose)))\b",
    re.IGNORECASE,
)
# The actions a key-holding job may use: the checkout, and the ssh transport the
# key is for. Any other action counts as third-party code.
KEY_JOB_ACTIONS = ("actions/checkout@", "appleboy/ssh-action@")
# The droplet keys are all named *SSH*KEY. A dynamic lookup, the whole secrets
# object, or `secrets: inherit` could be any of them. Case-blind, as Actions
# expressions are.
SSH_KEY = re.compile(
    r"\bsecrets(?:\.\w*SSH\w*KEY\w*\b|\[\s*['\"]\w*SSH\w*KEY\w*['\"]\s*\]|\[\s*(?!['\"])|:\s*inherit\b)"
    r"|\btoJSON\(\s*secrets\s*\)",
    re.IGNORECASE,
)


def _workflow_files() -> list[Path]:
    return sorted([*WORKFLOWS.glob("*.yml"), *WORKFLOWS.glob("*.yaml")])


def _workflow_jobs() -> list:
    return [
        pytest.param(path.name, name, job, id=f"{path.name}:{name}")
        for path in _workflow_files()
        for name, job in _workflow(path.name)["jobs"].items()
    ]


def _runs_third_party_code(job: dict) -> bool:
    steps = job.get("steps", [])
    return (
        "container" in job
        or "services" in job
        # Another repository's reusable workflow.
        or ("uses" in job and not str(job["uses"]).startswith("./"))
        or any("uses" in step and not str(step["uses"]).startswith(KEY_JOB_ACTIONS) for step in steps)
        or any(THIRD_PARTY_CODE.search(step.get("run", "")) for step in steps)
    )


def test_the_guard_recognises_what_the_jobs_run():
    # Guards the guard: patterns that matched nothing would pass every job.
    assert _runs_third_party_code(_job("ci.yml", "web-build"))
    assert _runs_third_party_code(_job("ci.yml", "backend-tests"))
    assert _runs_third_party_code(_job("ci.yml", "lint"))
    assert _runs_third_party_code(_job("ci.yml", "e2e-prod"))
    assert _runs_third_party_code({"services": {"db": {"image": "postgres"}}, "steps": []})
    assert _runs_third_party_code({"steps": [{"uses": "docker://alpine:3"}]})
    assert _runs_third_party_code({"steps": [{"uses": "./.github/actions/setup-uv"}]})
    assert _runs_third_party_code({"steps": [{"run": "uv run python scripts/check.py"}]})
    assert _runs_third_party_code({"steps": [{"run": "docker run --rm some/image"}]})
    assert _runs_third_party_code({"steps": [{"run": "npm --prefix frontend ci"}]})
    assert _runs_third_party_code({"steps": [{"run": "sudo apt-get -y install jq"}]})
    assert _runs_third_party_code({"uses": "someorg/repo/.github/workflows/x.yml@main"})
    assert not _runs_third_party_code(_job("ci.yml", "staging"))
    assert not _runs_third_party_code(_job("ci.yml", "deploy-production"))
    assert not _runs_third_party_code(_job("ci.yml", "production-smoke-tests"))
    assert SSH_KEY.search(yaml.safe_dump(_job("ci.yml", "deploy-production")))
    assert SSH_KEY.search("key: ${{ secrets['STAGING_SSH_KEY'] }}")
    assert SSH_KEY.search("key: ${{ secrets[format('{0}_SSH_KEY', inputs.env)] }}")
    assert SSH_KEY.search("ALL: ${{ toJson(secrets) }}")
    assert SSH_KEY.search("key: ${{ secrets.server_ssh_key }}")
    assert SSH_KEY.search("key: ${{ secrets.DEPLOY_SSH_PRIVATE_KEY }}")
    assert SSH_KEY.search(yaml.safe_dump({"uses": "someorg/repo/.github/workflows/x.yml@main", "secrets": "inherit"}))
    assert not SSH_KEY.search("RADAR_API_KEY: ${{ secrets.RADAR_API_KEY }}")


@pytest.mark.parametrize(("workflow", "name", "job"), _workflow_jobs())
def test_no_job_that_runs_third_party_code_can_read_a_droplet_key(workflow, name, job):
    if _runs_third_party_code(job):
        assert not SSH_KEY.search(yaml.safe_dump(job)), f"{workflow}:{name} runs third-party code"


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda path: path.name)
def test_no_droplet_key_is_handed_to_every_job(path):
    # Workflow-level env reaches every job, the npm ones included.
    assert not SSH_KEY.search(yaml.safe_dump(_workflow(path.name).get("env", {})))


# ── Production's E2E rollback ────────────────────────────────────────────────


def test_the_e2e_rollback_acts_on_the_test_step_alone():
    # A failed pull, checkout or npm ci fails e2e-prod as well, and says
    # nothing about production. Only the test step's own verdict may count.
    e2e = _job("ci.yml", "e2e-prod")
    assert [step for step in e2e["steps"] if step.get("id") == "e2e" and "test:e2e:prod" in step["run"]]
    assert e2e["outputs"]["tests"] == "${{ steps.e2e.outcome }}"


def test_the_e2e_rollback_holds_the_production_lock_on_the_runner():
    rollback = _job("ci.yml", "rollback-production-on-e2e-failure")
    assert "container" not in rollback
    assert rollback["needs"] == ["e2e-prod"]
    assert rollback["concurrency"] == {"group": "production-deploy", "cancel-in-progress": False}
    script = _script("ci.yml", "rollback-production-on-e2e-failure")
    identity = _index(script, re.escape('[ "$(hostname)" = "retina-prod" ]'))
    assert identity < _index(script, r"\bbash deploy/rollback\.sh$")


@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/heads/topic", "refs/pull/417/merge"])
@pytest.mark.parametrize("event", ["push", "pull_request", "workflow_dispatch"])
@pytest.mark.parametrize("tests", ["failure", "cancelled", "success", "skipped", ""])
@pytest.mark.parametrize("cancelled", [False, True])
def test_the_e2e_rollback_requires_failed_tests_on_a_main_push(ref, event, tests, cancelled):
    # "" is what the output reads when e2e-prod never reached the test step,
    # or was skipped outright. Cancelling the run stops it: an operator cancels
    # to keep a known flake from reverting a healthy production.
    allowed = _allowed(
        "ci.yml",
        "rollback-production-on-e2e-failure",
        {"github.ref": ref, "github.event_name": event, "needs.e2e-prod.outputs.tests": tests},
        cancelled=cancelled,
    )
    assert allowed is (ref == "refs/heads/main" and event == "push" and tests == "failure" and not cancelled)
