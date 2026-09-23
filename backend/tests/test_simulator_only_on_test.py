"""Only the test droplet runs a simulator.

Production and staging carry the real network and nothing else: their `fleet`
service sits behind a compose profile no deploy enables, their server leaves
SYNTHETIC_FLEET_ENABLED unset so the simulation ingest path is never mounted,
and the staging deploy starts `server` alone. These pin that shape, because a
bare `docker compose up -d` or a `--no-deps fleet` line in a deploy script is
all it takes to bring a fleet back — and the smoke suite has to assert the
absence rather than, as it once did, wait for the presence.
"""

import re
from pathlib import Path

import orjson
import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_OVERLAYS = {name: _REPO / f"docker-compose.{name}.yml" for name in ("prod", "staging", "test")}
_WORKFLOW = _REPO / ".github" / "workflows" / "staging-deploy-verify.yml"
_SMOKE = _REPO / "deploy" / "staging-smoke-test.sh"
_SIM_INGEST = _REPO / "backend" / "routes" / "sim_ingest.py"

# The environments that must run no simulator. `test` is the one that may.
_REAL_ONLY = ("prod", "staging")


class _ComposeLoader(yaml.SafeLoader):
    """Compose's merge tags (`!override`, `!reset`) are not YAML the safe loader knows."""


def _ignore_tag(loader: yaml.Loader, suffix: str, node: yaml.Node):
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_multi_constructor("", _ignore_tag)


def _overlay(name: str) -> dict:
    return yaml.load(_OVERLAYS[name].read_text(), Loader=_ComposeLoader)


def _server_env(name: str) -> dict[str, str]:
    entries = _overlay(name)["services"]["server"]["environment"]
    return dict(e.split("=", 1) for e in entries)


@pytest.mark.parametrize("env", _REAL_ONLY)
def test_no_simulator_flag_off_the_test_droplet(env):
    # main.py mounts routes/sim_ingest.py only under this flag; an environment
    # that runs no fleet must not carry the write path the fleet POSTs through.
    assert "SYNTHETIC_FLEET_ENABLED" not in _server_env(env), f"{env} mounts the simulation ingest path"


def test_the_test_droplet_still_sets_the_flag():
    assert _server_env("test").get("SYNTHETIC_FLEET_ENABLED") == "1"


@pytest.mark.parametrize("env", _REAL_ONLY)
def test_the_fleet_is_behind_an_unenabled_profile(env):
    # A profile is what keeps a bare `docker compose up -d` on the droplet from
    # bringing the fleet back. Named `sim` in both overlays so the runbook's one
    # bring-it-back-for-an-afternoon incantation is the same on each box.
    assert _overlay(env)["services"]["fleet"].get("profiles") == ["sim"], f"{env}'s fleet starts by default"


def test_the_test_droplets_fleet_starts_by_default():
    assert "profiles" not in _overlay("test")["services"]["fleet"]


def _deploy_script() -> str:
    steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["deploy"]["steps"]
    scripts = [s["with"]["script"] for s in steps if "script" in s.get("with", {})]
    assert len(scripts) == 1, f"expected one ssh deploy step, found {len(scripts)}"
    return scripts[0]


def test_the_staging_deploy_starts_the_server_alone():
    # Naming `fleet` on a compose command line auto-enables its profile, so the
    # deploy must never mention it — not even with --no-deps, which is how
    # staging used to start it as a separate step.
    runnable = [line for line in _deploy_script().splitlines() if not line.lstrip().startswith("#")]
    compose_lines = [line for line in runnable if "docker compose" in line]
    assert compose_lines, "the deploy no longer runs docker compose at all"
    for line in compose_lines:
        assert not re.search(r"\bfleet\b", line), f"the staging deploy starts the fleet: {line.strip()}"
        assert "--profile" not in line, f"the staging deploy enables a profile: {line.strip()}"


def test_the_smoke_job_no_longer_waits_for_a_fleet():
    steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["smoke-tests"]["steps"]
    waits = [s for s in steps if "fleet" in s.get("name", "").lower()]
    assert not waits, f"the smoke job still gates on a fleet: {[s['name'] for s in waits]}"
    assert "synthetic_active" not in _SMOKE.read_text(), "the smoke suite still asserts fleet nodes are answering"


def test_the_smoke_suite_asserts_the_simulator_is_absent():
    # Two probes, because the flag and the mount are set in different places:
    # the health body carries the flag, and the ingest route must 404 (a
    # mounted POST route answers a GET with 405).
    smoke = _SMOKE.read_text()
    assert "'\"synthetic_fleet\":false'" in smoke, "the smoke suite does not assert /api/health reports no fleet"
    probe = re.search(r'check_status\s+"[^"]*"\s+"\$\{API_URL\}(/api/sim/[^"?]+)\??[^"]*"\s+"404"', smoke)
    assert probe, "the smoke suite does not probe a sim ingest route for 404"
    # The route it probes must be one sim_ingest.py actually mounts, or the 404
    # proves nothing.
    mounted = set(re.findall(r'@router\.(?:post|get|put|delete)\("([^"]+)"', _SIM_INGEST.read_text()))
    assert probe[1] in mounted, f"{probe[1]} is not a route sim_ingest.py mounts: {sorted(mounted)}"


def test_health_serialises_the_flag_the_smoke_suite_greps_for(client, monkeypatch):
    # The smoke suite matches the literal `"synthetic_fleet":false`, so the
    # serialisation — no space after the colon, lowercase false — is part of
    # the contract, not an accident of the JSON encoder.
    monkeypatch.delenv("SYNTHETIC_FLEET_ENABLED", raising=False)
    r = client.get("/api/health")
    assert r.status_code == 200
    assert '"synthetic_fleet":false' in r.text
    assert orjson.loads(r.content)["synthetic_fleet"] is False
