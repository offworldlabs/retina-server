"""Every vhost routed to tower-finder-service must be probed by the smoke test.

The defect this guards against is one vhost silently missing the proxy, and a
smoke test that probes a subset cannot see it. The two lists are maintained by
hand in different files and different languages, so the invariant is asserted
here rather than left to a comment.
"""

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_TEMPLATE = _REPO / "deploy" / "nginx" / "nginx.conf.template"
_SMOKE = _REPO / "deploy" / "staging-smoke-test.sh"

# Hostname role -> the staging URL variable the smoke test probes it as.
#
# HOST_DASH and HOST_ADMIN joined the list when the tower routes were
# deduplicated: their `location /api/` used to fall through to the monolith's
# own copy of the tower stack, which had already diverged from the service's
# (one Phoenix query, KAET 183.0 MHz on the tower vhosts and KSAZ-TV 195.0 MHz
# on dash). That copy has since been deleted. They serve dashboard/dist and have
# no tower search UI, so they are here for the routes rather than for a screen.
_ROLE_TO_SMOKE_VAR = {
    "HOST_MAIN": "BASE_URL",
    "HOST_MAP": "MAP_URL",
    "HOST_TESTMAP": "TESTMAP_URL",
    "HOST_API": "API_URL",
    "HOST_DASH": "DASH_URL",
    "HOST_ADMIN": "ADMIN_URL",
}

# The paths towers-proxy.conf hands to the service. Every vhost that includes it
# must forward all three. This used to guard against a split-brain (each route
# answered by whichever implementation the vhost happened to reach); now that the
# monolith's copy is deleted, a route missing from the snippet is a 404 on that
# vhost instead — louder, but still only visible in production.
_PROXIED_PATHS = ("/api/towers", "/api/elevation", "/api/config")


def _server_blocks(text: str) -> list[str]:
    """Split the template on its top-level `server {` blocks."""
    return ["server {" + part for part in text.split("\nserver {")[1:]]


@pytest.fixture(scope="module")
def routed_roles() -> set[str]:
    """The HOST_* roles whose vhost forwards to tower-finder-service."""
    text = _TEMPLATE.read_text()
    roles = set()
    for block in _server_blocks(text):
        # Either the shared /api/towers include, or the api vhost's own /towers.
        if "towers-proxy.conf" not in block and "tower-finder.conf" not in block:
            continue
        names = re.search(r"server_name\s+([^;]+);", block)
        if not names:
            continue
        for role in re.findall(r"\$\{(HOST_[A-Z_]+)\}", names.group(1)):
            roles.add(role)
    return roles


def test_the_template_routes_the_roles_we_think_it_does(routed_roles):
    """A new routed vhost must be added to _ROLE_TO_SMOKE_VAR and the smoke test.

    HOST_LEGACY_REDIRECT is the fleet's own name for the service, proxied whole
    rather than at /api/towers, and is not part of the SPA seam.
    """
    assert routed_roles - {"HOST_LEGACY_REDIRECT"} == set(_ROLE_TO_SMOKE_VAR)


def test_the_smoke_test_probes_every_routed_vhost(routed_roles):
    probed = set(re.findall(r"\$\{([A-Z_]+_URL)\}/(?:api/)?towers", _SMOKE.read_text()))
    expected = {_ROLE_TO_SMOKE_VAR[role] for role in routed_roles if role in _ROLE_TO_SMOKE_VAR}
    missing = expected - probed
    assert not missing, f"routed but never probed by the staging smoke test: {sorted(missing)}"


def test_the_smoke_test_defines_every_url_it_is_expected_to_probe():
    """A probe written against an unset variable expands to a bare path.

    `set -u` would catch it, but only at the line — after every check above it
    has already reported. Assert the definitions exist instead.
    """
    smoke = _SMOKE.read_text()
    undefined = [var for var in _ROLE_TO_SMOKE_VAR.values() if not re.search(rf"^{var}=", smoke, re.M)]
    assert not undefined, f"probed by _ROLE_TO_SMOKE_VAR but never assigned in the smoke test: {undefined}"


def test_the_shared_snippet_proxies_every_deduplicated_path():
    """One vhost include must carry the whole tower stack, not just the search.

    /api/elevation and /api/config have no implementation left in this repo, so a
    route dropped from this snippet is not served at all on that vhost: the
    request falls through `location /` to an app that no longer has the handler
    and answers 404.
    """
    snippet = (_TEMPLATE.parent / "snippets" / "towers-proxy.conf").read_text()
    locations = set(re.findall(r"^location\s+(\S+)\s*\{", snippet, re.M))
    missing = set(_PROXIED_PATHS) - locations
    assert not missing, f"towers-proxy.conf does not proxy: {sorted(missing)}"


def test_no_exact_match_location_outranks_the_proxied_paths():
    """`location = /api/config` anywhere would beat this prefix and take the route back.

    Prefix locations are ranked by length, so the /api/ fallback loses to these
    — but an exact match outranks every prefix regardless of length, and would
    take the route off the service on that one vhost. With the monolith's copy
    deleted there is nothing behind it to answer, so the result is a 404 rather
    than a stale ranking.
    """
    text = _TEMPLATE.read_text()
    for path in _PROXIED_PATHS:
        assert not re.search(rf"location\s*=\s*{re.escape(path)}\b", text), (
            f"an exact-match `location = {path}` outranks the towers-proxy prefix"
        )
