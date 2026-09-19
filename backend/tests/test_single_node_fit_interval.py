"""Deployment cadence cannot disable fits with invalid environment values."""

import pytest

from config.constants import _single_node_geo_interval_s


@pytest.mark.parametrize(
    "raw, expected", [("20", 20), ("0.5", 0.5), ("0", 10), ("-1", 10), ("nan", 10), ("inf", 10), ("bad", 10)]
)
def test_single_node_fit_interval(monkeypatch, raw, expected):
    monkeypatch.setenv("SINGLE_NODE_GEO_INTERVAL_S", raw)
    assert _single_node_geo_interval_s() == expected


def test_single_node_fit_interval_default(monkeypatch):
    monkeypatch.delenv("SINGLE_NODE_GEO_INTERVAL_S", raising=False)
    assert _single_node_geo_interval_s() == 10
