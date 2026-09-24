"""deploy/wait-for-health.sh, run as the deploys run it against a stub docker.

It is the only health wait the three deploys and rollback.sh have, and none of
those runs on a pull request; short of dispatching deploy-test.yml at a branch,
this is where the loop runs before a merge. `docker` and `sleep` are replaced
by exported shell functions: the stub answers the probe on a chosen attempt, and
`sleep` advances bash's SECONDS instead of waiting, so the whole budget runs at
once.
"""

import re
import subprocess
from pathlib import Path

from tests.migration_helpers import BACKEND

WAIT_SH = BACKEND.parent / "deploy" / "wait-for-health.sh"
COMPOSE = BACKEND.parent / "docker-compose.yml"

# Every docker call is logged to $CALLS; the probe answers on attempt
# $ANSWER_ON and reports anything it finds on stdin.
STUBS = r"""
docker() {
  echo "$*" >> "$CALLS"
  case "$1 $2" in
    "compose exec")
      ATTEMPTS=$((ATTEMPTS + 1))
      if read -r line; then echo "stdin: $line" >> "$CALLS"; fi
      [ "$ATTEMPTS" = "$ANSWER_ON" ] ;;
    "compose logs") echo "stub server logs" ;;
    *) echo "unexpected docker call: $*" >&2; return 2 ;;
  esac
}
sleep() { SECONDS=$((SECONDS + $1)); }
export -f docker sleep
"""


def _run(tmp_path: Path, answer_on: int | None, *, body: str = f"bash {WAIT_SH}") -> tuple:
    calls = tmp_path / "calls"
    calls.touch()
    env = {
        "PATH": "/usr/bin:/bin",
        "CALLS": str(calls),
        "ATTEMPTS": "0",
        "ANSWER_ON": str(answer_on) if answer_on else "never",
    }
    result = subprocess.run(  # noqa: S603, S607
        ["bash", "-c", f"{STUBS}\n{body}\n"],
        env=env,
        input="the rest of the caller's script\n",
        capture_output=True,
        text=True,
        check=False,
    )
    return result, calls.read_text().splitlines()


def _probes(calls: list[str]) -> list[str]:
    return [c for c in calls if c.startswith("compose exec")]


def test_an_answer_on_the_first_attempt_returns_at_once(tmp_path):
    result, calls = _run(tmp_path, answer_on=1)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["Server healthy after 0s"]
    assert len(_probes(calls)) == 1


def test_it_polls_once_a_second_until_the_server_answers(tmp_path):
    result, calls = _run(tmp_path, answer_on=10)

    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines[:-1] == [f"Waiting for health check... {s}s" for s in range(9)]
    assert lines[-1] == "Server healthy after 9s"
    assert len(_probes(calls)) == 10


def test_it_gives_up_once_the_budget_is_spent_and_shows_the_logs(tmp_path):
    result, calls = _run(tmp_path, answer_on=None)

    assert result.returncode == 1
    # One attempt per second from 0 s to 90 s inclusive.
    assert len(_probes(calls)) == 91
    assert "Health check failed after 90s" in result.stdout
    assert result.stdout.rstrip().endswith("stub server logs")


def test_the_probe_is_the_container_s_own_health_endpoint_with_a_timeout(tmp_path):
    _, calls = _run(tmp_path, answer_on=1)

    (probe,) = _probes(calls)
    assert probe.startswith("compose exec -T server python3 -c ")
    assert "urlopen('http://localhost:8000/api/health', timeout=5)" in probe


def test_the_probe_leaves_the_caller_s_stdin_alone(tmp_path):
    # `docker compose exec` reads stdin, so a caller whose script arrives on it
    # (`bash -s`) would lose the rest of that script to the first probe.
    _, calls = _run(tmp_path, answer_on=3)

    assert not [c for c in calls if c.startswith("stdin:")], calls


def test_sourcing_defines_the_wait_without_running_it(tmp_path):
    # rollback.sh sources it before moving the tree and calls it later.
    result, calls = _run(tmp_path, answer_on=1, body=f"source {WAIT_SH}; echo sourced; wait_for_health")

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == ["sourced", "Server healthy after 0s"]
    assert len(_probes(calls)) == 1


def test_the_budget_covers_the_healthcheck_start_period():
    # A shorter wait fails a boot the healthcheck would still accept.
    budget = re.search(r"^HEALTH_WAIT_SECONDS=(\d+)$", WAIT_SH.read_text(), re.M)
    start_period = re.search(r"start_period: (\d+)s", COMPOSE.read_text())
    assert budget and start_period
    assert int(budget.group(1)) >= int(start_period.group(1))
