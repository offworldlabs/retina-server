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

import importlib.util
import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_RENDERER = _REPO / "deploy" / "render-nginx-config.py"
_TEMPLATE = _REPO / "deploy" / "nginx" / "nginx.conf.template"

# Any deployed environment renders the same directives; only names differ.
_VALUES = {
    "HOST_MAIN": "towers.example.com",
    "HOST_API": "api.example.com",
    "HOST_MAP": "map.example.com",
    "HOST_DASH": "dash.example.com",
    "HOST_ADMIN": "admin.example.com",
    "HOST_TESTMAP": "testmap.example.com",
    "HOST_LEGACY_REDIRECT": "tower-finder.example.com",
    "CSP_CONNECT_SRC": "https://api.example.com",
}


@pytest.fixture(scope="module")
def rendered() -> str:
    spec = importlib.util.spec_from_file_location("render_nginx_config", _RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    flags = module.resolve_flags(_VALUES)
    text = module.expand_includes(_TEMPLATE, _TEMPLATE.parent, flags)
    return module.substitute(text, _VALUES)


def _locations(text: str) -> list[tuple[str, str]]:
    """(header, body) for every `location ... { ... }`, innermost braces only.

    The template nests no locations, so a non-greedy match to the first closing
    brace is the whole body.
    """
    return [(m.group(1), m.group(2)) for m in re.finditer(r"(location[^\n{]*)\{([^{}]*)\}", text)]


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
