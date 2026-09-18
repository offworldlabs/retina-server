"""Which rate-limit zone the credential endpoints land in.

Nothing else asserts this. The only live check is deploy/staging-smoke-test.sh,
which runs after merge, so a credential endpoint silently demoted to the general
`api` zone (30r/s) reaches production before anything notices. These are cheap
and they run on every PR.
"""

import re

from tests.nginx_helpers import locations, render

#: Every vhost that carries a `location /api/auth/` at all. The other six have
#: only a blanket `location /api/`, which is why routes/auth.py does not rely on
#: nginx alone to bound the sign-in endpoints.
_CREDENTIAL_PREFIX = "/api/auth/"


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
    looser `session` zone deliberately. Anything else appearing here is a
    credential endpoint that has quietly escaped the tight limit."""
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
    }, excused
