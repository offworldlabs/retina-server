"""Both smoke suites must take their tally from deploy/smoke-tally.sh.

The suites are separate implementations in different files, and the counters
were the part that silently diverged: a warned check counted as a pass in one
and as its own figure in the other, so the same outage read differently per
environment. Sourcing the shared file is what stops that, and a source line is
easy to drop without anything failing, so the coupling is asserted here rather
than left to a comment.
"""

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_TALLY = _REPO / "deploy" / "smoke-tally.sh"
_STAGING = _REPO / "deploy" / "staging-smoke-test.sh"
_CI = _REPO / ".github" / "workflows" / "ci.yml"

# The production suite lives inline in the workflow rather than in its own file.
_SUITES = {"staging-smoke-test.sh": _STAGING, "ci.yml (production smoke)": _CI}


@pytest.fixture(scope="module")
def tally_text() -> str:
    return _TALLY.read_text()


@pytest.mark.parametrize("name", sorted(_SUITES))
def test_each_suite_sources_the_shared_tally(name: str) -> None:
    # A `source` command, not the substring: `# shellcheck source=...` names the
    # file too and would satisfy a looser check on its own.
    # `.*` because staging resolves its own directory first, spaces and all.
    sourced = re.search(r"^\s*(?:source|\.)\s+.*smoke-tally\.sh", _SUITES[name].read_text(), re.M)
    assert sourced, (
        f"{name} no longer sources deploy/smoke-tally.sh, so its counters and "
        "summary are its own again and can drift from the other suite's."
    )


@pytest.mark.parametrize("name", sorted(_SUITES))
def test_no_suite_declares_its_own_counters(name: str) -> None:
    # An assignment to zero is a redeclaration; `PASS=$((PASS+1))` is a use.
    own = re.findall(r"^\s*(?:PASS|FAIL|WARN)=0\b", _SUITES[name].read_text(), re.M)
    assert not own, (
        f"{name} initialises {own} itself. deploy/smoke-tally.sh owns the "
        "counters; a local copy shadows it and reintroduces the divergence."
    )


def test_the_tally_owns_all_three_counters(tally_text: str) -> None:
    for counter in ("PASS", "FAIL", "WARN"):
        assert re.search(rf"^{counter}=0$", tally_text, re.M), (
            f"{counter} is no longer initialised in deploy/smoke-tally.sh, so "
            "whichever suite relies on it starts from an unset variable."
        )


def test_the_summary_takes_no_arguments(tally_text: str) -> None:
    body = tally_text.split("smoke_summary() {", 1)[-1]
    assert "$1" not in body, (
        "smoke_summary reads a positional argument again. A caller that can "
        "vary the format can make the two suites report differently, which is "
        "what sharing the function exists to prevent."
    )
