"""The query-parameter rules the public archive routes share."""

import importlib

import pytest

BBOX_DETAIL = "bbox must be south,west,north,east in degrees, with south <= north"


def test_shared_instant_rule_parses_ms_and_iso_and_rejects_garbage():
    from routes.archive_common import parse_instant_ms

    assert parse_instant_ms("1700000000123", "from") == 1_700_000_000_123
    assert parse_instant_ms("2023-11-14T22:13:20Z", "from") == 1_700_000_000_000
    with pytest.raises(Exception) as caught:
        parse_instant_ms("yesterday", "to")
    assert caught.value.status_code == 422
    assert caught.value.detail == "to must be epoch milliseconds or an ISO-8601 time"


def test_shared_bbox_rule_parses_corners_and_rejects_bad_boxes():
    from routes.archive_common import parse_bbox

    box = parse_bbox("-10,20.5,30,40")
    assert (box.south, box.west, box.north, box.east) == (-10.0, 20.5, 30.0, 40.0)
    for bad in ("1,2,3", "30,0,10,1", "a,b,c,d", "0,-181,1,1"):
        with pytest.raises(Exception) as caught:
            parse_bbox(bad)
        assert caught.value.status_code == 422
        assert caught.value.detail == BBOX_DETAIL


def test_shared_invalid_parameter_is_a_422_with_the_given_detail():
    from routes.archive_common import invalid_parameter

    error = invalid_parameter("to must be after from")
    assert error.status_code == 422
    assert error.detail == "to must be after from"


def test_both_archive_routes_use_the_shared_parameter_rules():
    common = importlib.import_module("routes.archive_common")
    tracks = importlib.import_module("routes.tracks_archive")
    detections = importlib.import_module("routes.detections_archive")
    for module in (tracks, detections):
        assert module.parse_bbox is common.parse_bbox
        assert module.parse_instant_ms is common.parse_instant_ms
        assert module.invalid_parameter is common.invalid_parameter
        assert module.Fleet is common.Fleet
        for private in ("_bbox", "_instant_ms", "_invalid"):
            assert not hasattr(module, private)
