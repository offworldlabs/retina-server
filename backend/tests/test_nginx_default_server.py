"""A hostname no vhost claims must be refused, not served by the first block.

nginx falls back to the first `server` block on a listen port when no
`server_name` matches. The template declared no `default_server`, so an
unmatched name was answered by the ${HOST_MAIN} tower SPA: a node calling
/api/towers on a typo'd or retired hostname got an HTML page with a 200 and
parsed it as JSON.

Asserted on the RENDERED config: the catch-all reaches its TLS directives
through an include, and the plain-HTTP variant only exists after expansion.
"""

from __future__ import annotations

import re

import pytest

from tests.nginx_helpers import VALUES, render


@pytest.fixture(scope="module")
def rendered() -> str:
    return render()


@pytest.fixture(scope="module")
def rendered_plain() -> str:
    """The laptop stack's render (TLS_ENABLED=false)."""
    return render({**VALUES, "TLS_ENABLED": "false"})


def _server_blocks(text: str) -> list[str]:
    """Each top-level `server { ... }`, cut at its own closing brace.

    Splitting on `server {` alone runs each block into the next one's leading
    comment, which carries a hostname and defeats the assertions below.
    """
    blocks = []
    for part in text.split("\nserver {")[1:]:
        end = re.search(r"^\}", part, re.MULTILINE)
        assert end, f"unterminated server block: {part[:80]!r}"
        blocks.append("server {" + part[: end.end()])
    return blocks


def _catchall(text: str) -> str:
    blocks = [b for b in _server_blocks(text) if "default_server" in b]
    assert len(blocks) == 1, f"expected exactly one default_server block, got {len(blocks)}"
    return blocks[0]


def test_exactly_one_vhost_is_the_declared_default(rendered):
    """Two default_server blocks on one port is an nginx -t error, not a warning."""
    assert len(re.findall(r"\bdefault_server\b", rendered)) == 1


def test_the_default_is_the_catch_all_and_not_a_named_vhost(rendered):
    """A named vhost holding default_server would re-create the original bug."""
    catchall = _catchall(rendered)
    assert re.search(r"^\s*server_name\s+_;", catchall, re.MULTILINE), catchall
    for host in VALUES.values():
        assert host not in catchall, f"catch-all must claim no real hostname, found {host}"


def test_the_catch_all_refuses_rather_than_serving_anything(rendered):
    """421: the authority is wrong, not the path. Nothing else may be reachable."""
    catchall = _catchall(rendered)
    assert re.search(r"^\s*return\s+421;", catchall, re.MULTILINE), catchall
    for directive in ("root ", "proxy_pass", "try_files", "alias "):
        assert directive not in catchall, f"catch-all must serve nothing, found {directive!r}"


def test_the_catch_all_still_demands_cloudflares_client_certificate(rendered):
    """It terminates TLS like any other vhost, so it needs the same origin boundary.

    Without this the catch-all would be the one block on the box that accepts a
    handshake from something that is not Cloudflare.
    """
    catchall = _catchall(rendered)
    assert "ssl_verify_client on;" in catchall, catchall
    assert "ssl_certificate " in catchall, catchall


def test_the_catch_all_precedes_every_named_vhost(rendered):
    """Belt and braces: it is also the positional default, so the two agree."""
    blocks = _server_blocks(rendered)
    tls_blocks = [b for b in blocks if "listen 443 ssl" in b]
    assert "default_server" in tls_blocks[0], "catch-all must be the first 443 block"


def test_the_laptop_stack_keeps_its_fall_through(rendered_plain):
    """`http://localhost:8080` is not a declared vhost and is reached by fall-back.

    The catch-all is gated on RETINA_IF TLS for this reason; an ungated one
    turns every bare-localhost request on the laptop into a 421.
    """
    assert "default_server" not in rendered_plain
    assert "return 421;" not in rendered_plain
