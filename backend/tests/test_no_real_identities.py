"""No real node identity, hostname, name or position may live in the tracked tree.

This repo is public and the API publishes nodes under an opaque ``node_ref``
at a position displaced by the fuzz in ``services/public_location.py``. A real
``ret…`` id, a ``*.retnode.com`` hostname, a real site name or a true receiver
coordinate in a tracked file undoes that at the source, so the ban is a test
rather than a review habit.

Identities and hostnames are matched by shape, since a real one has one. Names
and coordinates have no shape that distinguishes them from an invented one, so
they are matched against digests: writing the banned values in here would be
the disclosure the file exists to prevent. The digests cannot be read back, so
a value in doubt is checked by running the test. To ban a further value, hash
it as the scan does and add the digest to the matching set below::

    python3 -c 'import hashlib,sys;print(hashlib.sha256(sys.argv[1].encode()).hexdigest())' 'some value'

A name is hashed lower-cased and stripped of punctuation, so a site called
"Some Place-2" is banned by the digests of ``some`` and ``place``. A
coordinate is hashed unsigned, as the bare decimal literal with trailing zeros
and any trailing point removed, so ``-12.3400`` is banned by the digest of
``12.34``.

The coordinate ban covers the true position and every truncation of it that
still lands inside the published fuzz annulus. That annulus is the standard: a
fixture further out than the fuzz radius discloses less than the public feed
already does, and 1 decimal place is always far enough, which is why only
literals carrying two or more are examined. Two decimals is the coarsest rung
inside the annulus and the one that collides: the tree holds hundreds of
distinct two-decimal literals, and one of them appearing in a minified bundle
or an SVG path is not a position. The two-decimal rungs therefore fail only
together, one per axis, which is the shape a disclosed position has; three
decimals and finer fail on their own.

Submodule contents are not scanned. They are separate repositories, absent
entirely from a non-recursive clone, and a failure raised here against one of
them could not be fixed here. ``UNSCANNED_SUBMODULES`` names what is left
uncovered and the test asserts that list is still complete, so the gap cannot
widen unnoticed. Every other tracked path must be readable: one the scan
cannot open fails, and is never passed over.

The allow-list is the set of ids invented for documentation and fixtures.
Adding to it is how a new synthetic id becomes legal; a real one must never be
added.
"""

import hashlib
import re
import subprocess
from pathlib import Path

# ret<8 hex> ids, and the node hostnames of the whole retnode.com family in
# either the dotted or the hyphenated form.
REAL = re.compile(r"ret[0-9a-f]{8}|[a-z0-9][a-z0-9-]*[-.]retnode(?:\.com)?", re.IGNORECASE)
ALLOWED = {
    "ret1a2b3c4d",
    "ret00000000",
    "ret9f8e7d6c",
    "retdeadbeef",
    "retffffffff",
    "ret2b3c4d5e",
    "retc0ffee00",
    "ret0badcafe",
    "ret000000ff",
    "retdeadbee2",
    "ret0123abcd",
    # The dashboard's worked example of a node's own tunnel hostname.
    "your-node.retnode.com",
}

# Decimal literals of two or more places, the precision at which a coordinate
# can sit inside the fuzz annulus.
NUMBER = re.compile(r"\d{1,3}\.\d{2,}")
# Three decimals or better: ~110 m, so one literal is the position on its own.
BANNED_COORDS = {
    "7862f123bfbcb375a099a2b5d04973d0fd2958249c02de3c2b18d86c7a8fed6e",
    "bf9125e82d94352415cbd1f5b94e4b0e94453a1ab310d52b929e276426807f89",
    "7eadf42c729d4488ec977fe7ee3132d19b678742c8525c24c5a326b1b0669907",
    "2e37706226e894d4d457071fdd25006dfb3d8535e582103a9a9e54de1f3d9810",
    "056ef01068ba443c0b768f4d1c9ae043773eb696951c3f4b1130d32fee1bc69d",
    "585ab776fb5e716de08712e5c5d7b6da5980434f35f98d66107bc28c3e47bb8f",
    "e45fdbea330db87bce6eb854e1b005aabd8f046524fcabdad55b1fdc9e85f6ea",
    "5ba69d3a93f6453c9f1e1fcef27375dfd584811c2e12615be0bfae843de2678d",
    "703d99b9617c42b5ba0d55bde89396b7c3ce327c64e5641386026a5bf74834cd",
}
# The two-decimal rungs of the same position, one per axis. A file must carry
# both to be an offender; either alone is a collision, not a disclosure.
BANNED_COORD_AXES = {
    "0d528a55b6c1ab0aa25c23195b0e441aebb4baa1ff4fed8c442b9672dbd79021",
    "94044db8bede5b113e989fbf278da00d7f6d7bcf94e937c2d1e4fb2169e41111",
}

