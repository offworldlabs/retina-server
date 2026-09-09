"""deploy/rollback.sh, exercised against scratch repositories.

The lookup is extracted from the script and executed under the script's own
shell flags rather than reimplemented here, so these fail if the real line
regresses. Running rollback.sh whole is not an option: everything after this
lookup resets the tree and rebuilds containers.

Production's rollback died here on 2026-08-27 (ClickUp 86cbaxrcw). Piping into a
reader that stops after one line is a race under `set -o pipefail`, not a
threshold: measured on the production checkout at 212 tags it aborted on 3 runs
in 5. So the first test below forces the writer to still be writing, by making
the listing larger than a pipe buffer, rather than hoping to lose the race.
deploy/pre-deploy.sh carries the same warning at its own image lookup.
"""

import re
import subprocess
from pathlib import Path

from tests.migration_helpers import BACKEND

ROLLBACK_SH = BACKEND.parent / "deploy" / "rollback.sh"

# A pipe buffer is 64 KiB on Linux and smaller on macOS. At ~23 bytes a line
# this is several times either, so a reader taking one line cannot drain it and
# the writer is guaranteed to still be going when the pipe closes.
BULK_TAG_COUNT = 8000


def _git(repo: Path, *args: str, env: dict | None = None) -> None:
    subprocess.run(  # noqa: S603, S607
        ["git", *args], cwd=repo, check=True, capture_output=True, env=env
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("scratch\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _lookup_line() -> str:
    """The LAST_GOOD assignment exactly as it stands in rollback.sh."""
    lines = [ln.strip() for ln in ROLLBACK_SH.read_text().splitlines() if re.match(r"\s*LAST_GOOD=", ln)]
    assert len(lines) == 1, f"expected one LAST_GOOD assignment, found {len(lines)}"
    return lines[0]


def _run_lookup(repo: Path) -> subprocess.CompletedProcess:
    # `set -euo pipefail` is rollback.sh's own preamble. Without pipefail a
    # SIGPIPE is invisible and these pass against the broken line.
    script = f'set -euo pipefail\n{_lookup_line()}\nprintf "%s" "$LAST_GOOD"\n'
    return subprocess.run(  # noqa: S602, S607
        ["bash", "-c", script], cwd=repo, capture_output=True, text=True, check=False
    )


def test_lookup_survives_a_listing_larger_than_a_pipe_buffer(tmp_path):
    repo = _repo(tmp_path)
    head = subprocess.run(  # noqa: S603, S607
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()
    # One batch, because 8000 `git tag` invocations is a slow test.
    batch = "".join(f"create refs/tags/deploy-{i:08d}-000000 {head}\n" for i in range(BULK_TAG_COUNT))
    subprocess.run(  # noqa: S603, S607
        ["git", "update-ref", "--stdin"],
        cwd=repo,
        input=batch,
        text=True,
        check=True,
        capture_output=True,
    )

    result = _run_lookup(repo)

    assert result.returncode != 141, (
        "the lookup died of SIGPIPE. Under `set -o pipefail` a pipeline into a reader that "
        "stops early aborts the whole script, so rollback.sh never reaches the tree revert "
        "and production stays on the build it was rolling back from, while CI reports that "
        "a rollback ran. See deploy/pre-deploy.sh for the same note."
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.startswith("deploy-"), f"got {result.stdout!r}"


def test_lookup_picks_the_newest_tag(tmp_path):
    repo = _repo(tmp_path)
    # Annotated, so each carries its own tagger date. Lightweight tags inherit
    # the commit's, which would make -creatordate a tie across all of them and
    # the "newest" assertion meaningless.
    for name, when in (
        ("deploy-20260101-000000", "2026-01-01T00:00:00Z"),
        ("deploy-20260827-140211", "2026-08-27T14:02:11Z"),
        ("deploy-20260501-120000", "2026-05-01T12:00:00Z"),
    ):
        _git(
            repo,
            "tag",
            "-a",
            name,
            "-m",
            name,
            env={
                "PATH": "/usr/bin:/bin",
                "GIT_COMMITTER_DATE": when,
                "GIT_COMMITTER_NAME": "Test",
                "GIT_COMMITTER_EMAIL": "test@example.com",
            },
        )

    result = _run_lookup(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "deploy-20260827-140211", f"got {result.stdout!r}"


def test_an_abort_partway_says_the_rollback_did_not_complete(tmp_path):
    """The 2026-08-27 failure was silent: the log's last line announced a
    rollback that then aborted, so CI reported a failed step and nothing said
    production had not moved. Any non-zero exit before the restore finishes has
    to contradict that announcement itself.
    """
    result = subprocess.run(  # noqa: S603, S607
        ["bash", str(ROLLBACK_SH)],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "APP_DIR": str(tmp_path / "absent")},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "ROLLBACK DID NOT COMPLETE" in combined, (
        f"an aborted rollback said nothing about not having completed:\n{combined}"
    )
