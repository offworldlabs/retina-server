"""The week-long immutable cache is confined to Vite's hashed /assets/ tree.

Cloudflare keeps a `public, immutable` response for the whole `expires`
window, so the policy is only safe on a name that carries a content hash.
Every other static file is served `no-store`, like index.html, or a deploy
that changes it stays invisible until the window ends, under an index.html
that already expects the new file. `no-cache` alone is not enough: Cloudflare
revalidates at the edge but rewrites the browser-facing header to the zone's
4 h Browser Cache TTL.

Asserted on the RENDERED config: the locations arrive through spa.conf, and
nginx takes the first regex location that matches, so the order the include
lays them out in is the behaviour.
"""

from __future__ import annotations

import pytest

from tests.nginx_helpers import locations, render

# The extension list both static-file locations share.
_STATIC = r"\.(js|css|"


@pytest.fixture(scope="module")
def rendered() -> str:
    return render()


def test_immutable_is_confined_to_hashed_assets(rendered):
    for header, body in locations(rendered):
        if "immutable" in body:
            assert "^/assets/" in header, f"{header.strip()} is immutable but its names carry no hash"


def test_every_other_static_file_is_revalidated(rendered):
    statics = [(h, b) for h, b in locations(rendered) if _STATIC in h]
    assert statics, "no static-file locations rendered"
    # In pairs, one per SPA vhost: the /assets/ location first, then the
    # catch-all. A catch-all that came first would take /assets/ too and drop
    # the week.
    assert len(statics) % 2 == 0, [h.strip() for h, _ in statics]
    for assets, rest in zip(statics[0::2], statics[1::2], strict=True):
        assert "^/assets/" in assets[0] and "immutable" in assets[1], assets[0].strip()
        assert "^/assets/" not in rest[0] and "no-store" in rest[1], rest[0].strip()
