"""No real node identity or node hostname may live in the tracked tree.

This repo is public and the API publishes nodes under an opaque ``node_ref``.
A real ``ret…`` id or a ``radar3*.retnode.com`` hostname in a tracked file
undoes that at the source, so the ban is a test rather than a review habit.

The allow-list is the set of ids invented for documentation and fixtures.
Adding to it is how a new synthetic id becomes legal; a real one must never
be added.
"""

import re
import subprocess
from pathlib import Path

REAL = re.compile(r"ret[0-9a-f]{8}|radar3a?[-.]retnode|radar3a?\.retnode\.com")
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
}


def test_no_real_node_identity_is_tracked():
    root = Path(__file__).resolve().parents[2]
    files = subprocess.run(["git", "ls-files"], cwd=root, capture_output=True, text=True).stdout.split()
    offenders = []
    for rel in files:
        p = root / rel
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        for m in REAL.finditer(text):
            if m.group(0) not in ALLOWED:
                offenders.append(f"{rel}: {m.group(0)}")
    assert not offenders, "real node identities in tracked files:\n" + "\n".join(offenders)
