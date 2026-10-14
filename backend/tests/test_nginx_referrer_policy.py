"""A console page's address must not ride along as the Referer of its requests.

The console's addresses carry a private node's ref (/nodes/<ref>, /data?node=<ref>).
Under `strict-origin-when-cross-origin` a same-origin request sends the whole
address as its Referer, so a call to a public route (and its access log) learns
which private node the owner was looking at. `strict-origin` sends the origin only.

Asserted on the RENDERED config: the policy reaches a vhost through
security-headers-page.conf, and `location = /index.html` (which serves every SPA
route through try_files) comes from spa.conf with its own add_header, which
nginx does not merge with the server-level ones.
"""

from __future__ import annotations

import re

import pytest

from tests.nginx_helpers import block, render

_POLICY = re.compile(r'add_header\s+Referrer-Policy\s+"([^"]*)"')


@pytest.fixture(scope="module")
def html_vhosts() -> dict[str, str]:
    """server_name -> block, for every vhost that serves the SPA's HTML."""
    rendered = render()
    vhosts = {}
    for part in rendered.split("\nserver {")[1:]:
        text = "server {" + part
        if "/index.html;" not in text:
            continue
        vhosts[re.search(r"server_name\s+([^;]+);", text).group(1)] = text
    return vhosts


def test_every_html_vhost_is_covered(html_vhosts):
    assert sorted(html_vhosts) == ["admin.example.com", "app.example.com", "towers.example.com"]


def test_every_html_vhost_sends_strict_origin(html_vhosts):
    for name, text in html_vhosts.items():
        assert _POLICY.findall(text) == ["strict-origin", "strict-origin"], name


def test_the_entry_page_sends_strict_origin_itself(html_vhosts):
    """Every SPA route is answered by `location = /index.html`; its add_header
    replaces the server-level ones, so the policy has to be set in it too."""
    for name, text in html_vhosts.items():
        entry = block(text, "location = /index.html")
        assert _POLICY.findall(entry) == ["strict-origin"], name
        assert "always" in re.search(r"add_header\s+Referrer-Policy[^;]*;", entry).group(0), name


def test_no_html_vhost_sends_the_default_policy(html_vhosts):
    for name, text in html_vhosts.items():
        assert "strict-origin-when-cross-origin" not in text, name
