"""The app vhost serves the console at its root, and its old addresses redirect.

`/dash/` was the console's mount while the map had `/`, and links to it are
still in circulation, mailed sign-in links among them. Each must land on the
same page at the root with its query intact. The same goes for the standalone
data explorer's old `/data/` paths.

Asserted on the RENDERED config: `location /` and the regex cache locations
arrive through spa.conf, so location precedence only exists after expansion.
"""

from __future__ import annotations

import re

import pytest

from tests.nginx_helpers import VALUES, block, render


@pytest.fixture(scope="module")
def app_vhost() -> str:
    rendered = render()
    start = rendered.index(f"server_name {VALUES['HOST_APP']};")
    end = rendered.find("\nserver {", start)
    return rendered[start : end if end != -1 else len(rendered)]


def test_the_console_is_the_root(app_vhost):
    assert "root /app/dashboard/dist;" in app_vhost
    assert "root /app/frontend/dist" not in app_vhost


def test_nothing_is_served_from_the_old_mount(app_vhost):
    assert "dist-dash" not in app_vhost
    assert "alias" not in app_vhost


def test_the_old_mount_redirects_every_path_to_its_root_twin(app_vhost):
    """`rewrite ... permanent` keeps the query string, which a bare `return` drops.

    `^~` matters as it did when this was a mount: without it spa.conf's regex
    locations answer /dash/assets/*.js themselves instead of redirecting.
    """
    body = block(app_vhost, "location ^~ /dash/ {")
    assert re.search(r"rewrite\s+\^/dash/\(\.\*\)\$\s+/\$1\s+permanent;", body)


def test_the_slashless_mount_redirects_too(app_vhost):
    """`/dash` does not match `^~ /dash/`, and would otherwise fall through to
    the SPA, which has no such route."""
    body = block(app_vhost, "location = /dash {")
    assert "return 301 /$is_args$args;" in body


def test_data_is_the_consoles_own_page(app_vhost):
    """An exact `location = /data` outranks everything and would bounce the
    console's own Data Explorer on every direct load, reload and shared link."""
    assert "location = /data" not in app_vhost


def test_the_old_explorers_paths_land_on_the_page(app_vhost):
    """The query is the page's filters (dataExplorer/urlState.ts)."""
    body = block(app_vhost, "location ^~ /data/ {")
    assert "return 301 /data$is_args$args;" in body


def test_no_redirect_names_a_hostname(app_vhost):
    """deploy/check-env-parity.py maps each environment's HOST_* values back to
    a role token, so a literal hostname here would read as a parity failure."""
    for line in app_vhost.splitlines():
        if "return 301" in line or "rewrite" in line:
            assert "://" not in line, line
