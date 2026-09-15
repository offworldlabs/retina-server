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
