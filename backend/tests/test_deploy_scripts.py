"""deploy/gate.sh and deploy/deploy.sh, run for real against a scratch clone.

Stand-in commands on PATH play the parts that need a droplet: hostname, docker,
df, openssl, flock and sleep. The gate and the deploy are the files in this
tree with their absolute host paths pointed into the scratch directory, and
pre-deploy.sh, wait-for-health.sh and rollback.sh are stand-ins that record
being called; their own behaviour is tested in test_rollback_script.py and
test_wait_for_health.py.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parents[2] / "deploy"
GATE_SH = DEPLOY / "gate.sh"
DEPLOY_SH = DEPLOY / "deploy.sh"
SETUP_SH = DEPLOY / "setup-server.sh"
TOWER_CONTRACT_SH = DEPLOY / "tower-contract.sh"

APP_DIR = "/opt/retina-server"
GATE_PATH = "/usr/local/sbin/retina-deploy-gate"
CF_CA = "/etc/ssl/cloudflare/origin-pull-ca.pem"

GOOD_ENV = "JWT_SECRET=" + "x" * 64 + "\nRADAR_API_KEY=key\n"
TOWERS_BODY = '{"towers":[],"user_frequencies_mhz":[1234.5]}'
CONFIG_BODY = '{"ranking":{},"receiver":{},"broadcast_bands":{},"search":{}}'


def _pointed(text: str, *pairs: tuple[str, str]) -> str:
    # A path that is no longer in the script would leave the test driving the
    # real host path, so each one must be there to replace.
    for old, new in pairs:
        assert old in text, f"{old} is no longer in the script; update this test"
        text = text.replace(old, new)
    return text


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603, S607
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@dataclass
class Box:
    root: Path

    @property
    def app(self) -> Path:
        return self.root / "app"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def seed(self) -> Path:
        return self.root / "seed"

    @property
    def installed_gate(self) -> Path:
        return self.root / "sbin" / "retina-deploy-gate"

    @property
    def ca(self) -> Path:
        return self.root / "origin-pull-ca.pem"

    @property
    def gate_copy(self) -> str:
        return _pointed(
            GATE_SH.read_text(),
            (APP_DIR, str(self.app)),
            (GATE_PATH, str(self.installed_gate)),
        )

    @property
    def deploy_copy(self) -> str:
        return _pointed(DEPLOY_SH.read_text(), (CF_CA, str(self.ca)))

    def set(self, name: str, value: str) -> None:
        (self.state / name).write_text(value)

    def calls(self) -> list[str]:
        log = self.state / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def called(self, prefix: str) -> list[str]:
        return [line for line in self.calls() if line.startswith(prefix)]

    def head(self) -> str:
        return _git(self.app, "rev-parse", "HEAD")

    def push(self, files: dict[str, str | None], branch: str = "main") -> str:
        """Commit files to the seed clone and push them to origin; None deletes."""
        _git(self.seed, "fetch", "-q", "origin")
        _git(self.seed, "checkout", "-q", "-B", branch, "origin/main")
        for rel, text in files.items():
            path = self.seed / rel
            if text is None:
                path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-qm", f"change on {branch}")
        _git(self.seed, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        return _git(self.seed, "rev-parse", "HEAD")

    def gate_command(self, request: str | None, service: str = "retina-server") -> dict:
        env = {
            "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
            "HOME": str(self.root / "home"),
        }
        if request is not None:
            env["SSH_ORIGINAL_COMMAND"] = request
        return {"args": ["bash", str(self.root / "gate.sh"), service], "cwd": self.root, "env": env}

    def gate(self, request: str | None, service: str = "retina-server") -> subprocess.CompletedProcess:
        return subprocess.run(  # noqa: S603, S607
            **self.gate_command(request, service), capture_output=True, text=True, check=False
        )

    def deploy_directly(self, **env: str) -> subprocess.CompletedProcess:
        script = self.root / "deploy-direct.sh"
        script.write_text(self.deploy_copy)
        return subprocess.run(  # noqa: S603, S607
            ["bash", str(script)],
            cwd=self.app,
            env={
                "PATH": f"{self.root / 'bin'}:{os.environ['PATH']}",
                "HOME": str(self.root / "home"),
                "APP_DIR": str(self.app),
                "RUN_ID": "1",
                "TARGET_SHA": self.head(),
                **env,
            },
            capture_output=True,
            text=True,
            check=False,
        )


def _stubs(box: Box) -> None:
    log = box.state / "calls.log"
    marker = box.app / ".deploy-in-progress"
    state = box.state
    b = box.root / "bin"
    _executable(b / "hostname", f'#!/bin/bash\ncat "{state}/hostname"\n')
    _executable(b / "sleep", "#!/bin/bash\n")
    # Holds the lock back while a `hold` file exists, until `go` appears, and
    # says it is waiting with a `waiting` file.
    _executable(
        b / "flock",
        f'#!/bin/bash\nif [ -f "{state}/hold" ]; then\n    touch "{state}/waiting"\n'
        f'    while [ ! -f "{state}/go" ]; do /bin/sleep 0.05; done\nfi\n'
        f'echo "flock $*" >> "{log}"\n',
    )
    _executable(
        b / "df",
        "#!/bin/bash\n"
        "echo 'Filesystem 1M-blocks Used Available Use% Mounted on'\n"
        f'echo "/dev/vda1 100000 1 $(cat "{state}/disk") 1% /"\n',
    )
    _executable(b / "openssl", '#!/bin/bash\n[ "$1" = x509 ] && grep -q "BEGIN CERTIFICATE" "$3"\n')
    _executable(
        b / "docker",
        f"""#!/bin/bash
