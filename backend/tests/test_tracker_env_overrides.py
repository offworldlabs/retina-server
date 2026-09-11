"""Tests for the server-side TRACKER_PROCESS_NOISE_* env overrides.

The tracker's Kalman filter reads its process noise from retina-tracker's
module-level config singleton, so these tests assert on that singleton (and on
a filter built from it) rather than only on the dict the pipeline passes to the
Tracker. The singleton is process-global state shared with every other test in
the session, so it is snapshotted and restored around each test here.
"""

import logging

import pytest
import retina_tracker
import retina_tracker.config as rt_config
from retina_tracker.kalman import KalmanFilter

import pipeline.passive_radar as passive_radar
from pipeline.passive_radar import DEFAULT_NODE_CONFIG, PassiveRadarPipeline

_ENV_VARS = ("TRACKER_PROCESS_NOISE_DOPPLER", "TRACKER_PROCESS_NOISE_DELAY")

# What the packaged retina-tracker config.yaml ships, and what the library
# falls back to when no config is loaded at all.
PACKAGED_DOPPLER = 0.5
PACKAGED_DELAY = 0.1


@pytest.fixture(autouse=True)
def _isolate_tracker_globals(monkeypatch):
    """Restore the library singleton and the log-once flag after each test."""
    saved = rt_config._config
    # An ambient value in the developer's shell would otherwise decide the
    # "no override" cases, so clear both regardless of what the test does.
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(passive_radar, "_tracker_env_override_logged", False)
    yield
    rt_config.set_config(saved)


# ── Helper ───────────────────────────────────────────────────────────────────


class TestApplyTrackerEnvOverrides:
    def test_missing_process_noise_section_is_created(self, monkeypatch):
        monkeypatch.setenv("TRACKER_PROCESS_NOISE_DOPPLER", "20")
        cfg = {"tracker": {"m_threshold": 3}}

        out = passive_radar._apply_tracker_env_overrides(cfg)

        assert out is cfg
        assert cfg["process_noise"] == {"doppler": 20.0}
        # Untouched sections survive.
        assert cfg["tracker"] == {"m_threshold": 3}

    def test_no_env_leaves_config_untouched(self):
        cfg = {"process_noise": {"doppler": 0.5, "delay": 0.1}}

        passive_radar._apply_tracker_env_overrides(cfg)

        assert cfg == {"process_noise": {"doppler": 0.5, "delay": 0.1}}


# ── Through the pipeline ─────────────────────────────────────────────────────


class TestPipelineProcessNoise:
    def test_no_env_keeps_packaged_defaults(self):
        PassiveRadarPipeline(DEFAULT_NODE_CONFIG)

        assert rt_config.PROCESS_NOISE_DOPPLER() == PACKAGED_DOPPLER
        assert rt_config.PROCESS_NOISE_DELAY() == PACKAGED_DELAY

    def test_doppler_override_reaches_the_filter(self, monkeypatch, caplog):
        monkeypatch.setenv("TRACKER_PROCESS_NOISE_DOPPLER", "20")

        with caplog.at_level(logging.INFO, logger=passive_radar.__name__):
            p = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)

        assert rt_config.PROCESS_NOISE_DOPPLER() == 20.0
        # Delay is not named by the env, so it keeps the packaged value.
        assert rt_config.PROCESS_NOISE_DELAY() == PACKAGED_DELAY
        # A filter built after construction sees it: Q[3,3] is q_doppler * dt.
        assert KalmanFilter(dt=1.0).Q[3, 3] == pytest.approx(20.0)
        # And the per-instance dict the Tracker holds carries it too.
        assert p.tracker.config["process_noise"]["doppler"] == 20.0
        assert "TRACKER_PROCESS_NOISE_DOPPLER" in caplog.text

    def test_delay_override_reaches_the_filter(self, monkeypatch):
        monkeypatch.setenv("TRACKER_PROCESS_NOISE_DELAY", "3.5")

        p = PassiveRadarPipeline(DEFAULT_NODE_CONFIG)

        assert rt_config.PROCESS_NOISE_DELAY() == 3.5
        assert rt_config.PROCESS_NOISE_DOPPLER() == PACKAGED_DOPPLER
        assert KalmanFilter(dt=1.0).Q[1, 1] == pytest.approx(3.5)
        assert p.tracker.config["process_noise"]["delay"] == 3.5

    def test_logged_once_per_process(self, monkeypatch, caplog):
        monkeypatch.setenv("TRACKER_PROCESS_NOISE_DOPPLER", "20")

        with caplog.at_level(logging.INFO, logger=passive_radar.__name__):
            for _ in range(3):
                PassiveRadarPipeline(DEFAULT_NODE_CONFIG)

        assert caplog.text.count("tracker process noise:") == 1

    @pytest.mark.parametrize("bad", ["abc", "0", "-3", "", "   ", "nan", "inf"])
    def test_invalid_values_are_ignored_with_a_warning(self, monkeypatch, caplog, bad):
        monkeypatch.setenv("TRACKER_PROCESS_NOISE_DOPPLER", bad)

        with caplog.at_level(logging.WARNING, logger=passive_radar.__name__):
            PassiveRadarPipeline(DEFAULT_NODE_CONFIG)

        assert rt_config.PROCESS_NOISE_DOPPLER() == PACKAGED_DOPPLER
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("TRACKER_PROCESS_NOISE_DOPPLER" in r.getMessage() for r in warnings)

    def test_override_applies_without_a_packaged_config(self, monkeypatch):
        """The global singleton still moves when config.yaml is not found."""
        monkeypatch.setenv("TRACKER_PROCESS_NOISE_DOPPLER", "20")
        # Point the package lookup at a path that has no config.yaml beside it,
        # which is the branch that used to skip set_config() entirely.
        monkeypatch.setattr(retina_tracker, "__file__", "/nonexistent/retina_tracker/__init__.py")
        rt_config.set_config({"process_noise": {"doppler": PACKAGED_DOPPLER}})

        PassiveRadarPipeline(DEFAULT_NODE_CONFIG)

        assert rt_config.PROCESS_NOISE_DOPPLER() == 20.0
