"""`just test-ci` promises to run the suite as CI's shards do, and the command
exists twice: once in the justfile, once in ci.yml's backend-tests job. Nothing
runs the recipe in CI, so a flag added to one and not the other would pass
every check while the local run stopped predicting the gate. These hold the
two together.
"""

import re
import shlex
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
JUSTFILE = ROOT / "justfile"

# What a shard adds to the shared command: its slice of the suite, and the
# threshold it must not apply to a third of the measurement.
SHARD_ONLY = re.compile(r"\s--splits \$\{\{[^}]*\}\}|\s--group \$\{\{[^}]*\}\}|\s--cov-fail-under=0")


@pytest.fixture(scope="module")
def backend_tests() -> dict:
    return yaml.safe_load(CI.read_text())["jobs"]["backend-tests"]


@pytest.fixture(scope="module")
def recipe_command() -> str:
    commands = [line for line in _recipe_body("test-ci") if " -m pytest " in line]
    assert len(commands) == 1, f"test-ci has {len(commands)} pytest commands"
    return commands[0]


def _recipe_body(name: str) -> list[str]:
    """The indented lines under `name *args:` (or `name:`) in the justfile."""
    lines = JUSTFILE.read_text().splitlines()
    header = re.compile(rf"^{re.escape(name)}(\s[^:]*)?:")
    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    assert start is not None, f"the justfile has no {name} recipe"
    body = []
    for line in lines[start + 1 :]:
        if not line.startswith((" ", "\t")):
            break
        body.append(line.strip())
    return body


def _pytest_args(command: str) -> list[str]:
    words = shlex.split(command)
    return words[words.index("pytest") + 1 :]


def test_test_ci_runs_the_shards_command_without_the_sharding(backend_tests, recipe_command):
    runs = [step["run"] for step in backend_tests["steps"] if step.get("run", "").startswith("pytest ")]
    assert len(runs) == 1, f"backend-tests has {len(runs)} pytest steps"
    shard = _pytest_args(SHARD_ONLY.sub("", runs[0]))

    recipe = _pytest_args(recipe_command)
    assert recipe[-1] == "$@", "test-ci no longer passes its arguments through"
    # Without it just hands the recipe no positional parameters and "$@" is empty.
    assert re.search(r"^\[positional-arguments\]\ntest-ci ", JUSTFILE.read_text(), re.M), "test-ci drops its arguments"
    assert recipe[:-1] == shard


def test_test_ci_sets_the_environment_the_shards_run_in(backend_tests, recipe_command):
    """All of it bar the data file's name, which only the shards need, to tell
    their three fragments apart."""
    words = shlex.split(recipe_command)
    recipe = dict(word.split("=", 1) for word in words[: words.index("-m")] if re.fullmatch(r"[A-Z_]+=\S*", word))
    steps = [step for step in backend_tests["steps"] if step.get("run", "").startswith("pytest ")]
    shard = {**backend_tests.get("env", {}), **steps[0].get("env", {})}
    shard.pop("COVERAGE_FILE", None)
    assert recipe == {name: str(value) for name, value in shard.items()}


def test_test_runs_under_the_shards_scheduler(backend_tests):
    """`just test` is the everyday run, and a suite with tests that pass or fail
    on what shares their process wants the same workers and scheduler CI uses."""
    (command,) = [line for line in _recipe_body("test") if " -m pytest " in line]
    ours = _pytest_args(command)
    runs = [step["run"] for step in backend_tests["steps"] if step.get("run", "").startswith("pytest ")]
    shard = _pytest_args(SHARD_ONLY.sub("", runs[0]))
    for flag in ("-n", "--dist"):
        assert flag in shard, f"the shards no longer pass {flag}"
        assert flag in ours, f"just test does not pass {flag}"
        assert ours[ours.index(flag) + 1] == shard[shard.index(flag) + 1], (
            f"just test's {flag} differs from the shards'"
        )