marker=no; [ -f "{marker}" ] && marker=yes
echo "docker $* marker=$marker head=$(git -C "{box.app}" rev-parse --short HEAD)" >> "{log}"
case "$1 $2" in
    "network inspect") [ ! -f "{state}/no-edge" ] && echo "tower-finder-service " ;;
    "run --rm")
        for arg in "$@"; do
            case "$arg" in
                *"/api/towers?"*) cat "{state}/towers" ;;
                *"/api/config") cat "{state}/config" ;;
                *"/api/geocode") cat "{state}/geocode" ;;
            esac
        done
        ;;
esac
""",
    )


def _tree(box: Box) -> dict[str, str]:
    log = box.state / "calls.log"
    return {
        "deploy/deploy.sh": box.deploy_copy,
        "deploy/gate.sh": box.gate_copy,
        "deploy/tower-contract.sh": TOWER_CONTRACT_SH.read_text(),
        "deploy/pre-deploy.sh": (
            "#!/bin/bash\nm=no; [ -f .deploy-in-progress ] && m=yes\n"
            'echo "pre-deploy marker=$m head=$(git rev-parse --short HEAD)'
            f' gc.autoDetach=$(git config gc.autoDetach)" >> "{log}"\n'
        ),
        "deploy/wait-for-health.sh": (
            "#!/bin/bash\nm=no; [ -f .deploy-in-progress ] && m=yes\n"
            f'echo "wait-for-health marker=$m head=$(git rev-parse --short HEAD)" >> "{log}"\n'
            # Stands in for a record that cannot be written, a full disk say.
            f'if [ -f "{box.state}/no-record" ]; then mkdir .last-deploy; fi\n'
            f'[ "$(cat "{box.state}/health")" = ok ]\n'
        ),
        "deploy/rollback.sh": (
            "#!/bin/bash\nm=no; [ -f .deploy-in-progress ] && m=yes\n"
            'echo "rollback APP_DIR=$APP_DIR gc.autoDetach=$(git config gc.autoDetach) marker=$m"'
            f' >> "{log}"\nexit "$(cat "{box.state}/rollback-rc")"\n'
        ),
        **{
            f"deploy/env.{env}.example": f"COMPOSE_FILE=docker-compose.yml:docker-compose.{env}.yml\n"
            for env in ("prod", "staging", "test")
        },
        ".gitignore": ".env\nbackend/.env\n.deploy-in-progress\n.last-deploy\n.deploy-gate.lock\n",
        "VERSION": "first\n",
    }


@pytest.fixture
def box(tmp_path: Path) -> Box:
    box = Box(tmp_path)
    for directory in (box.state, tmp_path / "home", tmp_path / "sbin"):
        directory.mkdir()
    for name, value in (
        ("hostname", "retina-prod"),
        ("disk", "50000"),
        ("health", "ok"),
        ("rollback-rc", "0"),
        ("towers", TOWERS_BODY),
        ("config", CONFIG_BODY),
        ("geocode", "422"),
    ):
        box.set(name, value)
    box.ca.write_text("-----BEGIN CERTIFICATE-----\n")
    _stubs(box)
    (tmp_path / "gate.sh").write_text(box.gate_copy)

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)  # noqa: S603, S607
    box.seed.mkdir()
    _git(box.seed, "init", "-q", "-b", "main")
    _git(box.seed, "remote", "add", "origin", str(origin))
    for rel, text in _tree(box).items():
        _executable(box.seed / rel, text)
    _git(box.seed, "add", "-A")
    _git(box.seed, "commit", "-qm", "first")
    _git(box.seed, "push", "-q", "origin", "HEAD:refs/heads/main")
    _git(tmp_path, "clone", "-q", str(origin), str(box.app))
    (box.app / "backend").mkdir()
    (box.app / "backend" / ".env").write_text(GOOD_ENV)
    return box


@pytest.fixture
def moved(box: Box) -> str:
    """main one commit past what the box runs."""
    return box.push({"VERSION": "second\n"})


# ── What the key can ask for ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "request_text",
    [
        "status; id",
        "deploy 1 && id",
        "deploy $(id)",
        "deploy `id`",
        "status\nid",
        "deploy 1 'main'",
        "deploy 1 main*",
    ],
)
def test_no_request_reaches_a_shell(box, request_text):
    result = box.gate(request_text)
    assert result.returncode == 1
    assert "the request holds characters no verb takes" in result.stdout
    assert box.calls() == []


@pytest.mark.parametrize("request_text", [None, "", "bash", "rollback", "status extra", "deploy"])
def test_only_the_four_verbs_run(box, request_text):
    result = box.gate(request_text)
    assert result.returncode == 1
    assert "::error::retina-deploy-gate:" in result.stdout
    assert not box.called("pre-deploy") and not box.called("rollback")


def test_an_unknown_service_is_refused(box):
    result = box.gate("status", service="tower-finder-service")
    assert result.returncode == 1
    assert "unknown service" in result.stdout


def test_a_box_without_the_deploy_directory_is_refused(box):
    box.app.rename(box.root / "elsewhere")
    result = box.gate("status")
    assert result.returncode == 1
    assert "does not exist" in result.stdout


@pytest.mark.parametrize("request_text", ["deploy 123", "rollback 77 deploy", "probe-towers", "status"])
def test_a_box_that_is_not_a_clone_is_refused(box, request_text):
    shutil.rmtree(box.app / ".git")
    result = box.gate(request_text)
    assert result.returncode == 1
    assert "is not a git clone" in result.stdout
    assert not box.called("pre-deploy") and not box.called("rollback")


def test_a_box_that_is_no_retina_environment_is_refused(box):
    box.set("hostname", "retina-prod-latest")
    result = box.gate("status")
    assert result.returncode == 1
    assert "no retina environment" in result.stdout


@pytest.mark.parametrize("request_text", ["deploy 1 main none", "deploy abc", "deploy -1", "deploy 1 2"])
def test_production_deploys_main_and_nothing_else(box, request_text):
    before = box.head()
    result = box.gate(request_text)
    assert result.returncode == 1
    assert box.head() == before
    assert not box.called("pre-deploy")


# ── deploy ───────────────────────────────────────────────────────────────────


def test_a_deploy_runs_the_fetched_commits_script_not_the_trees(box, moved):
    # Whatever sits in the working tree, even edited by hand, is not what runs.
    (box.app / "deploy" / "deploy.sh").write_text('#!/bin/bash\necho "stale script ran"\nexit 1\n')
    result = box.gate("deploy 123")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "stale script ran" not in result.stdout
    assert box.head() == moved
    assert f"Deployed {moved} to production." in result.stdout


def test_a_deploy_keeps_its_order(box, moved):
    before = _git(box.app, "rev-parse", "--short", "HEAD")
    after = moved[: len(before)]
    assert box.gate("deploy 123").returncode == 0
    calls = [line for line in box.calls() if line.startswith(("pre-deploy", "docker", "flock", "wait-for-health"))]
    assert calls[0].startswith("flock -w")
    assert calls[1] == f"pre-deploy marker=no head={before} gc.autoDetach=false"
    swap = next(i for i, line in enumerate(calls) if line.startswith("docker compose up"))
    assert calls[swap] == f"docker compose up -d --build --remove-orphans server marker=yes head={after}"
    # Dangling images only, then the cache, both before the swap, and no stop
    # of any kind ahead of it.
    assert calls[swap - 2] == f"docker image prune -f marker=yes head={after}"
    assert calls[swap - 1] == f"docker builder prune -f --keep-storage 10GB marker=yes head={after}"
    assert not [line for line in calls[:swap] if any(v in line.split() for v in ("down", "stop", "rm", "kill"))]
    assert calls[swap + 1] == f"wait-for-health marker=yes head={after}"


def test_a_caller_that_goes_away_does_not_stop_the_deploy(box, moved):
    # The CI job is cancelled, or its connection drops, part way through.
    process = subprocess.Popen(  # noqa: S603, S607
        **box.gate_command("deploy 123"), stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    assert process.stdout.read(1)
    process.stdout.close()
    assert process.wait(timeout=300) == 0
    assert not (box.app / ".deploy-in-progress").exists()
    assert (box.app / ".last-deploy").read_text().startswith("run_id=123\n")
    assert f"Deployed {moved} to production." in (box.app / ".deploy-gate.log").read_text()
    # The job that went away reports its deploy failed, and the deploy it
    # finished is left in place, but not quietly: its checks never ran.
    result = box.gate("rollback 123 deploy")
    assert result.returncode == 1
    assert "Its checks may never have run" in result.stdout
    assert not box.called("rollback")


def test_a_caller_gone_before_the_lock_freed_starts_nothing(box, moved):
    before = box.head()
    box.set("hold", "")
    process = subprocess.Popen(  # noqa: S603, S607
        **box.gate_command("deploy 123"), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    deadline = time.monotonic() + 120
    while not (box.state / "waiting").exists():
        assert time.monotonic() < deadline, "the gate never queued for the lock"
        time.sleep(0.05)
    process.stdout.close()
    box.set("go", "")
    assert process.wait(timeout=120) != 0
    assert box.called("flock")
    assert not box.called("pre-deploy")
    assert box.head() == before


def test_a_healthy_deploy_leaves_a_record_and_no_marker(box, moved):
    before = time.time()
    assert box.gate("deploy 123").returncode == 0
    assert not (box.app / ".deploy-in-progress").exists()
    record = dict(line.split("=", 1) for line in (box.app / ".last-deploy").read_text().splitlines())
    assert record["run_id"] == "123"
    assert record["sha"] == moved
    assert int(before) <= int(record["finished"]) <= time.time()
    assert (box.app / ".env").read_text() == "COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml\n"


def test_a_deploy_that_cannot_record_itself_stays_marked(box, moved):
    box.set("no-record", "")
    assert box.gate("deploy 123").returncode == 1
    assert (box.app / ".deploy-in-progress").read_text().startswith("run 123 started ")
    (box.app / ".last-deploy").rmdir()
    assert box.gate("rollback 123 deploy").returncode == 0
    assert box.called("rollback")


def test_a_deploy_of_main_refreshes_the_gate(box, moved):
    assert box.gate("deploy 123").returncode == 0
    assert box.installed_gate.read_text() == box.gate_copy
    assert os.access(box.installed_gate, os.X_OK)


def test_a_gate_that_will_not_install_does_not_fail_a_healthy_deploy(box):
    target = box.push({"deploy/gate.sh": "if then\n"})
    result = box.gate("deploy 123")
    assert result.returncode == 0, result.stdout
    assert "the gate was not refreshed" in result.stdout
    assert (box.app / ".last-deploy").read_text().startswith("run_id=123\n")
    assert box.head() == target


def test_a_tag_named_main_is_never_deployed(box, moved):
    # A bare `git fetch origin main` prefers refs/tags/main to the branch.
    tagged = box.push({"VERSION": "tagged\n"}, branch="scratch")
    _git(box.root / "origin.git", "update-ref", "refs/tags/main", tagged)
    assert box.gate("deploy 123").returncode == 0
    assert box.head() == moved


def test_a_commit_without_the_deploy_script_is_refused(box):
    before = box.head()
    box.push({"deploy/deploy.sh": None})
    result = box.gate("deploy 123")
    assert result.returncode == 1
    assert "has no deploy/deploy.sh" in result.stdout
    assert box.head() == before
    assert not box.called("pre-deploy")


def test_a_box_that_never_answers_is_left_marked(box, moved):
    box.set("health", "down")
    result = box.gate("deploy 123")
    assert result.returncode == 1
    assert len(box.called("wait-for-health")) == 1
    assert (box.app / ".deploy-in-progress").read_text().startswith("run 123 started ")
    assert not (box.app / ".last-deploy").exists()


def test_staging_deploys_without_productions_secrets(box, moved):
    box.set("hostname", "retina-staging")
    (box.app / "backend" / ".env").write_text("RADAR_API_KEY=key\n")
    result = box.gate("deploy 123")
    assert result.returncode == 0, result.stdout
    assert box.head() == moved
    assert (box.app / ".env").read_text() == "COMPOSE_FILE=docker-compose.yml:docker-compose.staging.yml\n"
    assert box.called("docker rm -f retina-staging-fleet")


def test_production_has_no_fleet_container_to_clear(box, moved):
    assert box.gate("deploy 123").returncode == 0
    assert not box.called("docker rm")


# ── Pre-flights refuse, and touch nothing ────────────────────────────────────


def _refuses_untouched(box: Box, result: subprocess.CompletedProcess, before: str, reason: str) -> None:
    assert result.returncode == 1, result.stdout
    assert reason in result.stdout
    assert box.head() == before
    assert not box.called("pre-deploy")
    assert not box.called("docker compose up")


def test_a_marker_left_by_any_run_refuses_the_deploy(box, moved):
    before = box.head()
    (box.app / ".deploy-in-progress").write_text("run 123 started 2026-09-24T00:00:00Z\n")
    _refuses_untouched(box, box.gate("deploy 123"), before, "is still present")


def test_an_env_naming_another_overlay_refuses_the_deploy(box, moved):
    before = box.head()
    (box.app / ".env").write_text("COMPOSE_FILE=docker-compose.yml:docker-compose.staging.yml\n")
    _refuses_untouched(box, box.gate("deploy 123"), before, "does not select docker-compose.prod.yml")


@pytest.mark.parametrize(
    ("backend_env", "reason"),
    [
        ("RADAR_API_KEY=key\n", "no effective JWT_SECRET"),
        ("JWT_SECRET=change-me\nRADAR_API_KEY=key\n", "no effective JWT_SECRET"),
        ("JWT_SECRET=" + "x" * 64 + "\nJWT_SECRET=short\nRADAR_API_KEY=key\n", "no effective JWT_SECRET"),
        ("JWT_SECRET=" + "x" * 64 + "\n", "no effective RADAR_API_KEY"),
    ],
)
def test_production_refuses_without_the_secrets_it_boots_on(box, moved, backend_env, reason):
    before = box.head()
    (box.app / "backend" / ".env").write_text(backend_env)
    _refuses_untouched(box, box.gate("deploy 123"), before, reason)


def test_staging_refuses_without_the_key_its_smoke_suite_reads(box, moved):
    box.set("hostname", "retina-staging")
    before = box.head()
    (box.app / "backend" / ".env").write_text("RADAR_API_KEY=key\nRADAR_API_KEY=\n")
    result = box.gate("deploy 123")
    _refuses_untouched(box, result, before, "the STAGING_RADAR_API_KEY secret's value")
    assert "the smoke suite cannot read the test router" in result.stdout


@pytest.mark.parametrize("host", ["retina-prod", "retina-staging"])
@pytest.mark.parametrize(("ca", "reason"), [(None, "is missing"), ("<html>", "is not a parseable PEM")])
def test_a_box_without_the_origin_pull_ca_refuses_the_deploy(box, moved, host, ca, reason):
    box.set("hostname", host)
    before = box.head()
    if ca is None:
        box.ca.unlink()
    else:
        box.ca.write_text(ca)
    _refuses_untouched(box, box.gate("deploy 123"), before, reason)


def test_a_full_disk_refuses_the_deploy(box, moved):
    box.set("disk", "1500")
    _refuses_untouched(box, box.gate("deploy 123"), box.head(), "only 1500MB free")


def test_the_deploy_checks_the_box_it_was_told_it_is_on(box):
    box.set("hostname", "retina-staging")
    _refuses_untouched(box, box.deploy_directly(DEPLOY_ENV="prod"), box.head(), "expected retina-prod")


def test_failures_are_injected_on_the_test_droplet_only(box):
    result = box.deploy_directly(DEPLOY_ENV="prod", FAIL_AT="after-build")
    _refuses_untouched(box, result, box.head(), "failures are injected on the test droplet only")


# ── The test droplet: branches, and injected failures ────────────────────────


@pytest.fixture
def test_box(box: Box) -> Box:
    box.set("hostname", "retina-test")
    return box


def test_the_test_droplet_needs_neither_secret(test_box, moved):
    (test_box.app / "backend" / ".env").write_text("")
    result = test_box.gate("deploy 9 main none")
    assert result.returncode == 0, result.stdout
    assert test_box.head() == moved


def test_the_test_droplet_deploys_the_branch_it_is_named(test_box):
    target = test_box.push({"VERSION": "feature\n"}, branch="feature/x")
    result = test_box.gate("deploy 9 feature/x none")
    assert result.returncode == 0, result.stdout + result.stderr
    assert test_box.head() == target
    # A branch never replaces the gate.
    assert not test_box.installed_gate.exists()


def test_a_dependency_branch_deploys_to_test(test_box):
    target = test_box.push({"VERSION": "bump\n"}, branch="renovate/@types-node-22.x")
    result = test_box.gate("deploy 9 renovate/@types-node-22.x none")
    assert result.returncode == 0, result.stdout
    assert test_box.head() == target


def test_a_pull_request_ref_is_never_fetched(test_box):
    fork = test_box.push({"VERSION": "fork\n"}, branch="scratch")
    _git(test_box.root / "origin.git", "update-ref", "refs/pull/1/head", fork)
    _git(test_box.root / "origin.git", "update-ref", "-d", "refs/heads/scratch")
    before = test_box.head()
    result = test_box.gate("deploy 9 refs/pull/1/head none")
    assert result.returncode == 1
    assert "could not fetch branch 'refs/pull/1/head'" in result.stdout
    assert test_box.head() == before


@pytest.mark.parametrize(
    "request_text",
    [
        "deploy 9 main",
        "deploy 9 -x none",
        "deploy 9 a..b none",
        "deploy 9 main sometimes",
        "deploy 9 no-such-branch none",
    ],
)
def test_the_test_droplet_refuses_what_is_not_a_branch_and_a_failure(test_box, request_text):
    before = test_box.head()
    assert test_box.gate(request_text).returncode == 1
    assert test_box.head() == before
    assert not test_box.called("pre-deploy")


def test_an_injected_preflight_failure_touches_nothing(test_box, moved):
    before = test_box.head()
    result = test_box.gate("deploy 9 main preflight")
    _refuses_untouched(test_box, result, before, "Injected failure at preflight")
    assert not (test_box.app / ".deploy-in-progress").exists()
    assert not test_box.called("docker network create")
    assert "nothing to roll back" in test_box.gate("rollback 9 deploy").stdout
    assert not test_box.called("rollback")


def test_an_injected_failure_after_the_marker_is_rolled_back(test_box, moved):
    before = test_box.head()
    result = test_box.gate("deploy 9 main after-marker")
    assert result.returncode == 1
    assert test_box.head() == before
    assert test_box.called("pre-deploy")
    assert (test_box.app / ".deploy-in-progress").read_text().startswith("run 9 started ")
    assert test_box.gate("rollback 9 deploy").returncode == 0
    assert test_box.called("rollback")
    assert not (test_box.app / ".deploy-in-progress").exists()


def test_an_injected_failure_after_the_build_is_rolled_back(test_box, moved):
    result = test_box.gate("deploy 9 main after-build")
    assert result.returncode == 1
    assert test_box.head() == moved
    assert test_box.called("docker compose up")
    assert not test_box.called("wait-for-health")
    assert test_box.gate("rollback 9 deploy").returncode == 0
    assert test_box.called("rollback")


# ── rollback: decided here, once ─────────────────────────────────────────────


def _record(box: Box, run_id: str, finished: float) -> None:
    (box.app / ".last-deploy").write_text(f"run_id={run_id}\nsha={box.head()}\nfinished={int(finished)}\n")


MARKER_77 = "run 77 started 2026-09-24T00:00:00Z\n"


def test_a_deploy_that_failed_after_it_began_is_rolled_back(box):
    (box.app / ".deploy-in-progress").write_text(MARKER_77)
    result = box.gate("rollback 77 deploy")
    assert result.returncode == 0, result.stdout
    assert box.called("flock -w")
    assert box.called(f"rollback APP_DIR={box.app} gc.autoDetach=false marker=yes")
    assert not (box.app / ".deploy-in-progress").exists()


@pytest.mark.parametrize("failed", ["deploy", "checks"])
def test_another_runs_marker_is_refused(box, failed):
    (box.app / ".deploy-in-progress").write_text("run 76 started 2026-09-24T00:00:00Z\n")
    _record(box, "77", time.time() - 60)
    result = box.gate(f"rollback 77 {failed}")
    assert result.returncode == 1
    assert "(run 76 started" in result.stdout
    assert not box.called("rollback")
    assert (box.app / ".deploy-in-progress").exists()


def test_a_failed_deploy_never_rolls_back_a_finished_one(box):
    # A re-run keeps its run id. When the re-run's deploy refuses in a
    # pre-flight, the first attempt's healthy deploy is what is running. The
    # gate cannot tell that from a deploy whose caller went away, so it acts on
    # neither and says so.
    _record(box, "77", time.time() - 60)
    result = box.gate("rollback 77 deploy")
    assert result.returncode == 1
    assert "nothing was rolled back" in result.stdout
    assert not box.called("rollback")
    assert (box.app / ".last-deploy").exists()


def test_a_finished_deploy_whose_checks_failed_is_rolled_back(box):
    _record(box, "77", time.time() - 60)
    assert box.gate("rollback 77 checks").returncode == 0
    # Marked while it ran, so a rollback cut off leaves the box refusing deploys.
    assert box.called("rollback")[0].endswith(" marker=yes")
    assert not (box.app / ".last-deploy").exists()
    assert not (box.app / ".deploy-in-progress").exists()


def test_checks_never_roll_back_a_deploy_in_flight(box):
    (box.app / ".deploy-in-progress").write_text(MARKER_77)
    result = box.gate("rollback 77 checks")
    assert result.returncode == 1
    assert not box.called("rollback")


@pytest.mark.parametrize("age_s", [91 * 60, -600], ids=["outside the window", "from the future"])
def test_a_late_verdict_on_the_running_deploy_refuses_loudly(box, age_s):
    # Nothing is rolled back, but the run goes red: its deploy is still what
    # runs and its checks said it is broken.
    _record(box, "77", time.time() - age_s)
    result = box.gate("rollback 77 checks")
    assert result.returncode == 1
    assert "too long for its checks to roll it back" in result.stdout
    assert not box.called("rollback")


def test_checks_never_roll_back_another_runs_deploy(box):
    _record(box, "78", time.time() - 60)
    result = box.gate("rollback 77 checks")
    assert result.returncode == 0
    assert "nothing to roll back" in result.stdout
    assert not box.called("rollback")


@pytest.mark.parametrize("failed", ["deploy", "checks"])
def test_a_rollback_with_nothing_to_undo_is_a_no_op(box, failed):
    result = box.gate(f"rollback 77 {failed}")
    assert result.returncode == 0
    assert not box.called("rollback")


@pytest.mark.parametrize("failed", ["deploy", "checks"])
def test_an_empty_marker_is_still_a_marker(box, failed):
    # deploy.sh refuses on the file alone, so the gate reads it the same way.
    (box.app / ".deploy-in-progress").write_text("")
    _record(box, "77", time.time() - 60)
    result = box.gate(f"rollback 77 {failed}")
    assert result.returncode == 1
    assert "an empty marker" in result.stdout
    assert not box.called("rollback")
    assert (box.app / ".deploy-in-progress").exists()


@pytest.mark.parametrize("request_text", ["rollback 77", "rollback 77 smoke", "rollback 77 checks now"])
def test_a_rollback_says_what_failed(box, request_text):
    (box.app / ".deploy-in-progress").write_text(MARKER_77)
    assert box.gate(request_text).returncode == 1
    assert not box.called("rollback")
    assert not box.called("flock")


def test_a_rollback_that_leaves_the_database_ahead_stays_red(box):
    box.set("rollback-rc", "2")
    (box.app / ".deploy-in-progress").write_text(MARKER_77)
    result = box.gate("rollback 77 deploy")
    assert result.returncode == 2
    assert "database downgrade is outstanding" in result.stdout
    assert not (box.app / ".deploy-in-progress").exists()


def test_a_failed_rollback_keeps_the_marker(box):
    box.set("rollback-rc", "1")
    (box.app / ".deploy-in-progress").write_text(MARKER_77)
    result = box.gate("rollback 77 deploy")
    assert result.returncode == 1
    assert "Rollback FAILED" in result.stdout
    assert (box.app / ".deploy-in-progress").read_text() == MARKER_77


def test_a_failed_rollback_from_the_record_marks_the_box(box, moved):
    # With no marker to keep, the next deploy would snapshot the half-restored
    # box as its rollback point.
    box.set("rollback-rc", "1")
    _record(box, "77", time.time() - 60)
    assert box.gate("rollback 77 checks").returncode == 1
    assert (box.app / ".deploy-in-progress").read_text().startswith("run 77 rolling back since ")
    result = box.gate("deploy 124")
    assert result.returncode == 1
    assert "is still present" in result.stdout


def test_a_tree_changed_since_the_deploy_is_not_rolled_back(box, moved):
    # Someone has fixed the box by hand since; the run's late verdict is about
    # a deploy that is no longer there.
    _record(box, "77", time.time() - 60)
    _git(box.app, "fetch", "-q", "origin")
    _git(box.app, "reset", "-q", "--hard", moved)
    result = box.gate("rollback 77 checks")
    assert result.returncode == 0
    assert "nothing to roll back" in result.stdout
    assert not box.called("rollback")


def test_a_deploy_that_retakes_the_rollback_point_ends_the_last_ones_window(box):
    # pre-deploy.sh has retagged :rollback by the time it fails, so the last
    # deploy's run can no longer roll back to what it replaced.
    _record(box, "77", time.time() - 60)
    (box.app / "deploy" / "pre-deploy.sh").write_text("#!/bin/bash\nexit 1\n")
    box.push({"VERSION": "second\n"})
    assert box.gate("deploy 78").returncode == 1
    assert not (box.app / ".last-deploy").exists()
    assert "nothing to roll back" in box.gate("rollback 77 checks").stdout


@pytest.mark.parametrize("failed", ["deploy", "checks"])
def test_a_rollback_cut_off_can_be_resumed(box, failed):
    (box.app / ".deploy-in-progress").write_text("run 77 rolling back since 2026-09-24T00:00:00Z\n")
    result = box.gate(f"rollback 77 {failed}")
    assert result.returncode == 0, result.stdout
    assert "resuming the rollback it began" in result.stdout
    assert box.called("rollback")
    assert not (box.app / ".deploy-in-progress").exists()


def test_one_run_rolls_back_once(box):
    _record(box, "77", time.time() - 60)
    assert box.gate("rollback 77 checks").returncode == 0
    assert box.gate("rollback 77 checks").returncode == 0
    assert len(box.called("rollback")) == 1


# ── status and probe-towers ──────────────────────────────────────────────────


def test_status_reports_the_deployed_commit_and_the_deploy_state(box):
    _record(box, "77", 1_000)
    (box.app / ".deploy-in-progress").write_text("run 78 started x\n")
    lines = box.gate("status").stdout.splitlines()
    assert "env=prod" in lines
    assert f"head={box.head()}" in lines
    assert "marker=run 78 started x" in lines
    assert "last_deploy_run_id=77" in lines


def test_the_towers_probe_passes_on_a_service_that_honours_the_contract(box):
    result = box.gate("probe-towers")
    assert result.returncode == 0, result.stdout
    runs = box.called("docker run")
    assert len(runs) == 3
    assert all("--network retina-edge" in line and "curlimages/curl:" in line for line in runs)
    assert all("@sha256:" in line and ":latest" not in line for line in runs)


def test_the_towers_probe_asks_the_contract_main_will_deploy(box):
    # main now asks for a key the service does not return; the box still runs
    # the tree before it, whose contract the service meets.
    contract = TOWER_CONTRACT_SH.read_text().replace(
        '"broadcast_bands" "search"', '"broadcast_bands" "search" "coming"'
    )
    assert contract != TOWER_CONTRACT_SH.read_text()
    before = box.head()
    box.push({"deploy/tower-contract.sh": contract})
    result = box.gate("probe-towers")
    assert result.returncode == 1
    assert 'answered /api/config without "coming"' in result.stdout
    assert box.head() == before


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        ("no-edge", "", "retina-edge does not exist"),
        ("towers", '{"towers":[]}', "answered without"),
        ("config", '{"ranking":{}}', 'answered /api/config without "receiver"'),
        ("geocode", "404", "with HTTP 404"),
    ],
)
def test_the_towers_probe_names_each_breach(box, name, value, reason):
    box.set(name, value)
    result = box.gate("probe-towers")
    assert result.returncode == 1
    assert reason in result.stdout


# ── Installing the gate ──────────────────────────────────────────────────────


def test_install_puts_an_executable_gate_in_place(box):
    source = box.root / "gate.sh"
    result = subprocess.run(  # noqa: S603, S607
        ["bash", str(source), "--install"], capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr
    assert box.installed_gate.read_text() == source.read_text()
    assert os.access(box.installed_gate, os.X_OK)
    assert list(box.installed_gate.parent.iterdir()) == [box.installed_gate]


def test_an_install_that_fails_leaves_the_gate_and_nothing_beside_it(box):
    box.installed_gate.write_text("the gate already there\n")
    # The stand-in for /usr/local/sbin, made one the install cannot write to.
    box.installed_gate.parent.chmod(0o555)
    try:
        result = subprocess.run(  # noqa: S603, S607
            ["bash", str(box.root / "gate.sh"), "--install"], capture_output=True, text=True, check=False
        )
    finally:
        box.installed_gate.parent.chmod(0o755)
    assert result.returncode == 1
    assert "the gate already there is unchanged" in result.stdout
    assert box.installed_gate.read_text() == "the gate already there\n"
    assert list(box.installed_gate.parent.iterdir()) == [box.installed_gate]


# ── setup-server.sh installs the gate ────────────────────────────────────────


def _install_block(box: Box, app_dir: Path) -> subprocess.CompletedProcess:
    """setup-server.sh's gate install, run for app_dir with box.app standing in
    for /opt/retina-server."""
    text = SETUP_SH.read_text()
    block = text[text.index("# The deploy gate, the one command") : text.index("# The origin boundary.")]
    block = _pointed(block, ("!= /opt/retina-server", f"!= {box.app}"))
    return subprocess.run(  # noqa: S603, S607
        ["bash", "-c", f'set -euo pipefail\nAPP_DIR="$1"\n{block}\necho "provisioning carried on"', "-", str(app_dir)],
        env={"PATH": f"{box.root / 'bin'}:{os.environ['PATH']}", "HOME": str(box.root / "home")},
        capture_output=True,
        text=True,
        check=False,
    )


def test_setup_installs_mains_gate_whatever_the_tree_holds(box):
    (box.app / "deploy" / "gate.sh").write_text("#!/bin/bash\necho the tree's gate\n")
    result = _install_block(box, box.app)
    assert result.returncode == 0, result.stdout + result.stderr
    assert box.installed_gate.read_text() == box.gate_copy
    assert "provisioning carried on" in result.stdout


def test_setup_carries_on_when_the_gate_will_not_install(box):
    box.push({"deploy/gate.sh": "if then\n"})
    _git(box.app, "fetch", "-q", "origin")
    result = _install_block(box, box.app)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "The deploy gate was not installed" in result.stdout
    assert "provisioning carried on" in result.stdout
    assert not box.installed_gate.exists()


def test_setup_installs_no_gate_for_another_directory(box):
    other = box.root / "elsewhere"
    other.mkdir()
    result = _install_block(box, other)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "it serves" in result.stdout
    assert not box.installed_gate.exists()


def test_setup_installs_no_gate_where_there_is_no_clone(box):
    shutil.rmtree(box.app / ".git")
    result = _install_block(box, box.app)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "is not a git clone" in result.stdout
    assert not box.installed_gate.exists()
