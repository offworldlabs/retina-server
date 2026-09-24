"""Every action CI runs is pinned to a commit.

A tag is a pointer its owner can move, and the next run executes whatever it
names. Several of these jobs hold root SSH keys to the droplets.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
FILES = sorted(
    [
        *(ROOT / ".github" / "workflows").glob("*.y*ml"),
        *(ROOT / ".github" / "actions").glob("*/action.y*ml"),
    ]
)
COMMIT = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")
DIGEST = re.compile(r"^docker://\S+@sha256:[0-9a-f]{64}$")
# Dependabot rewrites this comment with the SHA, and a reader has nothing else
# to tell which release a pin is.
VERSIONED = re.compile(r"^\s*(?:-\s+)?uses:\s*\S+@[0-9a-f]{40}\s+#\s*v\d+(?:\.\d+)*\s*$")


def _uses(node: object) -> Iterator[str]:
    """Every `uses:` value in a loaded workflow or action, jobs and steps alike."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses" and isinstance(value, str):
                yield value
            else:
                yield from _uses(value)
    elif isinstance(node, list):
        for item in node:
            yield from _uses(item)


def _remote(ref: str) -> bool:
    return not ref.startswith("./")


def test_there_are_workflows_to_check():
    assert len(FILES) > 1


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_action_is_pinned_to_a_commit(path):
    refs = [ref for ref in _uses(yaml.safe_load(path.read_text())) if _remote(ref)]
    floating = [ref for ref in refs if not (COMMIT.match(ref) or DIGEST.match(ref))]
    assert not floating, (
        f"{floating} name a tag or branch. Pin each to the full 40-character commit SHA "
        "with its release in a comment, e.g. `uses: actions/checkout@<sha> # v4.4.0`."
    )


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_every_pin_names_its_release(path):
    lines = [line for line in path.read_text().splitlines() if re.match(r"^\s*(?:-\s+)?uses:\s*[^.\s]", line)]
    bare = [line.strip() for line in lines if "docker://" not in line and not VERSIONED.match(line)]
    assert not bare, f"{bare} carry no `# vX.Y.Z` comment after the SHA"
