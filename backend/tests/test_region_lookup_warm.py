"""The border polygons must be parsed at startup, not on the first request.

Loading is ~1.5s of synchronous JSON + shapely work on a 5 MB file. On the
event loop inside a request handler that stalls every other request on the
worker — node ingest included — once per process.
"""

import unittest.mock

import pytest

import services.region_lookup as region_lookup


def _clear_cache():
    """Drop the module-level geometry cache so a cold start can be observed."""
    region_lookup._geoms.clear()


@pytest.fixture(autouse=True)
def _restore_borders():
    """Leave the cache warm for whatever runs next, whichever way the test went."""
    yield
    region_lookup.warm_borders()


class TestWarmBorders:
    def test_warm_borders_populates_the_cache(self):
        _clear_cache()
        assert not region_lookup._geoms

        region_lookup.warm_borders()

        assert set(region_lookup._geoms) == {"us", "ca", "au"}

    def test_warm_borders_is_idempotent(self):
        _clear_cache()
        region_lookup.warm_borders()
        first = dict(region_lookup._geoms)

        region_lookup.warm_borders()

        # Same geometry objects, not re-parsed.
        assert all(region_lookup._geoms[k] is first[k] for k in first)

    def test_classify_region_still_loads_on_demand_without_a_warm_up(self):
        """Scripts and tests import this module without running app startup."""
        _clear_cache()

        assert region_lookup.classify_region(42.38708028093612, -71.24905416622781) == "us"

    def test_a_warm_cache_is_never_re_parsed(self):
        """The regression this guards: the 5 MB parse happening on the request path.

        classify_region() always calls _load_borders(); the point is that once
        warm it returns on a dict check. Patch the shapely constructor, which
        only runs on a real parse, rather than the guard itself.
        """
        _clear_cache()
        region_lookup.warm_borders()

        with unittest.mock.patch.object(region_lookup, "shape", side_effect=AssertionError("re-parsed after warm-up")):
            assert region_lookup.classify_region(42.38708028093612, -71.24905416622781) == "us"
            assert region_lookup.classify_region(48.8566, 2.3522) is None


class TestLifespanWarmsBorders:
    def test_startup_warms_the_borders_before_serving(self):
        """Booting the app must leave the polygons in memory.

        `with TestClient(app)` runs main.py's lifespan, the same way the
        fourteen other test files that boot the app do.
        """
        from fastapi.testclient import TestClient

        from main import app

        _clear_cache()
        assert not region_lookup._geoms

        with TestClient(app, raise_server_exceptions=False):
            assert set(region_lookup._geoms) == {"us", "ca", "au"}

    def test_a_request_after_startup_does_not_re_parse(self):
        """End to end: boot, then serve, with the parser rigged to fail."""
        from fastapi.testclient import TestClient

        from main import app

        _clear_cache()

        with TestClient(app, raise_server_exceptions=False) as client:
            with unittest.mock.patch.object(
                region_lookup, "shape", side_effect=AssertionError("re-parsed on the request path")
            ):
                # Paris is in no supported region — reaches classify_region and
                # returns 422 without needing to parse anything again.
                r = client.get("/api/towers", params={"lat": 48.8566, "lon": 2.3522})

        assert r.status_code == 422
