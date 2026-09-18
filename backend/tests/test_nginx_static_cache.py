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

import re

import pytest

from tests.nginx_helpers import block, locations, render

# The extension list both static-file locations share.
_STATIC = r"\.(js|css|"


@pytest.fixture(scope="module")
def rendered() -> str:
    return render()


def test_immutable_is_confined_to_hashed_assets(rendered):
    """Only a Vite `assets/` tree may be kept, wherever it is mounted.

    The app vhost serves the dashboard under /dash/, so its hashed tree is
    /dash/assets/ rather than /assets/ — still a content-hashed name, which is
    the property that makes the week safe.
    """
    for header, body in locations(rendered):
        if "immutable" in body:
            assert "/assets/" in header, f"{header.strip()} is immutable but its names carry no hash"


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


# The app vhost's bundle mounts. `^~` keeps spa.conf's regex pair off them,
# which is what makes /dash/assets/ reachable at all — and also what leaves
# them outside the pairing asserted above, so they are checked directly.
_PREFIXED_BUNDLES = ("location ^~ /dash/ {",)


def test_prefixed_bundles_revalidate_their_unhashed_files(rendered):
    """A name that survives a deploy must not be cached under a new index.html.

    The dashboard's theme-boot.js is exactly that: no content hash, so the
    edge would serve the old copy for a week if the mount inherited nothing and
    said nothing.
    """
    for opener in _PREFIXED_BUNDLES:
        body = block(rendered, opener)
        # Anchored at a line start: the word `location` also appears in the
        # comment above the nested block, and splitting on the bare substring
        # cuts there instead — passing for the wrong reason.
        before_nested = re.split(r"^\s*location\b", body[len(opener) :], maxsplit=1, flags=re.M)[0]
        assert "no-store" in before_nested, f"{opener} does not revalidate its unhashed files"
