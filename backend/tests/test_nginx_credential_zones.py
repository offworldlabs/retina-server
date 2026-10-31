"""Which rate-limit zone the credential endpoints land in.

Nothing else asserts this. The only live check is deploy/staging-smoke-test.sh,
which runs after merge, so a credential endpoint silently demoted to the general
`api` zone (30r/s) reaches production before anything notices. These are cheap
and they run on every PR.
"""

import re

import pytest

from tests.nginx_helpers import VALUES, block, locations, render

#: Every vhost that carries a `location /api/auth/` at all: HOST_MAIN, HOST_API
#: and HOST_APP. HOST_ADMIN has only a blanket `location /api/` (the HTTP
#: redirect and the default server have no locations), which is why
#: routes/auth.py does not rely on nginx alone to bound the sign-in endpoints.
_CREDENTIAL_PREFIX = "/api/auth/"

#: The owner's archive: under /api/auth/me, but on the public archive's limits.
_OWNER_ARCHIVE = "/api/auth/me/archive"


def _bodies(rendered: str, prefix: str) -> list[str]:
    return [body for header, body in locations(rendered) if header.split()[-1] == prefix]


def _servers(rendered: str) -> list[str]:
    """Every top-level `server { ... }` block, in order."""
    found, start = [], 0
    while (at := rendered.find("\nserver {", start)) != -1:
        server = block(rendered[at + 1 :], "server {")
        found.append(server)
        start = at + 1 + len(server)
    return found


def _auth_location_bodies() -> list[str]:
    rendered = render()
    return [
        body
        for header, body in locations(rendered)
        if re.fullmatch(rf"location\s+{re.escape(_CREDENTIAL_PREFIX)}\s*", header)
    ]


def test_the_credential_prefix_is_present_on_three_vhosts():
    """Guards the count itself: a vhost gaining or losing the block changes what
    the assertions below are actually covering."""
    assert len(_auth_location_bodies()) == 3


def test_every_credential_location_uses_the_tight_zone():
    bodies = _auth_location_bodies()
    assert bodies
    for body in bodies:
        assert "zone=auth" in body, body


def test_no_credential_location_falls_into_the_general_api_zone():
    for body in _auth_location_bodies():
        assert "zone=api" not in body, body


def test_the_magic_link_endpoints_inherit_the_credential_prefix():
    """POST /api/auth/magic-link and its consume endpoint are matched by the
    `/api/auth/` prefix, so they need no location of their own. A longer-prefix
    location for either would take them out of the `auth` zone silently, which
    is what this catches."""
    rendered = render()
    longer = [header for header, _ in locations(rendered) if "magic-link" in header]
    assert longer == [], f"magic-link has its own location(s), check the zone: {longer}"


def test_the_session_endpoints_are_still_the_only_ones_excused():
    """/api/auth/me and /logout are hit twice by a single page load and use the
    looser `session` zone deliberately; the owner's archive under the first
    uses the general `api` zone, as the public archive does. Anything else
    appearing here is a credential endpoint that has quietly escaped the tight
    limit."""
    rendered = render()
    excused = {
        header.split()[-1]
        for header, body in locations(rendered)
        if header.split()[-1].startswith(_CREDENTIAL_PREFIX)
        and header.split()[-1] != _CREDENTIAL_PREFIX
        and "zone=auth" not in body
    }
    assert excused == {
        "/api/auth/me",
        "/api/auth/logout",
        _OWNER_ARCHIVE,
    }, excused


def test_the_session_reads_use_the_session_zone():
    rendered = render()
    for prefix in ("/api/auth/me", "/api/auth/logout"):
        bodies = _bodies(rendered, prefix)
        assert len(bodies) == 3, prefix
        for body in bodies:
            assert "zone=session" in body, body


def test_the_owners_archive_gets_what_the_public_archive_gets():
    """A Data Explorer scan pages through it and a download can be large, so the
    session zone's 5r/s and 30 s would refuse what /api/data/archive serves.
    The body is the rendered `location /api/` one, the public archive's."""
    rendered = render()
    public = set(_bodies(rendered, "/api/"))
    assert public
    bodies = _bodies(rendered, _OWNER_ARCHIVE)
    assert len(bodies) == 3
    for body in bodies:
        assert body in public, body
        assert "zone=api" in body and "proxy_read_timeout 120s;" in body, body


@pytest.mark.parametrize("tls", ["true", "false"])
def test_the_owners_archive_sits_beside_every_session_location(tls):
    """On a vhost with /api/auth/me and no archive location, the archive would
    fall back to the session zone there alone. Both renders, since TLS decides
    how many server blocks there are."""
    rendered = render({**VALUES, "TLS_ENABLED": tls})
    servers = _servers(rendered)
    # Every block the render opens, so none is left out of the check below.
    assert len(servers) == len(re.findall(r"^server \{", rendered, re.MULTILINE))
    with_session = 0
    for server in servers:
        prefixes = {header.split()[-1] for header, _ in locations(server)}
        assert ("/api/auth/me" in prefixes) == (_OWNER_ARCHIVE in prefixes), server.splitlines()[:6]
        with_session += "/api/auth/me" in prefixes
    assert with_session == 3