# Words, so a site or node display name is caught in whatever punctuation it
# is written with.
WORD = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")
BANNED_NAMES = {
    "a954e0efadae9e5aa74f5b29b397f8de9308627a171c780d30852f5c437d86c0",
    "bdd97b1ee2b7e4b92a565f066dca695df4de675d2a9c96ce0630010e0a158b50",
    "64d7e95f25fafcee5add1c9c62e15b61a48942db9fed91fe929f699b14cac590",
}

# Tracked gitlinks, whose contents this guard does not read. Each is its own
# public repository and answers for its own tree.
UNSCANNED_SUBMODULES = {
    "libs/retina-analytics",
    "libs/retina-custody",
    "libs/retina-geolocator",
    "libs/retina-simulation",
    "libs/retina-tracker",
}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _tracked_entries(root: Path) -> tuple[list[str], set[str]]:
    """Tracked blobs and tracked gitlinks, kept apart: a gitlink is a directory
    on disk, so left in the file list it would read as an unopenable path.
    check=True so a broken listing fails rather than passing an empty scan;
    -z so a path needing quoting is still returned verbatim."""
    out = subprocess.run(["git", "ls-files", "-sz"], cwd=root, capture_output=True, text=True, check=True)
    blobs: list[str] = []
    gitlinks: set[str] = set()
    for record in out.stdout.split("\0"):
        if not record:
            continue
        meta, _, rel = record.partition("\t")
        if meta.split()[0] == "160000":
            gitlinks.add(rel)
        else:
            blobs.append(rel)
    return blobs, gitlinks


def test_no_real_node_identity_is_tracked():
    root = Path(__file__).resolve().parents[2]
    files, gitlinks = _tracked_entries(root)
    assert files, "git ls-files listed nothing: the scan would pass without reading a file"
    assert gitlinks == UNSCANNED_SUBMODULES, (
        "the submodules this guard leaves unscanned have changed, so what it does not "
        "cover is no longer what it says it does not cover: "
        f"{sorted(gitlinks ^ UNSCANNED_SUBMODULES)}"
    )
    offenders: list[str] = []
    unreadable: list[str] = []
    for rel in files:
        p = root / rel
        try:
            text = p.read_text(errors="ignore")
        except OSError as exc:
            unreadable.append(f"{rel}: {exc}")
            continue
        for m in REAL.finditer(text):
            if m.group(0).lower() not in ALLOWED:
                offenders.append(f"{rel}: identity {m.group(0)}")
        for token in {w.lower() for w in WORD.findall(text)}:
            if _digest(token) in BANNED_NAMES:
                offenders.append(f"{rel}: name {token}")
        if "vendor/" in rel:
            continue  # third-party code, where a bare decimal is not a coordinate
        numbers = {t.rstrip("0").rstrip(".") for t in NUMBER.findall(text)}
        hits = {t for t in numbers if _digest(t) in BANNED_COORDS}
        axes = {t for t in numbers if _digest(t) in BANNED_COORD_AXES}
        if len(axes) == len(BANNED_COORD_AXES):
            hits |= axes
        offenders += [f"{rel}: coordinate {t}" for t in sorted(hits)]
    assert not unreadable, "tracked paths could not be read, so they went unscanned:\n" + "\n".join(unreadable)
    assert not offenders, "real node identities, coordinates or names in tracked files:\n" + "\n".join(offenders)
