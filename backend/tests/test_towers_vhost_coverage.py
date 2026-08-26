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
_ROLE_TO_SMOKE_VAR = {
    "HOST_MAIN": "BASE_URL",
    "HOST_MAP": "MAP_URL",
    "HOST_TESTMAP": "TESTMAP_URL",
    "HOST_API": "API_URL",
}


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
