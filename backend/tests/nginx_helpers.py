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
    "HOST_APP": "app.example.com",
    "HOST_ADMIN": "admin.example.com",
    "CSP_CONNECT_SRC": "https://api.example.com",
}


def render(values: dict[str, str] | None = None) -> str:
    """Render with VALUES, or with `values` to exercise a flag (TLS_ENABLED=false)."""
    values = VALUES if values is None else values
    spec = importlib.util.spec_from_file_location("render_nginx_config", _RENDERER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    flags = module.resolve_flags(values)
    text = module.expand_includes(_TEMPLATE, _TEMPLATE.parent, flags)
    return module.substitute(text, values)


def locations(text: str) -> list[tuple[str, str]]:
    """(header, body) for every `location ... { ... }`, innermost braces only.

    A block containing another one is invisible here — the pattern stops at the
    first closing brace — so a nested location is reached with block() below
    instead.
    """
    return [(m.group(1), m.group(2)) for m in re.finditer(r"(location[^\n{]*)\{([^{}]*)\}", text)]


def block(text: str, opener: str) -> str:
    """The whole `{ ... }` block opened by the line `opener`, nesting included."""
    start = text.index(opener)
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise AssertionError(f"unbalanced braces after {opener!r}")
