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

import os
import re
import subprocess
from pathlib import Path

from tests.migration_helpers import BACKEND

ROLLBACK_SH = BACKEND.parent / "deploy" / "rollback.sh"
WAIT_SH = BACKEND.parent / "deploy" / "wait-for-health.sh"

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


def _assignment(name: str) -> str:
    """The `name=` assignment exactly as it stands in rollback.sh."""
    lines = [ln.strip() for ln in ROLLBACK_SH.read_text().splitlines() if re.match(rf"\s*{name}=", ln)]
    assert len(lines) == 1, f"expected one {name} assignment, found {len(lines)}"
    return lines[0]


def _preamble() -> str:
    """rollback.sh's own shell flags; a fragment run without them proves nothing."""
    lines = [ln.strip() for ln in ROLLBACK_SH.read_text().splitlines() if ln.startswith("set -")]
    assert len(lines) == 1, f"expected one set line, found {len(lines)}"
    return lines[0]


def _lookup_line() -> str:
    return _assignment("LAST_GOOD")


def _run_lookup(repo: Path) -> subprocess.CompletedProcess:
    # Without pipefail a SIGPIPE is invisible and these pass against the
    # broken line.
    script = f'{_preamble()}\n{_lookup_line()}\nprintf "%s" "$LAST_GOOD"\n'
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


# ── A container the deploy never replaced is left running ────────────────────


def _function_text(name: str) -> str:
    """A function's definition exactly as it stands in rollback.sh."""
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", ROLLBACK_SH.read_text(), re.M | re.S)
    assert match, f"{name} not found in rollback.sh"
    return match.group(0)


# Shadows the docker binary for the three calls the predicate makes; values
# arrive through the environment. `exit` inside `$(...)` only ends that
# substitution, which is how the real CLI's failures reach the script too. The
# probe must be the bounded one from wait-for-health.sh: unbounded, a wedged
# server that accepts and never answers would hang the rollback right here.
STUB_DOCKER = """
docker() {
  case "$*" in
    "compose ps -q --status running server") printf '%s\\n' "$CID" ;;
    "inspect --format {{.Created}} "*) printf '%s\\n' "$CREATED" ;;
    "compose exec -T server python3 -c "*"timeout=5)") [ "$HEALTHY" = 1 ] ;;
    *) echo "unexpected docker call: $*" >&2; exit 2 ;;
  esac
}
"""


def _predates_and_answers(tag: str, *, cid: str, created: str, healthy: bool) -> bool:
    script = (
        f"{_preamble()}\nsource {WAIT_SH}\n{_assignment('COMPOSE_SERVICE')}\n{STUB_DOCKER}"
        f"{_function_text('container_predates_and_answers')}"
        f"if container_predates_and_answers {tag!r}; then echo yes; else echo no; fi\n"
    )
    env = {**os.environ, "CID": cid, "CREATED": created, "HEALTHY": "1" if healthy else "0"}
    result = subprocess.run(  # noqa: S603, S607
        ["bash", "-c", script], capture_output=True, text=True, check=True, env=env
    )
    return result.stdout.strip() == "yes"


TAG = "deploy-20260915-173523"
BEFORE = "2026-09-15T17:33:40.123456789Z"
AFTER = "2026-09-15T17:36:02.000000000Z"


def test_a_container_older_than_the_tag_that_answers_is_left_alone():
    assert _predates_and_answers(TAG, cid="c91d23bb4158", created=BEFORE, healthy=True)


def test_a_container_created_after_the_tag_is_restarted():
    # Every recreate postdates the tag, a config-only change on the same image
    # included, so this is what tells "never replaced" from "replaced".
    assert not _predates_and_answers(TAG, cid="c91d23bb4158", created=AFTER, healthy=True)


def test_the_same_second_as_the_tag_counts_as_after():
    assert not _predates_and_answers(TAG, cid="c91d23bb4158", created="2026-09-15T17:35:23.900000000Z", healthy=True)


def test_a_container_that_does_not_answer_is_restarted():
    # Running is not serving; a wedged process on the right image still needs
    # the restart.
    assert not _predates_and_answers(TAG, cid="c91d23bb4158", created=BEFORE, healthy=False)


def test_no_running_container_means_the_full_restart():
    assert not _predates_and_answers(TAG, cid="", created=BEFORE, healthy=True)


# ── The health wait is the deploys' ──────────────────────────────────────────


def test_the_health_wait_is_sourced_before_the_tree_moves():
    # The deploys wait with deploy/wait-for-health.sh, so a rollback that
    # waited differently could fail a boot the deploy would have accepted. Every
    # restore path moves the tree, possibly to a commit without that file, so it
    # has to be loaded before the first move rather than run at the wait.
    commands = [ln.strip() for ln in ROLLBACK_SH.read_text().splitlines() if not ln.lstrip().startswith("#")]
    source = commands.index('source "$(dirname "${BASH_SOURCE[0]}")/wait-for-health.sh"')
    moves = [i for i, ln in enumerate(commands) if re.match(r"git (?:reset --hard|checkout)\b", ln)]
    assert moves and source < min(moves)
    assert "if wait_for_health; then" in commands
