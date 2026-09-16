"""Every response from this origin must say it came from this origin.

A smoke probe asserts a hostname answered, never which service answered it. On
2026-09-14 a Cloudflare Origin Rule moved towers.retina.fm to tower-finder-service
and the probes kept passing against a different origin. Both services return the
same status, and on /api/health the same CSP, HSTS, X-Frame-Options and
X-Content-Type-Options — so nothing in a response distinguished them.

The marker header is what distinguishes them. These tests pin the two halves that
have to agree: the value nginx sends, and the value the smoke suites look for.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from tests.nginx_helpers import render

_REPO = Path(__file__).resolve().parents[2]
_SNIPPET = _REPO / "deploy" / "nginx" / "snippets" / "origin-marker.conf"
_SHARED = _REPO / "deploy" / "origin-marker.sh"
_STAGING = _REPO / "deploy" / "staging-smoke-test.sh"
_CI = _REPO / ".github" / "workflows" / "ci.yml"


def _production_smoke() -> str:
    """The production-smoke-tests job alone, not the whole 1200-line workflow.

    Scoped deliberately. Matching against the entire file would let any of these
    strings survive in an unrelated job, or in a comment, and go on satisfying
    the assertions below after the production job had quietly lost its origin
    check — the silent regression this file exists to prevent, one job over.
    """
    return yaml.safe_dump(yaml.safe_load(_CI.read_text())["jobs"]["production-smoke-tests"])


# The production suite lives inline in the workflow rather than in its own file.
_SUITES = {
    "staging-smoke-test.sh": lambda: _STAGING.read_text(),
    "ci.yml (production smoke)": _production_smoke,
}

_HEADER = "X-Retina-Origin"


@pytest.fixture(scope="module")
def rendered() -> str:
    return render()


def _tls_blocks(text: str) -> list[str]:
    blocks = []
    for part in text.split("\nserver {")[1:]:
        end = re.search(r"^\}", part, re.MULTILINE)
        assert end, f"unterminated server block: {part[:80]!r}"
        block = "server {" + part[: end.end()]
        if "listen 443 ssl" in block:
            blocks.append(block)
    return blocks


def test_every_tls_vhost_sends_the_marker(rendered):
    """Including the catch-all: a 421 is ours too, and says so."""
    blocks = _tls_blocks(rendered)
    assert blocks, "no TLS server blocks rendered"
    missing = [b.split("\n")[1].strip() for b in blocks if _HEADER not in b]
    assert not missing, f"TLS vhosts without the marker header: {missing}"


def test_the_marker_is_set_with_always(rendered):
    """Without `always` nginx omits it on the catch-all's 421 and on every error."""
    for match in re.finditer(rf"add_header\s+{_HEADER}\s+[^;]+;", rendered):
        assert "always" in match.group(0), match.group(0)


def test_the_marker_value_is_a_literal(rendered):
    """A ${...} placeholder would need registering in SUBSTITUTIONS, HOST_VARS and
    every compose overlay, and the parity check needs the renders byte-identical
    across environments anyway."""
    assert "${" not in _SNIPPET.read_text()


def test_the_shell_and_the_config_agree_on_the_marker(rendered):
    """The half that sends it and the half that looks for it, pinned together.

    They live in different languages in different directories; drift between
    them turns every marker probe into an assertion about nothing.
    """
    sent = re.search(rf'add_header\s+{_HEADER}\s+"([^"]+)"', rendered)
    assert sent, "the rendered config sets no marker header"
    expected = re.search(r'^RETINA_ORIGIN_VALUE="([^"]+)"', _SHARED.read_text(), re.M)
    assert expected, "deploy/origin-marker.sh defines no RETINA_ORIGIN_VALUE"
    assert sent.group(1) == expected.group(1), (
        f"nginx sends {sent.group(1)!r} but the smoke suites look for {expected.group(1)!r}."
    )


def test_the_shell_and_the_config_agree_on_the_header_name(rendered):
    name = re.search(r'^RETINA_ORIGIN_HEADER="([^"]+)"', _SHARED.read_text(), re.M)
    assert name, "deploy/origin-marker.sh defines no RETINA_ORIGIN_HEADER"
    # curl's header matching downcases; the config's spelling is the canonical one.
    assert name.group(1) == _HEADER.lower()


def test_the_shell_side_is_lowercase(rendered):
    """assert_origin_marker downcases the response before matching.

    Agreement between the two halves is not enough on its own: an uppercase
    letter in either would satisfy every test above and still match nothing at
    runtime, and on production a marker probe that cannot match rolls the
    deploy back.
    """
    text = _SHARED.read_text()
    for var in ("RETINA_ORIGIN_HEADER", "RETINA_ORIGIN_VALUE"):
        found = re.search(rf'^{var}="([^"]+)"', text, re.M)
        assert found, f"deploy/origin-marker.sh defines no {var}"
        assert found.group(1) == found.group(1).lower(), (
            f"{var} is {found.group(1)!r}. The header block is downcased before "
            "matching, so an uppercase letter here matches nothing."
        )


@pytest.mark.parametrize("name", sorted(_SUITES))
def test_each_suite_sources_the_shared_marker(name: str) -> None:
    # A `source` command, not the substring: `# shellcheck source=...` names the
    # file too and would satisfy a looser check on its own. The whitespace after
    # `source` is what separates them. Not anchored to the line start, because
    # the production job arrives here as re-dumped YAML rather than as the file.
    sourced = re.search(r"(?:source|\.)\s+[^\n]*origin-marker\.sh", _SUITES[name]())
    assert sourced, (
        f"{name} no longer sources deploy/origin-marker.sh, so whatever it "
        "asserts about the origin is its own copy and can drift from the config."
    )


@pytest.mark.parametrize("name", sorted(_SUITES))
def test_no_suite_hardcodes_the_marker(name: str) -> None:
    """A literal in a suite keeps passing after the config's value changes.

    The header name only. The marker's *value* is this repo's own name, which
    appears throughout ci.yml for unrelated reasons (paths, image tags), so
    matching on it would fire on edits that have nothing to do with the marker.
    """
    own = re.findall(_HEADER, _SUITES[name](), re.I)
    assert not own, (
        f"{name} names the marker header itself ({own}). Use RETINA_ORIGIN_HEADER from deploy/origin-marker.sh."
    )


@pytest.mark.parametrize("name", sorted(_SUITES))
def test_each_suite_probes_the_marker(name: str) -> None:
    assert "assert_origin_marker" in _SUITES[name](), (
        f"{name} sources the shared marker but never asserts it, so a vhost "
        "flipped to another origin would still pass every probe."
    )
