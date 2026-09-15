"""Render deploy/nginx/nginx.conf.template the way start.sh does, for tests
that assert on the result rather than on the template.

The include markers and flag blocks only exist before expansion, so anything
about location order or a directive reaching a location through a snippet is
only observable on the rendered text.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
_RENDERER = REPO / "deploy" / "render-nginx-config.py"
_TEMPLATE = REPO / "deploy" / "nginx" / "nginx.conf.template"

# Any deployed environment renders the same directives; only names differ.
VALUES = {
    "HOST_MAIN": "towers.example.com",
    "HOST_API": "api.example.com",
    "HOST_MAP": "map.example.com",
    "HOST_DASH": "dash.example.com",
    "HOST_ADMIN": "admin.example.com",
    "HOST_DATA": "data.example.com",
    "HOST_TESTMAP": "testmap.example.com",
    "HOST_LEGACY_REDIRECT": "tower-finder.example.com",
    "CSP_CONNECT_SRC": "https://api.example.com",
}


def render() -> str:
    spec = importlib.util.spec_from_file_location("render_nginx_config", _RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    flags = module.resolve_flags(VALUES)
    text = module.expand_includes(_TEMPLATE, _TEMPLATE.parent, flags)
    return module.substitute(text, VALUES)


def locations(text: str) -> list[tuple[str, str]]:
    """(header, body) for every `location ... { ... }`, innermost braces only.

    The template nests no locations, so a non-greedy match to the first closing
    brace is the whole body.
    """
    return [(m.group(1), m.group(2)) for m in re.finditer(r"(location[^\n{]*)\{([^{}]*)\}", text)]
