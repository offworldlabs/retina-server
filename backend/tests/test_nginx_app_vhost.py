"""The app vhost serves the console at its root.

Asserted on the RENDERED config: `location /` and the regex cache locations
arrive through spa.conf, so location precedence only exists after expansion.
"""

from __future__ import annotations

import re

import pytest

from tests.nginx_helpers import REPO, VALUES, locations, render


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


def test_data_is_the_consoles_own_page(app_vhost):
    """An exact `location = /data` outranks everything and would bounce the
    console's own Data Explorer on every direct load, reload and shared link."""
    assert "location = /data" not in app_vhost


def test_no_vhost_serves_the_deleted_map():
    """frontend/ is gone, so a root or alias into its dist is a 404 site."""
    assert "/app/frontend/dist" not in render()


def test_websocket_locations_log_without_the_query_string():
    """API keys travel as `?apikey=` on websocket URLs; the format they log with
    must carry the path but none of the variables that hold the query."""
    rendered = render()
    assert rendered.count("location /ws/ {") == 4
    for header, body in locations(rendered):
        if header.strip() == "location /ws/":
            assert "access_log /var/log/nginx/access.log path_only;" in body

    security = (REPO / "deploy" / "nginx-security.conf").read_text()
    fmt = re.search(r"log_format path_only ([^;]*);", security).group(1)
    assert '"$request_method $uri $server_protocol"' in fmt
    assert not re.search(r"\$request\b(?!_)|\$request_uri|\$args|\$arg_|\$query_string|\$is_args", fmt)
