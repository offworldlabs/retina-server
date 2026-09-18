"""The staging fleet gate: what "the fleet is up" is allowed to mean.

Every staging deploy recreates the fleet container, so the smoke job has to
wait for the simulated nodes to reconnect before it can assert anything about
them. The wait step and deploy/staging-smoke-test.sh must agree on the count
they read and must both be able to fail; these pin that.
"""

import re
import subprocess
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO / ".github" / "workflows" / "staging-deploy-verify.yml"
_SMOKE = _REPO / "deploy" / "staging-smoke-test.sh"

# The dashboard field each side reads out of /api/test/dashboard.
_FIELD = re.compile(r"\['nodes'\]\['(\w+)'\]")


@pytest.fixture(scope="module")
def wait_step() -> str:
    """The `run` block of the smoke job's fleet wait step."""
    steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["smoke-tests"]["steps"]
    matches = [s["run"] for s in steps if "wait for fleet" in s.get("name", "").lower()]
    assert len(matches) == 1, f"expected one fleet wait step, found {len(matches)}"
    return matches[0]


@pytest.fixture(scope="module")
def fleet_assertion() -> str:
    """The line in the smoke suite that asserts the fleet is present."""
    lines = [line for line in _SMOKE.read_text().splitlines() if _FIELD.search(line)]
    assert len(lines) == 1, f"expected one fleet assertion, found {len(lines)}"
    return lines[0]


def test_the_wait_step_gates_on_answering_fleet_nodes(wait_step):
    # nodes.active counts every entry in connected_nodes, including the
    # HTTP-registered pseudo-nodes. nodes.synthetic keeps entries that a
    # disconnect only marked, so it stays high after the fleet has gone. Only
    # synthetic_active means "fleet nodes answering now".
    assert _FIELD.findall(wait_step) == ["synthetic_active"], (
        f"the fleet gate reads {_FIELD.findall(wait_step)}, not the answering-fleet count"
    )


def test_the_wait_step_holds_out_for_a_fleet_not_a_node(wait_step):
    # Run 35085480844: six nodes connected, the gate took that as ready, and
    # all six had dropped eighteen seconds later. A floor read from the overlay
    # is what separates a fleet serving from one flickering.
    assert "fleet_min_active" in wait_step, "the gate no longer holds to a floor, so one node reads as a fleet"
    assert not re.search(r'-ge ["\']?1["\']?\s*\]', wait_step), "the gate is back to accepting a single node"


def test_the_wait_step_fails_when_the_fleet_never_connects(wait_step):
    # Falling out of the loop used to be indistinguishable from succeeding in
    # it, so a fleet that never came up was reported as a passing step.
    runnable = [line.strip() for line in wait_step.splitlines() if line.strip() and not line.lstrip().startswith("#")]
    assert runnable[-1] == "exit 1", f"the wait step ends with {runnable[-1]!r}, so the loop can fall through green"


def test_the_wait_steps_probe_is_bounded(wait_step):
    # Without a cap the loop's own bound means nothing against a hung origin:
    # it runs past the step timeout and is killed rather than reporting.
    for line in wait_step.splitlines():
        if "curl" in line:
            assert "--max-time" in line, f"unbounded curl in the wait step: {line.strip()}"


def test_the_smoke_suite_still_reports_when_the_gate_fails():
    # A skipped suite is why the gate could not be allowed to fail before. The
    # 31 checks ahead of the fleet assertion are how you tell this race from a
    # staging outage, so they have to run even once the gate has failed.
    steps = yaml.safe_load(_WORKFLOW.read_text())["jobs"]["smoke-tests"]["steps"]
    smoke = [s for s in steps if "bash deploy/staging-smoke-test.sh" in s.get("run", "")]
    assert len(smoke) == 1, f"expected one smoke step, found {len(smoke)}"
    # The negation, specifically: `if: cancelled()` would satisfy a looser
    # check while running the suite on precisely the runs nobody reads.
    condition = re.sub(r"\s+", "", str(smoke[0].get("if", "")))
    assert "!cancelled()" in condition, (
        f"the smoke suite does not run once the gate has failed (if: {smoke[0].get('if', '<none>')})"
    )


def test_the_gate_and_the_smoke_assertion_read_the_same_field(wait_step, fleet_assertion):
    # Two places assert the fleet is up. If they read different counts, the
    # gate can pass on one meaning and the assertion fail on another.
    assert _FIELD.findall(wait_step) == _FIELD.findall(fleet_assertion)


def test_the_smoke_fleet_assertion_retries():
    # The assertion is last in the suite, so without a retry of its own its
    # verdict depends on how long the unrelated checks ahead of it took.
    call = re.search(r"check_json_field .*?synthetic_active.*?(\d+)\s*$", _SMOKE.read_text(), re.M | re.S)
    assert call, "the fleet assertion no longer passes an attempt count"
    assert int(call[1]) > 1, f"the fleet assertion looks only {call[1]} time(s)"


