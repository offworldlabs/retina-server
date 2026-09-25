"""`just new-migration` has two halves: a number no other branch holds, and a
file that passes test_migrations.py as scaffolded. These pin both, the second
against the rollback classifier's own pattern and values, since it is what reads
the declaration on a droplet.
"""

import argparse
import importlib.util
import shutil
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from scripts.next_revision import VERSIONS, Refused, _is_own, highest, next_revision
from tests.migration_helpers import BACKEND

_spec = importlib.util.spec_from_file_location("classify", BACKEND.parent / "deploy" / "classify-migration-gap.py")
classify = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(classify)


def test_the_highest_number_ignores_everything_but_numbered_revisions():
    paths = [
        f"{VERSIONS}/0007_node_contacts.py",
        f"{VERSIONS}/0012_magic_link_intent.py",
        # What alembic names a revision whose message has no word characters.
        f"{VERSIONS}/0013_.py",
        f"{VERSIONS}/__init__.py",
        f"{VERSIONS}/3f2a9c1b7d4e_unnumbered.py",
        f"{VERSIONS}/0099_fixture/readme.py",
        "backend/tests/0500_not_a_revision.py",
        "docs/0400_note.py",
    ]
    assert highest(paths) == 13
    assert highest([]) == 0


def test_the_next_number_is_past_every_source():
    tree = [f"{VERSIONS}/0019_node_events.py"]
    main = [f"{VERSIONS}/0019_node_events.py"]
    pulls = [f"{VERSIONS}/0021_claimed_by_an_open_pr.py", "backend/main.py"]
    assert next_revision(tree, main, pulls) == "0022"
    assert next_revision(tree, main, []) == "0020"
    # This branch's own new revision, ahead of main.
    assert next_revision([*tree, f"{VERSIONS}/0020_mine.py"], main, []) == "0021"


@pytest.mark.parametrize(
    "tree",
    [
        [f"{VERSIONS}/0018_node_reports.py"],
        # Numbered past an open PR's 0019 that has since landed: a higher
        # number than main's, and still missing main's head.
        [f"{VERSIONS}/0018_node_reports.py", f"{VERSIONS}/0020_mine.py"],
    ],
    ids=["behind", "ahead-but-missing-mains-head"],
)
def test_a_tree_missing_mains_revisions_is_refused(tree):
    """Its revision would chain from an older head than main's and fork it."""
    main = [f"{VERSIONS}/0018_node_reports.py", f"{VERSIONS}/0019_node_events.py"]
    with pytest.raises(Refused, match="0019_node_events"):
        next_revision(tree, main, [])


def _scaffold(tmp_path: Path, cmd_opts: argparse.Namespace | None) -> str:
    """A revision rendered through the real template into a copy of versions/.
    Without --autogenerate, revision never runs env.py, so nothing touches a
    database."""
    shutil.copytree(BACKEND / "migrations", tmp_path / "migrations", ignore=shutil.ignore_patterns("__pycache__"))
    config = Config()
    config.set_main_option("script_location", str(tmp_path / "migrations"))
    config.cmd_opts = cmd_opts
    script = command.revision(config, message="add a thing", rev_id="9999")
    return Path(script.path).read_text()


@pytest.mark.parametrize("safety", [classify.ADDITIVE, classify.DESTRUCTIVE])
def test_a_scaffold_declares_the_safety_the_recipe_passes(tmp_path, safety):
    source = _scaffold(tmp_path, argparse.Namespace(x=[f"rollback_safety={safety}"]))
    declared = classify.SAFETY_RE.search(source)
    assert declared, "the scaffold declares no rollback_safety"
    assert declared.group(1) == safety


@pytest.mark.parametrize("cmd_opts", [None, argparse.Namespace(x=None)], ids=["api", "cli-without-x"])
def test_a_scaffold_made_without_the_recipe_is_refused(tmp_path, cmd_opts):
    """A bare `alembic revision` must not produce a file that passes as
    declared, or the choice the recipe forces is skipped silently."""
    declared = classify.SAFETY_RE.search(_scaffold(tmp_path, cmd_opts))
    assert declared, "the placeholder no longer matches the pattern, so the failure would not name it"
    assert declared.group(1) not in {classify.ADDITIVE, classify.DESTRUCTIVE}


@pytest.mark.parametrize(
    ("pull", "own"),
    [
        ({"number": 7, "headRefName": "feat/x", "isCrossRepository": False}, True),
        ({"number": 8, "headRefName": "feat/y", "isCrossRepository": True}, True),
        ({"number": 9, "headRefName": "feat/x", "isCrossRepository": True}, False),
        ({"number": 10, "headRefName": "feat/z", "isCrossRepository": False}, False),
    ],
    ids=["named-after-this-branch", "matched-by-gh", "a-fork-of-the-same-name", "someone-elses"],
)
def test_only_this_branchs_own_pull_request_is_left_out(pull, own):
    assert _is_own(pull, branch="feat/x", matched=8) is own
