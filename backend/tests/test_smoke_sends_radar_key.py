"""Every deploy check that reads the test router sends the radar key.

The router's reads answer only an administrator or that key, and a check that
reads them anonymously fails, which on production rolls the deploy back. So each
request to /api/test/ in the smoke suites and the E2E specs carries the key, and
each suite is handed its own environment's key. The one deliberate exception is
the check that an anonymous read is refused, which each suite makes on both
hosts that route /api/ to the app.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_STAGING_SMOKE = _REPO / "deploy" / "staging-smoke-test.sh"
_CI = _REPO / ".github" / "workflows" / "ci.yml"
_STAGING_WORKFLOW = _REPO / ".github" / "workflows" / "staging-deploy-verify.yml"
_E2E_SPECS = sorted((_REPO / "e2e" / "specs").glob("*.spec.ts"))

# A request's URL starts at a host, as a variable (`${API_URL}`, `$API_URL`,
# `${hosts.api}`) or written out; a path named in a label or a message is not a
# request.
_TEST_ROUTER_URL = re.compile(r"(?:\$\{?[\w.]+\}?|https?://[\w.-]+)/api/test/")

# check_status's expected code closes the command.
_EXPECTS_REFUSAL = re.compile(r'"401"\s*$')


def _production_smoke_step() -> dict:
    steps = yaml.safe_load(_CI.read_text())["jobs"]["production-smoke-tests"]["steps"]
    step = next((s for s in steps if s.get("id") == "smoke"), None)
    assert step, "ci.yml's production-smoke-tests job has no step with id `smoke`"
    return step


def _staging_smoke_step() -> dict:
    steps = yaml.safe_load(_STAGING_WORKFLOW.read_text())["jobs"]["smoke-tests"]["steps"]
    step = next((s for s in steps if "staging-smoke-test.sh" in s.get("run", "")), None)
    assert step, "staging-deploy-verify.yml's smoke-tests job no longer runs deploy/staging-smoke-test.sh"
    return step


def _shell_commands(script: str) -> list[str]:
    """The script's commands with continuation lines joined and comments dropped."""
    joined = re.sub(r"\\\n\s*", " ", script)
    return [line for line in joined.splitlines() if not line.lstrip().startswith("#")]


_SHELL_SUITES = {
    "staging-smoke-test.sh": lambda: _STAGING_SMOKE.read_text(),
    "ci.yml production smoke": lambda: _production_smoke_step()["run"],
}


@pytest.mark.parametrize("name", sorted(_SHELL_SUITES))
def test_every_test_router_request_in_a_smoke_suite_sends_the_key(name: str) -> None:
    script = _SHELL_SUITES[name]()
    requests = [c for c in _shell_commands(script) if _TEST_ROUTER_URL.search(c)]
    assert requests, f"{name} no longer reads the test router, so this check guards nothing"
    # The production suite wraps its keyed reads in check_keyed, which counts
    # only while its own body sends the key.
    helper = re.search(r"check_keyed\(\) \{(.*?)\n\s*\}\n", script, re.S)
    helper_sends_key = bool(helper) and "RADAR_KEY_HEADER" in helper.group(1)

    def sends_key(command: str) -> bool:
        return "RADAR_KEY_HEADER" in command or (command.lstrip().startswith("check_keyed ") and helper_sends_key)

    anonymous = [c.strip() for c in requests if not sends_key(c) and not _EXPECTS_REFUSAL.search(c)]
    assert not anonymous, f"{name} reads the test router without the radar key: {anonymous}"


@pytest.mark.parametrize("name", sorted(_SHELL_SUITES))
def test_each_smoke_suite_asserts_an_anonymous_read_is_refused_on_both_hosts(name: str) -> None:
    refusals = [
        c
        for c in _shell_commands(_SHELL_SUITES[name]())
        if c.lstrip().startswith("check_status ")
        and "/api/test/dashboard" in c
        and _EXPECTS_REFUSAL.search(c)
        and "RADAR_KEY_HEADER" not in c
    ]
    hosts = {("app" if re.search(r"APP_URL|app\.retina\.fm", c) else "api") for c in refusals}
    assert hosts == {"api", "app"}, f"{name} asserts the anonymous refusal on {sorted(hosts) or 'no host'}"


@pytest.mark.parametrize("spec", _E2E_SPECS, ids=lambda p: p.name)
def test_every_test_router_request_in_an_e2e_spec_sends_the_key(spec: Path) -> None:
    requests = [line for line in spec.read_text().splitlines() if _TEST_ROUTER_URL.search(line)]
    anonymous = [line.strip() for line in requests if '"X-API-Key"' not in line]
    assert not anonymous, f"{spec.name} reads the test router without the radar key: {anonymous}"


@pytest.mark.parametrize(
    ("step", "secret"),
    [(_staging_smoke_step, "STAGING_RADAR_API_KEY"), (_production_smoke_step, "RADAR_API_KEY")],
    ids=["staging", "production"],
)
def test_each_smoke_suite_is_given_its_own_environments_key(step, secret: str) -> None:
    assert step().get("env", {}).get("RADAR_API_KEY") == f"${{{{ secrets.{secret} }}}}"


def _staging_key_preflight() -> str:
    """The deploy's radar-key check, which must sit with the other pre-flights."""
    steps = yaml.safe_load(_STAGING_WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
    (script,) = [s["with"]["script"] for s in steps if "script" in s.get("with", {})]
    start = script.index("# Pre-flight: the radar key.")
    end = script.index("# Pre-flights go above this line.")
    assert start < end, "the radar-key check runs after the box is already mid-deploy"
    return script[start:end]


@pytest.mark.parametrize(
    ("env_text", "refused"),
    [
        (None, True),
        ("", True),
        ("RADAR_API_KEY=\n", True),
        # compose passes the last occurrence on
        ("RADAR_API_KEY=abc\nRADAR_API_KEY=\n", True),
        ("RADAR_API_KEY=\nRADAR_API_KEY=abc\n", False),
        ("RADAR_API_KEY=abc\n", False),
    ],
    ids=["no .env", "no line", "empty", "emptied last", "set last", "set"],
)
def test_the_staging_deploy_refuses_a_box_without_a_radar_key(tmp_path, env_text, refused) -> None:
    (tmp_path / "backend").mkdir()
    if env_text is not None:
        (tmp_path / "backend" / ".env").write_text(env_text)
    result = subprocess.run(
        ["/bin/bash", "-c", _staging_key_preflight()], cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert (result.returncode != 0) == refused, result.stdout


def test_the_staging_suite_refuses_to_run_without_a_key() -> None:
    # An empty PATH, so a suite that ran on regardless fails at its first
    # external command rather than probing staging from a unit test.
    env = {k: v for k, v in os.environ.items() if k != "RADAR_API_KEY"} | {"PATH": "/nonexistent"}
    result = subprocess.run(["/bin/bash", str(_STAGING_SMOKE)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "RADAR_API_KEY" in result.stderr, result.stderr
