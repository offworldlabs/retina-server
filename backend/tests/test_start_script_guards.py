"""deploy/start.sh refuses to boot production without an alert destination.

The guard is the only thing standing between a restored box and a production
stack that runs unalerting while reading as healthy, so these extract the block
from the script as it stands and run it, rather than asserting on its text.
"""

import re
import subprocess
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


def _guard() -> str:
    text = START_SH.read_text()
    starts = [m.start() for m in _SECTION.finditer(text)]
    banners = [i for i in starts if text[i:].startswith("# ── Alerting guard")]
    assert len(banners) == 1, "deploy/start.sh has no single Alerting guard section"
    begin = banners[0]
    after = [i for i in starts if i > begin]
    return text[begin : after[0] if after else len(text)]


def _run(**env) -> subprocess.CompletedProcess:
    """The guard alone, under the same shell options the real script sets."""
    return subprocess.run(
        ["bash", "-c", "set -e\n" + _guard()],
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
