"""Every deploy check that reads the test router sends the radar key.

The router's reads are for operators rather than the public, and a check that
reads them anonymously is one refusal away from failing, which on production
rolls the deploy back. So each request to /api/test/ in the smoke suites and the
E2E specs carries the key, and each suite is handed its own environment's key.
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

# A request's URL is built from a host variable (`${API_URL}`, `$API_URL`,
# `${hosts.api}`); a path named in a label or a message is not a request.
_TEST_ROUTER_URL = re.compile(r"\$\{?[\w.]+\}?/api/test/")


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
    requests = [c for c in _shell_commands(_SHELL_SUITES[name]()) if _TEST_ROUTER_URL.search(c)]
    assert requests, f"{name} no longer reads the test router, so this check guards nothing"
    anonymous = [c.strip() for c in requests if "RADAR_KEY_HEADER" not in c]
    assert not anonymous, f"{name} reads the test router without the radar key: {anonymous}"


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


def test_the_staging_suite_refuses_to_run_without_a_key() -> None:
    # An empty PATH, so a suite that ran on regardless fails at its first
    # external command rather than probing staging from a unit test.
    env = {k: v for k, v in os.environ.items() if k != "RADAR_API_KEY"} | {"PATH": "/nonexistent"}
    result = subprocess.run(["/bin/bash", str(_STAGING_SMOKE)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert "RADAR_API_KEY" in result.stderr, result.stderr
