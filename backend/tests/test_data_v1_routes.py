"""The keyed reads (routes/data_v1.py): live search, lookup and history behind a key."""

import orjson
import pytest
from fastapi.testclient import TestClient

from core import state, users
from main import app
from scripts.generate_openapi import DATA_CONTRACT_PATH, data_contract_document, render
from services import tracks_store
from services.tasks import track_archive as ta
from tests.test_admin_users import _Access
from tests.test_data_access import _account, _key
from tests.test_tracks_archive_route import _seed, at, rec, write_new

TRAIL = [[51.46, -0.46, 3900.0, 1762041599.0], [51.47, -0.45, 4000.0, 1762041600.0]]


@pytest.fixture(autouse=True)
def _as_deployed(monkeypatch):
    monkeypatch.setattr(users, "AUTH_BYPASS", False)
    monkeypatch.setattr(users, "access_identity", _Access())


@pytest.fixture
def key() -> str:
    return _key(_account("ada@example.com"))[1]


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "tracks"
    monkeypatch.setattr(ta, "_TRACKS_DIR", str(path))
    tracks_store._reset_for_tests()
    yield path
    tracks_store._reset_for_tests()


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


def _live(monkeypatch) -> None:
    def adsb(hex_code, flight, lat, lon, **extra):
        return {
            "hex": hex_code,
            "type": "adsb_icao",
            "flight": flight,
            "lat": lat,
            "lon": lon,
            "alt_baro": 4000,
        } | extra

    aircraft = [
        adsb("4ca1b2", "BAW12", 51.47, -0.45, recent_positions=TRAIL),
        adsb("4ca1b3", "BAW13", 51.5, 0.1),
        adsb("c0ffee", "FJI910", -17.0, 179.6),
    ]
    monkeypatch.setattr(
        state, "latest_real_aircraft_json_bytes", orjson.dumps({"now": 1762041600.0, "aircraft": aircraft})
    )


def test_the_live_search_serves_a_key_the_aircraft_in_the_box_and_refuses_no_key(client, key, monkeypatch):
    _live(monkeypatch)

    response = client.get("/api/v1/aircraft", params={"bbox": "51,-1,52,1"}, headers={"x-apikey": key})
    refused = client.get("/api/v1/aircraft", params={"bbox": "51,-1,52,1"})

    assert [row["hex"] for row in response.json()["aircraft"]] == ["4ca1b2", "4ca1b3"]
    assert (refused.status_code, refused.json()["detail"]["reason"]) == (401, "missing_credentials")


def test_a_lookup_by_hex_returns_the_aircraft_with_its_trail(client, key, monkeypatch):
    _live(monkeypatch)

    response = client.get("/api/v1/lookup", params={"hex": "4CA1B2"}, headers={"Authorization": f"Bearer {key}"})

    [found] = response.json()["aircraft"]
    assert (found["hex"], found["flight"]) == ("4ca1b2", "BAW12")
    assert found["trail"] == [
        {"ts": 1762041599.0, "lat": 51.46, "lon": -0.46, "alt_baro": 3900.0},
        {"ts": 1762041600.0, "lat": 51.47, "lon": -0.45, "alt_baro": 4000.0},
    ]


def test_history_pages_the_real_rows_of_the_window(client, key, root):
    _seed()
    write_new(root, at(10, 31), [rec(at(10, 5), lat=1.0), rec(at(10, 10), lat=2.0), rec(at(10, 20), lat=3.0)])
    window = {"start": "2026-10-18T10:00:00Z", "end": "2026-10-18T11:00:00Z", "bbox": "-90,-90,90,-80", "limit": 2}

    page = client.get("/api/v1/history", params=window, headers={"x-apikey": key}).json()

    assert [row["lat"] for row in page["aircraft"]] == [1.0, 2.0]
    assert page["next_cursor"] is not None


def test_a_history_span_over_the_tier_is_422_naming_the_cap(client, key):
    window = {"start": "2026-10-10T00:00:00Z", "end": "2026-10-18T00:00:00Z", "hex": "4ca1b2"}

    response = client.get("/api/v1/history", params=window, headers={"x-apikey": key})

    assert (response.status_code, response.json()["detail"]) == (422, "start to end may span at most 604800 seconds")


def test_the_committed_data_contract_is_what_the_gated_routes_generate():
    document = data_contract_document()

    assert DATA_CONTRACT_PATH.read_text() == render(document)
    assert {(method, path) for path, operations in document["paths"].items() for method in operations} == {
        ("get", "/api/radar/accuracy"),
        ("get", "/api/radar/analytics"),
        ("get", "/api/radar/analytics/{node_ref}"),
        ("get", "/api/radar/anomalies"),
        ("get", "/api/radar/association/overlaps"),
        ("get", "/api/radar/association/status"),
        ("get", "/api/radar/data/aircraft.json"),
        ("get", "/api/radar/nodes"),
        ("get", "/api/radar/status"),
        ("get", "/api/radar/stream"),
        ("get", "/api/stats/summary"),
        ("get", "/api/v1/aircraft"),
        ("get", "/api/v1/ground-truth/aircraft"),
        ("get", "/api/v1/ground-truth/real"),
        ("get", "/api/v1/history"),
        ("get", "/api/v1/lookup"),
        ("get", "/api/v1/solver/aircraft"),
        ("get", "/api/v1/stream"),
    }
    assert all(
        {"401", "403", "429"} <= set(op["responses"]) for ops in document["paths"].values() for op in ops.values()
    )
    # Every tag an operation carries is declared, the gated public reads' included.
    assert [tag["name"] for tag in document["tags"]] == ["keyed data", "aircraft", "radar", "analytics", "stats"]
    assert set(document["components"]["securitySchemes"]) == {"ApiKeyHeader", "ApiKeyBearer"}
