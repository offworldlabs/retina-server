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
a value in doubt is checked by running the test.

The coordinate ban covers the true position and every truncation of it that
still lands inside the published fuzz annulus. That annulus is the standard: a
fixture further out than the fuzz radius discloses less than the public feed
already does, and 1 decimal place is always far enough, which is why only
literals carrying two or more are examined.

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
BANNED_COORDS = {
    "7862f123bfbcb375a099a2b5d04973d0fd2958249c02de3c2b18d86c7a8fed6e",
    "bf9125e82d94352415cbd1f5b94e4b0e94453a1ab310d52b929e276426807f89",
    "7eadf42c729d4488ec977fe7ee3132d19b678742c8525c24c5a326b1b0669907",
    "2e37706226e894d4d457071fdd25006dfb3d8535e582103a9a9e54de1f3d9810",
    "056ef01068ba443c0b768f4d1c9ae043773eb696951c3f4b1130d32fee1bc69d",
    "0d528a55b6c1ab0aa25c23195b0e441aebb4baa1ff4fed8c442b9672dbd79021",
    "585ab776fb5e716de08712e5c5d7b6da5980434f35f98d66107bc28c3e47bb8f",
    "e45fdbea330db87bce6eb854e1b005aabd8f046524fcabdad55b1fdc9e85f6ea",
    "5ba69d3a93f6453c9f1e1fcef27375dfd584811c2e12615be0bfae843de2678d",
    "703d99b9617c42b5ba0d55bde89396b7c3ce327c64e5641386026a5bf74834cd",
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


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _tracked_files(root: Path) -> list[str]:
    """Every tracked path. check=True so a broken listing fails rather than
    passing an empty scan."""
    out = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True)
    return out.stdout.split()


def test_no_real_node_identity_is_tracked():
    root = Path(__file__).resolve().parents[2]
    files = _tracked_files(root)
    assert files, "git ls-files listed nothing: the scan would pass without reading a file"
    offenders = []
    scanned = 0
    for rel in files:
        if "vendor/" in rel:
            continue  # third-party code, where a bare decimal is not a coordinate
        p = root / rel
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue  # submodule gitlinks and binaries
        scanned += 1
        for m in REAL.finditer(text):
            if m.group(0).lower() not in ALLOWED:
                offenders.append(f"{rel}: {m.group(0)}")
        for token in set(NUMBER.findall(text)):
            if _digest(token.rstrip("0").rstrip(".")) in BANNED_COORDS:
                offenders.append(f"{rel}: coordinate {token}")
        for token in {w.lower() for w in WORD.findall(text)}:
            if _digest(token) in BANNED_NAMES:
                offenders.append(f"{rel}: name {token}")
    assert scanned, "no tracked file could be read: the scan would pass vacuously"
    assert not offenders, "real node identities in tracked files:\n" + "\n".join(offenders)
