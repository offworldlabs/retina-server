"""deploy/start.sh refuses to boot production without an alert destination or
a JWT secret, and lets uvicorn finish shutting down when the container stops.

The alert guard is the only thing standing between a restored box and a
production stack that runs unalerting while reading as healthy. The JWT_SECRET
check stops a box that cannot serve before it migrates. So these extract each
block from the script as it stands and run it, rather than asserting on its
text. The supervisor is run the same way, with stand-ins for nginx and uvicorn.
"""

import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

START_SH = Path(__file__).resolve().parents[2] / "deploy" / "start.sh"

# Matches a section banner's opening only. Deliberately not the whole line: a
# banner that loses its trailing dashes would otherwise stop terminating the
# slice, which would silently extend it over the sections that follow and fail
# every test here for reasons that have nothing to do with the guard.
_SECTION = re.compile(r"^# ── ", re.MULTILINE)

_SET = {"ALERT_WEBHOOK_URL": "https://example.invalid/messages", "ALERT_WEBHOOK_AUTH": "pk_1"}

# services/alerting.py strips before deciding a value is present, so a key
# holding only a stray space is absent as far as alerting is concerned.
_BLANK = "   "


def _guard(section: str = "Alerting guard") -> str:
    text = START_SH.read_text()
    starts = [m.start() for m in _SECTION.finditer(text)]
    banners = [i for i in starts if text[i:].startswith(f"# ── {section}")]
    assert len(banners) == 1, f"deploy/start.sh has no single {section} section"
    begin = banners[0]
    after = [i for i in starts if i > begin]
    return text[begin : after[0] if after else len(text)]


