"""deploy/setup-server.sh states which image store Docker uses.

Docker's two stores cannot see each other's images, so the store has to be
settled before anything is built, and a re-run on a live box must leave it where
it is. buildx goes only where the store is containerd, since on overlay2 its
Bake build leaves the old container running. Nothing in CI or the deploys runs
this script, so these run its Docker
section against stubbed `docker`, `apt-get` and `systemctl`, and read the whole
script for the ordering.
"""

import json
import re
import subprocess
from pathlib import Path

import pytest

SETUP_SH = Path(__file__).resolve().parents[2] / "deploy" / "setup-server.sh"

# A banner's opening only, as in test_start_script_guards.py.
_SECTION = re.compile(r"^# ── ", re.MULTILINE)

LOG_OPTIONS = {"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}}

# Shell functions rather than executables on PATH: they shadow the commands just
# the same, and macOS scans every newly written executable on its first run,
# which costs seconds per stub.
_STUBS = r"""
# The daemon starting: it honours daemon.json, as Docker does, unless
# STUB_STORE forces a store regardless of it or STUB_DOWN leaves it silent.
stub_dockerd() {
    if [ -n "${STUB_DOWN:-}" ]; then
        rm -f "$STATE/store"
    elif [ -n "${STUB_STORE:-}" ]; then
        echo "$STUB_STORE" > "$STATE/store"
    elif grep -q '"containerd-snapshotter": *false' "$ETC/daemon.json" 2>/dev/null; then
        echo overlay2 > "$STATE/store"
    else
        echo containerd > "$STATE/store"
    fi
}
# `docker info` as the real CLI reports each store, failing when no daemon runs.
docker() {
    [ -f "$STATE/store" ] || { echo "Cannot connect to the Docker daemon" >&2; return 1; }
    [ "$1" = info ] || return 0
    local status='[["Backing Filesystem","extfs"],["Supports d_type","true"]]'
    if [ "$(cat "$STATE/store")" = containerd ]; then
        status='[["driver-type","io.containerd.snapshotter.v1"]]'
    fi
    if [ "${2:-}" = --format ]; then echo "$status"; else echo "Server Version: 29.1.3"; fi
}
# Installing docker.io starts a daemon only where none is running yet.
apt-get() {
    echo "$*" >> "$STATE/apt.log"
    if [ -f "$ETC/daemon.json" ]; then echo "daemon.json present" >> "$STATE/apt.log"; fi
    case " $* " in *" docker.io "*) [ -f "$STATE/store" ] || stub_dockerd ;; esac
    case "$* " in
        "install "*" docker-buildx "*) touch "$STATE/buildx" ;;
        "remove "*" docker-buildx "*) rm -f "$STATE/buildx" ;;
    esac
}
dpkg-query() {
    [ -f "$STATE/buildx" ] || return 1
    echo "install ok installed"
}
systemctl() {
    echo "$*" >> "$STATE/systemctl.log"
    if [ "$*" = "restart docker" ]; then stub_dockerd; fi
}
"""


def _docker_section() -> str:
    text = SETUP_SH.read_text()
    starts = [m.start() for m in _SECTION.finditer(text)]
    banners = [i for i in starts if text[i:].startswith("# ── 3. Install Docker")]
    assert len(banners) == 1, "deploy/setup-server.sh has no single Install Docker section"
    after = [i for i in starts if i > banners[0]]
    return text[banners[0] : after[0] if after else len(text)]


class Box:
    """A droplet reduced to what the Docker section touches."""

    def __init__(self, root: Path, store: str | None = None, docker_data: bool = False, buildx: bool = False):
        self.state = root / "state"
        self.etc = root / "etc-docker"
        self.lib = root / "var-lib-docker"
        self.state.mkdir()
        if store:
            (self.state / "store").write_text(store + "\n")
        if store or docker_data:
            self.lib.mkdir()
        if buildx:
            (self.state / "buildx").touch()

    def run(self, **env) -> subprocess.CompletedProcess:
        section = _docker_section()
        # A renamed path would leave the section writing to the host's real one.
        assert "/etc/docker" in section and "/var/lib/docker" in section
        section = section.replace("/etc/docker", str(self.etc)).replace("/var/lib/docker", str(self.lib))
        return subprocess.run(
            ["bash", "-c", "set -euo pipefail\n" + _STUBS + section],
            env={"PATH": "/usr/bin:/bin", "STATE": str(self.state), "ETC": str(self.etc), **env},
            capture_output=True,
            text=True,
        )

    def daemon_json(self) -> dict:
        return json.loads((self.etc / "daemon.json").read_text())

    def log(self, name: str) -> list[str]:
        path = self.state / f"{name}.log"
        return path.read_text().splitlines() if path.exists() else []

    @property
    def store(self) -> str:
        return (self.state / "store").read_text().strip()

    @property
    def has_buildx(self) -> bool:
        return (self.state / "buildx").exists()


def test_a_fresh_box_gets_the_containerd_store(tmp_path):
    box = Box(tmp_path)
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.daemon_json()["features"] == {"containerd-snapshotter": True}
    assert box.store == "containerd"


def test_the_store_is_stated_before_the_daemon_first_starts(tmp_path):
    # The package starts the daemon on install, so a daemon.json written after
    # it would leave the first start, and anything built before a restart, on
    # Docker's own choice.
    box = Box(tmp_path)
    assert box.run().returncode == 0
    assert "daemon.json present" in box.log("apt")


@pytest.mark.parametrize("store", ["containerd", "overlay2"])
def test_a_rerun_keeps_the_store_a_running_daemon_is_on(tmp_path, store):
    # overlay2 is production: containerd there would hide every image it holds,
    # the rollback image included.
    box = Box(tmp_path, store=store)
    result = box.run()
    assert result.returncode == 0, result.stderr
    assert box.daemon_json()["features"] == {"containerd-snapshotter": store == "containerd"}
    assert box.store == store
    # The already-running daemon only reads daemon.json again on a restart.
    assert "restart docker" in box.log("systemctl")


def test_docker_data_with_no_daemon_answering_is_refused(tmp_path):
    box = Box(tmp_path, docker_data=True)
    result = box.run()
    assert result.returncode != 0
    assert "no Docker daemon is answering" in result.stderr
    assert not (box.etc / "daemon.json").exists()
    assert box.log("apt") == []


def test_a_daemon_that_comes_up_on_the_other_store_stops_the_script(tmp_path):
    box = Box(tmp_path)
    result = box.run(STUB_STORE="overlay2")
    assert result.returncode != 0
    assert "Stopping before anything is built" in result.stderr


def test_a_daemon_that_stops_answering_after_the_restart_stops_the_script(tmp_path):
    # overlay2 especially: a silent daemon must not read as the classic store.
    box = Box(tmp_path, store="overlay2")
    result = box.run(STUB_DOWN="1")
    assert result.returncode != 0
    assert "no answer" in result.stderr


def test_the_log_options_are_kept(tmp_path):
    box = Box(tmp_path)
    assert box.run().returncode == 0
    config = box.daemon_json()
    assert {key: config[key] for key in LOG_OPTIONS} == LOG_OPTIONS


@pytest.mark.parametrize("store", [None, "containerd"], ids=["fresh", "containerd"])
def test_buildx_is_installed_from_ubuntus_package_on_the_containerd_store(tmp_path, store):
    box = Box(tmp_path, store=store)
    assert box.run().returncode == 0
    installs = [line.split() for line in box.log("apt") if line.split()[:1] == ["install"]]
    assert any("docker-buildx" in words for words in installs)
    assert box.has_buildx


@pytest.mark.parametrize("buildx", [False, True], ids=["absent", "present"])
def test_an_overlay2_box_is_left_without_buildx(tmp_path, buildx):
    # There `up --build` tags the new image but keeps the old container running,
    # so a deploy passes on stale code.
    box = Box(tmp_path, store="overlay2", buildx=buildx)
    assert box.run().returncode == 0
    assert not box.has_buildx
    assert not [line for line in box.log("apt") if line.split()[:1] == ["install"] and "docker-buildx" in line]


# Anything that puts an image in the store, and anything that can change which
# store the daemon is on.
BUILDS = re.compile(r"\bdocker\s+(?:compose\b.*\b(?:up|build|pull|create|run)\b|build\b|pull\b|load\b|run\b)")
STORE_CHANGES = re.compile(r"/etc/docker/daemon\.json|\b(?:systemctl|service)\s.*\bdocker\b(?!-)|\bdockerd\b")


def test_nothing_touches_the_store_setting_once_anything_is_built():
    # A restart onto a different store after the first build would hide it.
    lines = [line for line in SETUP_SH.read_text().splitlines() if line.strip() and not line.lstrip().startswith("#")]
    builds = [i for i, line in enumerate(lines) if BUILDS.search(line)]
    changes = [i for i, line in enumerate(lines) if STORE_CHANGES.search(line)]
    assert builds and changes
    assert max(changes) < min(builds)
