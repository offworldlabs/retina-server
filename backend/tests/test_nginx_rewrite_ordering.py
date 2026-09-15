"""No `rewrite ... break` may precede a `set` in the same rendered location.

`break` stops every remaining ngx_http_rewrite_module directive in its
location, and `set` is one of them. A location that rewrites before it sets
$tfs_upstream therefore proxies to `http://:8000` and answers 500 ("no host in
upstream"), while nginx -t and every other vhost stay perfectly happy.

That shipped once: the api vhost's `location = /towers` had the rewrite first,
so staging-api.retina.fm/towers 500'd while the three /api/towers vhosts were
fine. The staging smoke test caught it after the merge; this catches it before.

Asserted on the RENDERED config, not the template: the `set` reaches the
location through an include, so the ordering only exists after expansion.
"""

from __future__ import annotations

import re

import pytest

from tests.nginx_helpers import locations as _locations
from tests.nginx_helpers import render


@pytest.fixture(scope="module")
def rendered() -> str:
    return render()


def test_the_template_still_has_locations_to_check(rendered):
    """A rewrite of the config that broke this parse would silently pass below."""
    assert len(_locations(rendered)) > 5


def test_no_break_before_a_set(rendered):
    offenders = []
    for header, body in _locations(rendered):
        broke_at = None
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if broke_at is None and re.match(r"^rewrite\s+.*\bbreak\s*;", stripped):
                broke_at = stripped
            elif broke_at is not None and stripped.startswith("set "):
                offenders.append(f"{header.strip()}: {stripped!r} is dead, skipped by {broke_at!r}")
    assert not offenders, "rewrite ... break precedes a set, which nginx will skip:\n" + "\n".join(offenders)


def test_every_proxy_pass_variable_is_assigned_in_its_location(rendered):
    """The failure mode the ordering rule exists to prevent, stated directly."""
    offenders = []
    for header, body in _locations(rendered):
        for var in re.findall(r"proxy_pass\s+https?://\$([A-Za-z_][A-Za-z0-9_]*)", body):
            live = []
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if re.match(r"^rewrite\s+.*\bbreak\s*;", stripped):
                    break
                live.append(stripped)
            if not any(re.match(rf"^set\s+\${var}\b", line) for line in live):
                offenders.append(f"{header.strip()}: proxy_pass uses ${var}, never set (or set after a break)")
    assert not offenders, "\n".join(offenders)