def _run(section: str = "Alerting guard", **env) -> subprocess.CompletedProcess:
    """The guard alone, under the same shell options the real script sets."""
    return subprocess.run(
        ["bash", "-c", "set -e\n" + _guard(section)],
        env={"PATH": "/usr/bin:/bin", **env},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("missing", ["ALERT_WEBHOOK_URL", "ALERT_WEBHOOK_AUTH"])
def test_production_refuses_to_boot_without_an_alert_destination(missing):
    env = {"RETINA_ENV": "production", **_SET}
    del env[missing]
    result = _run(**env)
    assert result.returncode != 0
    assert missing in result.stderr
    assert "runbook" in result.stderr


@pytest.mark.parametrize("value", ["", _BLANK])
@pytest.mark.parametrize("missing", ["ALERT_WEBHOOK_URL", "ALERT_WEBHOOK_AUTH"])
def test_production_treats_an_empty_or_blank_value_as_absent(missing, value):
    # A key left in place with nothing (or a stray space) after the `=` is what
    # a half-finished edit on the host leaves behind, and alerting.py reads
    # both as disabled.
    result = _run(RETINA_ENV="production", **{**_SET, missing: value})
    assert result.returncode != 0
    assert missing in result.stderr


def test_production_boots_when_both_are_set():
    result = _run(RETINA_ENV="production", DIGITALOCEAN_READ_TOKEN="dop_v1_1", **_SET)
    assert result.returncode == 0, result.stderr
    assert "ALERT_WEBHOOK_URL" not in result.stderr
    assert "ALERT_WEBHOOK_AUTH" not in result.stderr


@pytest.mark.parametrize("env_name", ["staging", "test"])
def test_the_other_environments_warn_rather_than_refuse(env_name):
    # Losing their alerts costs visibility into a box no user depends on, which
    # is not worth trading for an outage.
    result = _run(RETINA_ENV=env_name, DIGITALOCEAN_READ_TOKEN="dop_v1_1")
    assert result.returncode == 0, result.stderr
    assert "ALERT_WEBHOOK_URL is not set" in result.stderr


@pytest.mark.parametrize("value", ["", _BLANK])
@pytest.mark.parametrize("env_name", ["staging", "test"])
def test_the_other_environments_warn_about_a_destination_they_cannot_authenticate(env_name, value):
    # A URL without a token is the worse half-finished edit of the two: alerts
    # are attempted and every one of them 401s, so nothing announces itself.
    result = _run(
        RETINA_ENV=env_name,
        DIGITALOCEAN_READ_TOKEN="dop_v1_1",
        **{**_SET, "ALERT_WEBHOOK_AUTH": value},
    )
    assert result.returncode == 0, result.stderr
    assert "ALERT_WEBHOOK_AUTH is not set" in result.stderr


@pytest.mark.parametrize("env_name", ["staging", "test"])
def test_a_box_with_no_destination_is_not_also_told_about_the_token(env_name):
    # Without a URL nothing is posted at all, so the token is not the thing to
    # act on and naming it would only compete with the line that is.
    result = _run(RETINA_ENV=env_name, DIGITALOCEAN_READ_TOKEN="dop_v1_1")
    assert "ALERT_WEBHOOK_AUTH" not in result.stderr


@pytest.mark.parametrize("env_name", ["production", "staging"])
def test_a_missing_digitalocean_token_never_refuses_a_boot(env_name):
    result = _run(RETINA_ENV=env_name, **_SET)
    assert result.returncode == 0, result.stderr
    assert "DIGITALOCEAN_READ_TOKEN is not set" in result.stderr


# ── JWT_SECRET ───────────────────────────────────────────────────────────────


def _environment(**env) -> subprocess.CompletedProcess:
    return _run("Environment guard", HOST_MAIN="app.example.invalid", **env)


@pytest.mark.parametrize("value", [None, ""])
@pytest.mark.parametrize("env_name", ["production", "Production"])
def test_production_refuses_to_boot_without_a_jwt_secret(env_name, value):
    # core/users.py lowercases RETINA_ENV and takes an empty value as none.
    env = {"RETINA_ENV": env_name} | ({} if value is None else {"JWT_SECRET": value})
    result = _environment(**env)
    assert result.returncode != 0
    assert "JWT_SECRET" in result.stderr
    assert "backend/.env.example" in result.stderr


def test_production_boots_with_a_jwt_secret():
    result = _environment(RETINA_ENV="production", JWT_SECRET="s3cret")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("env_name", ["dev", "test", "TEST"])
def test_dev_and_test_boot_without_a_jwt_secret(env_name):
    # core/users.py falls back to a development secret there.
    result = _environment(RETINA_ENV=env_name)
    assert result.returncode == 0, result.stderr


# ── Stopping ─────────────────────────────────────────────────────────────────
#
# The image runs start.sh under tini, which exits the moment start.sh does; the
# kernel then kills everything left in the container. So a stop is graceful
# only if the supervisor outlives uvicorn's own shutdown, where the lifespan
# makes its final writes and the mail outbox records a send already made.

_NGINX = """#!/bin/sh
# Runs until TERM, then takes a moment over its shutdown.
stop=0
trap 'stop=1' TERM
touch "$STATE/nginx.started"
if [ -n "$NGINX_DIES" ]; then
  # Falls over on its own once uvicorn is up.
  until [ -e "$STATE/uvicorn.runs" ]; do sleep 0.05; done
  exit 1
fi
while [ "$stop" = 0 ]; do sleep 0.05; done
sleep 0.3
touch "$STATE/nginx.stopped"
"""

# The second it takes after TERM is the lifespan's shutdown.
_UVICORN = """#!/bin/sh
stop=0
trap 'stop=1' TERM
echo run >>"$STATE/uvicorn.runs"
if [ -n "$UVICORN_CRASHES" ]; then
  # Not before nginx is up, so a supervisor giving up has a running nginx to stop.
  until [ -e "$STATE/nginx.started" ]; do sleep 0.05; done
  exit 3
fi
while [ "$stop" = 0 ]; do sleep 0.05; done
touch "$STATE/uvicorn.stopping"
sleep 1
touch "$STATE/uvicorn.stopped"
"""


@pytest.fixture
def supervisor_bash() -> str:
    """A bash with `wait -n` over named jobs (5.1 or later), as the image has.

    macOS ships 3.2, so there this skips. Linux is where CI and the image run,
    and there an older bash fails the test instead of skipping it quietly.
    """
    bash = shutil.which("bash")
    version: tuple[int, ...] = ()
    if bash:
        probe = subprocess.run(
            [bash, "-c", 'echo "${BASH_VERSINFO[0]} ${BASH_VERSINFO[1]}"'], capture_output=True, text=True
        )
        version = tuple(int(part) for part in probe.stdout.split()[:2] if part.isdigit())
    if version < (5, 1):
        found = ".".join(map(str, version)) or "missing"
        reason = f"the supervisor needs bash 5.1 or later for `wait -n`, and {bash or 'bash'} is {found}"
        if sys.platform == "linux":
            pytest.fail(reason)
        pytest.skip(reason)
    return bash


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _until(condition, what: str, timeout: float = 15) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def _log(state: Path) -> str:
    return (state / "supervisor.log").read_text()


def _runs(state: Path) -> int:
    return (state / "uvicorn.runs").read_text().count("run")


@pytest.fixture
def supervise(tmp_path, supervisor_bash):
    """Start the supervisor section with stand-ins for nginx and uvicorn.

    Each run gets a process group of its own, so whatever it leaves running
    when a test fails is killed afterwards and nothing else is touched.
    """
    started: list[subprocess.Popen] = []

    def start(*pointed: tuple[str, str], **env: str) -> subprocess.Popen:
        stubs = tmp_path / "bin"
        _executable(stubs / "nginx", _NGINX)
        _executable(stubs / "uvicorn", _UVICORN)
        section = _guard("uvicorn supervision")
        # The image's working directory, which the stand-ins have no use for.
        for old, new in (("cd /app/backend\n", f"cd {shlex.quote(str(tmp_path))}\n"), *pointed):
            assert old in section, f"{old!r} is no longer in the supervisor; update this test"
            section = section.replace(old, new)
        with (tmp_path / "supervisor.log").open("w") as log:
            process = subprocess.Popen(
                [supervisor_bash, "-c", "set -e\n" + section],
                env={"PATH": f"{stubs}:/usr/bin:/bin", "STATE": str(tmp_path), **env},
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        started.append(process)
        _until(lambda: (tmp_path / "uvicorn.runs").exists(), "uvicorn to start")
        _until(lambda: (tmp_path / "nginx.started").exists(), "nginx to start")
        return process

    yield start
    for process in started:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        process.wait(timeout=10)


@pytest.mark.parametrize("again", [None, signal.SIGTERM, signal.SIGINT], ids=["once", "then-term", "then-int"])
def test_a_stop_waits_for_uvicorn_and_nginx_to_finish_shutting_down(supervise, tmp_path, again):
    process = supervise()
    process.send_signal(signal.SIGTERM)
    if again is not None:
        # A second signal while the supervisor waits must not end the wait.
        _until(lambda: (tmp_path / "uvicorn.stopping").exists(), "uvicorn to begin stopping")
        _until(lambda: "shutting down" in _log(tmp_path), "the supervisor to stop restarting")
        process.send_signal(again)
    assert process.wait(timeout=30) == 0, _log(tmp_path)
    # Read the moment the supervisor has returned, which in the container is
    # the moment anything still running is killed.
    assert (tmp_path / "uvicorn.stopped").exists(), _log(tmp_path)
    assert (tmp_path / "nginx.stopped").exists(), _log(tmp_path)
    assert "uvicorn and nginx have exited" in _log(tmp_path)
    assert _runs(tmp_path) == 1


def test_a_stop_during_the_restart_backoff_starts_nothing(supervise, tmp_path):
    # A fresh uvicorn would only be booted into its own shutdown, spending the
    # stop's grace period on a cold start.
    process = supervise(UVICORN_CRASHES="1")
    _until(lambda: "restarting uvicorn in 2s" in _log(tmp_path), "the restart backoff")
    runs = _runs(tmp_path)
    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=30) == 0, _log(tmp_path)
    assert _runs(tmp_path) == runs, _log(tmp_path)
    assert "nginx is no longer running" not in _log(tmp_path)
    assert (tmp_path / "nginx.stopped").exists(), _log(tmp_path)


def test_nginx_dying_still_recreates_the_container(supervise, tmp_path):
    process = supervise(NGINX_DIES="1")
    assert process.wait(timeout=30) == 1, _log(tmp_path)
    assert "nginx exited (code 1); stopping uvicorn" in _log(tmp_path)
    assert (tmp_path / "uvicorn.stopped").exists(), _log(tmp_path)
    assert _runs(tmp_path) == 1


def test_a_fast_crash_loop_still_gives_up(supervise, tmp_path):
    # One fast failure in place of five, which is the same path sooner.
    process = supervise(("MAX_FAST_FAILURES=5\n", "MAX_FAST_FAILURES=1\n"), UVICORN_CRASHES="1")
    assert process.wait(timeout=30) == 1, _log(tmp_path)
    assert "1 consecutive fast failures; stopping nginx and exiting" in _log(tmp_path)
    assert (tmp_path / "nginx.stopped").exists(), _log(tmp_path)
    assert _runs(tmp_path) == 1
