"""The app vhost serves the console at its root.

Asserted on the RENDERED config: `location /` and the regex cache locations
arrive through spa.conf, so location precedence only exists after expansion.
"""

from __future__ import annotations

import pytest

from tests.nginx_helpers import VALUES, render


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
