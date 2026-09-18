"""Every redirect nginx issues must leave the host and port to the browser.

With nginx's default `absolute_redirect on`, `return 301 /path/` goes out as an
absolute URL built from the listen port. The laptop stack listens on 80 inside
the container and is published on 8080, so each such redirect would send the
browser to port 80, where nothing answers. The droplets listen on 443 and hide
the problem.

Asserted on the RENDERED config, in both its TLS and plain-HTTP forms, since
only the render shows which block a directive landed in.
"""

from __future__ import annotations

import re

import pytest

from tests.nginx_helpers import VALUES, render


@pytest.fixture(scope="module", params=["tls", "plain"])
def rendered(request) -> str:
    if request.param == "plain":
        return render({**VALUES, "TLS_ENABLED": "false"})
    return render()


def _directives(text: str) -> list[tuple[int, str]]:
    """(brace depth, line) for every non-comment line; depth 0 is http {}."""
    out, depth = [], 0
    for line in text.splitlines():
        code = re.sub(r"(?:^|\s)#.*", "", line).strip()
        if code:
            out.append((depth, code))
        depth += code.count("{") - code.count("}")
    assert depth == 0, "unbalanced braces in the rendered config"
    return out


def test_redirects_are_relative_on_every_vhost(rendered):
    """At http level, so a vhost added later is covered without remembering to."""
    assert (0, "absolute_redirect off;") in _directives(rendered)


def test_no_block_turns_absolute_redirects_back_on(rendered):
    """A server or location setting it again would override the http-level one."""
    offenders = [
        line for _, line in _directives(rendered) if line.startswith("absolute_redirect") and "off" not in line
    ]
    assert not offenders, f"absolute_redirect re-enabled: {offenders}"