def test_both_sides_take_the_floor_from_the_same_place():
    # Two places decide whether the fleet is up. A number written down twice
    # drifts; deploy/fleet-scale.sh reads it from the overlay that sets it.
    assert "fleet-scale.sh" in _SMOKE.read_text()
    assert "FLEET_NODES" in (_REPO / "deploy" / "fleet-scale.sh").read_text()


def test_synthetic_active_counts_only_fleet_nodes_answering_now():
    # The count the gate reads. Each of the three entries below satisfied the
    # old gate on run 35085480844 or the one before it.
    import orjson

    from core import state
    from routes.test import _build_dashboard_data

    entries = {
        "synth-live": {"is_synthetic": True, "status": "active"},
        "synth-gone": {"is_synthetic": True, "status": "disconnected"},
        "http-node": {"is_synthetic": False, "status": "active"},
    }
    with state.connected_nodes_lock:
        state.connected_nodes.update(entries)
    nodes = orjson.loads(_build_dashboard_data())["nodes"]

    assert nodes["synthetic_active"] == 1, "synthetic_active counted a dead or non-fleet node"
    assert nodes["active"] == 2, "active still counts the HTTP pseudo-node, which is why it cannot be the gate"
    assert nodes["synthetic"] == 2, "synthetic still keeps the disconnected node, which is why it cannot be the gate"


def _check_json_field(tmp_path: Path, body: str, attempts: int, becomes: str | None = None) -> tuple[int, int, str]:
    """Run the suite's own check_json_field against a canned body.

    Extracted rather than sourced, because sourcing the suite runs it against
    staging. `becomes` is what the endpoint starts answering once the function
    has waited, which is the only way to tell a real retry from one attempt.
    Returns its (PASS, FAIL) tally and the output it printed.
    """
    function = re.search(r"^check_json_field\(\) \{$.*?^\}$", _SMOKE.read_text(), re.M | re.S)
    assert function, "deploy/staging-smoke-test.sh no longer defines check_json_field"
    (tmp_path / "body.json").write_text(body)
    # Standing in for the wait itself, so the body can only change on a retry.
    wait = ":"
    if becomes is not None:
        (tmp_path / "later.json").write_text(becomes)
        wait = f'cp "{tmp_path}/later.json" "{tmp_path}/body.json"'

    harness = "\n".join(
        [
            "set -euo pipefail",
            "PASS=0; FAIL=0",
            # The suite reads bodies as `$($CURL "$url")`, so cat over a file is the same shape.
            "CURL=cat",
            f"sleep() {{ {wait}; }}",  # the retry's real delay would only slow the test
            function.group(),
            f'check_json_field "probe" "{tmp_path}/body.json" "[\'nodes\'][\'synthetic\']" "1" {attempts}',
            'echo "TALLY PASS=$PASS FAIL=$FAIL"',
        ]
    )
    proc = subprocess.run(["bash", "-c", harness], capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    # Its absence is the finding, not a parse error: under `set -e` an escaping
    # non-zero anywhere in the function skips this line and ends the run.
    tally = re.search(r"^TALLY PASS=(\d+) FAIL=(\d+)$", out, re.M)
    assert tally, f"check_json_field did not run to completion:\n{out}"
    return int(tally[1]), int(tally[2]), out


def test_a_retrying_check_passes_on_a_good_value(tmp_path):
    assert _check_json_field(tmp_path, '{"nodes": {"synthetic": 50}}', attempts=12)[:2] == (1, 0)


def test_a_retrying_check_looks_again_after_waiting(tmp_path):
    # The whole point of the retry, and the one thing text-matching the call
    # site cannot show: a value false on the first look and true on the second
    # has to pass. Without a second look this reports FAIL.
    passed, failed, out = _check_json_field(
        tmp_path, '{"nodes": {"synthetic": 0}}', attempts=2, becomes='{"nodes": {"synthetic": 50}}'
    )
    assert (passed, failed) == (1, 0), out


def test_a_retrying_check_gives_up_after_its_last_attempt(tmp_path):
    # The retry must not become an unbounded wait: the suite's step cap would
    # kill the run and none of the checks would report.
    passed, failed, out = _check_json_field(
        tmp_path, '{"nodes": {"synthetic": 0}}', attempts=2, becomes='{"nodes": {"synthetic": 0}}'
    )
    assert (passed, failed) == (0, 1), out
    assert "2 attempts" in out, out


def test_a_retrying_check_tallies_a_failure_rather_than_killing_the_suite(tmp_path):
    # The suite runs under `set -euo pipefail`. A retry loop that lets a failed
    # comparison escape aborts the run instead of tallying, and every check
    # after it silently never reports.
    passed, failed, out = _check_json_field(tmp_path, '{"nodes": {"synthetic": 0}}', attempts=3)
    assert (passed, failed) == (0, 1), out


def test_a_retrying_check_survives_an_unparseable_body(tmp_path):
    passed, failed, out = _check_json_field(tmp_path, "<html>404 not found</html>", attempts=2)
    assert (passed, failed) == (0, 1), out
