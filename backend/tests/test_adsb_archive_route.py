"""routes/adsb_archive.py: the public ADS-B archive, read back over HTTP.

The rows and what reads them are covered in tests/test_adsb_archive_store.py,
whose helpers write the parts here; these cover the parameters, the policy as
read per request, what the body may carry, and the API reference.
"""

import asyncio
import json
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from core.nodes import Node
from core.users import async_session_maker
from main import app
from routes.adsb_archive import ArchiveAdsb
from services import adsb_store, adsb_writer, publication
from tests.test_adsb_archive_store import (
    NO_REF,
    PRIV,
    PUB,
    PUBLISHED_KEYS,
    SYN,
    at,
    fix,
    ms_of,
    seed,
    write_part,
)


@pytest.fixture
def root(tmp_path, monkeypatch):
    path = tmp_path / "adsb"
    monkeypatch.setattr(adsb_writer, "ADSB_DIR", str(path))
    adsb_store._reset_for_tests()
    yield path
    adsb_store._reset_for_tests()


@pytest.fixture
def refs():
    return seed()


@pytest.fixture
def api():
    """The real app without its lifespan: this route needs none of it."""
    client = TestClient(app, raise_server_exceptions=False)
    yield client
    client.close()


def adsb(api, start, end, **params):
    return api.get("/api/data/adsb", params={"from": str(ms_of(start)), "to": str(ms_of(end)), **params})


def lats(response) -> list[float]:
    assert response.status_code == 200, response.text
    return [row["lat"] for row in response.json()["rows"]]


def test_the_window_is_capped_at_60_minutes(api, root, refs):
    write_part(root, at(11, 1), [fix(at(10), lat=1.0), fix(at(10, 59, 59, 999), lat=2.0)])
    sixty = adsb(api, at(10), at(11))
    assert lats(sixty) == [1.0, 2.0]
    page = ArchiveAdsb.model_validate(sixty.json(), strict=True)
    assert (page.from_ms, page.to, page.fleet, page.count, page.truncated) == (
        ms_of(at(10)),
        ms_of(at(11)),
        "real",
        2,
        False,
    )
    over = adsb(api, at(10), at(11, 0, 0, 1))
    assert over.status_code == 422
    assert over.json()["detail"] == "to - from exceeds the 60-minute cap"


@pytest.mark.parametrize(("start", "end"), [(at(10), at(10)), (at(10, 1), at(10))], ids=["equal", "reversed"])
def test_to_must_be_after_from(api, root, refs, start, end):
    response = adsb(api, start, end)
    assert response.status_code == 422
    assert response.json()["detail"] == "to must be after from"


def test_bbox_keeps_the_rows_inside_it_including_across_the_antimeridian(api, root, refs):
    write_part(
        root,
        at(10, 5),
        [
            fix(at(10, 1), "aaaaaa", lat=1.0, lon=179.5),
            fix(at(10, 1), "bbbbbb", lat=2.0, lon=-179.5),
            fix(at(10, 1), "cccccc", lat=3.0, lon=0.0),
            fix(at(10, 1), "dddddd", lat=20.0, lon=179.5),
        ],
    )
    assert lats(adsb(api, at(10), at(10, 30), bbox="-10,170,10,-170")) == [1.0, 2.0]
    assert lats(adsb(api, at(10), at(10, 30), bbox="-10,-10,10,10")) == [3.0]


def test_a_node_made_private_today_drops_its_fixes_from_the_same_files(api, root, refs):
    write_part(root, at(10, 5), [fix(at(10, 1), lat=1.0, node=PUB)])
    assert lats(adsb(api, at(10), at(10, 30))) == [1.0]

    async def _go():
        async with async_session_maker() as session:
            await session.execute(update(Node).where(Node.node_id == PUB).values(publication="private"))
            await session.commit()

    asyncio.run(_go())
    # What the registration routes do after a committed change.
    publication.invalidate()
    assert lats(adsb(api, at(10), at(10, 30))) == []


def test_no_attribution_or_withheld_node_appears_in_the_body(api, root, refs):
    write_part(
        root,
        at(10, 5),
        [
            fix(at(10, 1), "aaaaaa", node=PUB),
            fix(at(10, 1), "bbbbbb", node=PRIV),
            fix(at(10, 1), "cccccc", node=NO_REF),
            fix(at(10, 1), "dddddd", world="sim", node=SYN),
            fix(at(10, 1), "eeeeee", world="sim", node=None),
            fix(at(10, 1), "ffffff", source="aggregator", node=f"{PRIV},{PUB}"),
            fix(at(10, 1), "abcdef", source="aggregator", node=PRIV),
        ],
    )
    response = adsb(api, at(10), at(10, 30), fleet="all")
    body = json.loads(response.text)
    assert [row["hex"] for row in body["rows"]] == ["aaaaaa", "dddddd", "eeeeee", "ffffff"]
    assert all(set(row) == PUBLISHED_KEYS for row in body["rows"])
    assert not re.search(r"ret[0-9a-f]{8}", response.text)
    assert "synth-" not in response.text
    for ref in refs.values():
        assert ref not in response.text
    for key in ("source", "source_node_id", "world", "aggregator"):
        assert key not in response.text


def test_an_unknown_policy_is_a_503(api, root, refs, monkeypatch):
    write_part(root, at(10, 5), [fix(at(10, 1))])
    publication._reset_for_tests()

    def unavailable():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(publication, "_query", unavailable)
    response = adsb(api, at(10), at(10, 30))
    assert response.status_code == 503
    assert response.json() == {"detail": "Publication policy is temporarily unavailable"}


def test_the_reference_lists_the_read_under_archive_without_a_session(api):
    operation = api.get("/openapi.json").json()["paths"]["/api/data/adsb"]["get"]
    assert operation["tags"] == ["archive"]
    assert "security" not in operation
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"] == (
        "#/components/schemas/ArchiveAdsb"
    )
    assert {p["name"] for p in operation["parameters"]} == {"from", "to", "bbox", "fleet"}
    row = api.get("/openapi.json").json()["components"]["schemas"]["ArchiveAdsbRow"]
    assert set(row["properties"]) == PUBLISHED_KEYS
    assert row["properties"]["seen_ms"]["type"] == "integer"
    assert "seen_ms" in row["required"]
