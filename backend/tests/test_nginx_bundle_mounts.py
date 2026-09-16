"""A bundle mounted under a path prefix must behave like one served at a root.

The app vhost serves three bundles from one server block, which costs two
things a root-served bundle gets for free. Both failures are silent — a 200
carrying the wrong bytes — so neither shows up in a status check.

Asserted on the RENDERED config: `location /` and the regex cache locations
arrive through spa.conf, so the ranking these tests are about only exists
after expansion.
"""

from __future__ import annotations

import re

import pytest

from tests.nginx_helpers import render

# Prefix -> the root the mount aliases to.
_MOUNTS = {"/dash/": "/app/dashboard/dist/", "/data/": "/app/data-explorer/"}


@pytest.fixture(scope="module")
def rendered() -> str:
    return render()


def test_every_mount_stops_nginx_looking_at_the_regex_locations(rendered):
    """Without `^~`, spa.conf's regexes take the mount's own assets.

    A regex location outranks a prefix one whatever their lengths, and
    spa.conf's resolve against the vhost root (/app/frontend/dist). So
    /dash/assets/index-<hash>.js would be looked for in the MAP bundle and
    404, taking the dashboard's JS with it.
    """
    for prefix in _MOUNTS:
        assert re.search(rf"location\s+\^~\s+{re.escape(prefix)}\s*\{{", rendered), (
            f"{prefix} is not mounted with `^~`, so spa.conf's regex locations outrank it"
        )


def test_every_mount_redirects_its_slashless_form(rendered):
    """`/dash` does not match `location ^~ /dash/` — it lacks the trailing slash.

    Unredirected it falls through to spa.conf's `location /` and answers 200
    with the map bundle's index.html, so a user who types the obvious URL gets
    a working page that is the wrong application.
    """
    for prefix in _MOUNTS:
        slashless = prefix.rstrip("/")
        assert re.search(
            rf"location\s*=\s*{re.escape(slashless)}\s*\{{[^{{}}]*return\s+301\s+{re.escape(prefix)}", rendered
        ), f"no exact-match `location = {slashless}` redirecting to {prefix}"


def test_no_mount_names_a_hostname(rendered):
    """The redirects must stay relative.

    deploy/check-env-parity.py compares environments by substituting each
    one's HOST_* values back to a role token, so a literal hostname here would
    survive in the other environments' renders and read as a parity failure.
    """
    for prefix, root in _MOUNTS.items():
        assert f"return 301 {prefix}" in rendered, f"{prefix} redirect is not a relative target"
        assert f"alias {root}" in rendered, f"{prefix} does not alias {root}"
