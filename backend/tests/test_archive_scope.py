"""services/archive_access.py: one archive boundary, read under two scopes.

The public archive (routes/archive.py) covers every node but the private ones;
an owner's covers their own nodes, private ones included, and nothing else.
Both are exercised here against real Parquet files written by the archive's own
writer into a per-test directory, with real node and claim rows behind the
refs, so the scan, the key rewriting and the policy are the production code.

The owner's two routes are wired in routes/owner.py separately; `owner_app`
makes the same calls they make, so the HTTP answer is pinned here as well.
"""

import asyncio
import json
import os
import shutil
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException, Query
from fastapi.testclient import TestClient

from core.auth import get_user_tenures
from core.nodes import Node, NodeClaim
from core.users import async_session_maker
from main import app
from services import node_auth, node_refs, publication
from services.archive_access import list_files, owner_scope, public_scope, read_file
from services.parquet_writer import write_detections_parquet
from services.storage import list_archived_files, read_archived_file

_OWNER = "a1a1a1a1-0000-4000-8000-000000000001"
_OTHER = "b2b2b2b2-0000-4000-8000-000000000002"

_MINE_PUB, _MINE_PRIV = "ret0123abcd", "ret0a1b2c3d"
_THEIRS_PUB, _THEIRS_PRIV = "ret0badcafe", "ret1a2b3c4d"

# node -> (owner, publication)
_NODES = {
    _MINE_PUB: (_OWNER, "public"),
    _MINE_PRIV: (_OWNER, "private"),
    _THEIRS_PUB: (_OTHER, "public"),
    _THEIRS_PRIV: (_OTHER, "private"),
}

# (node, day of 2026-08, write time, the delay its one detection carries).
# The delay marks each file, so a listing or a download can be read back by it.
_WRITES = [
    (_MINE_PUB, 26, (10, 0, 0), 1.0),
    (_MINE_PRIV, 26, (11, 0, 0), 2.0),
    (_THEIRS_PUB, 26, (12, 0, 0), 3.0),
    (_THEIRS_PRIV, 26, (13, 0, 0), 4.0),
    (_MINE_PUB, 27, (10, 0, 0), 5.0),
    (_MINE_PRIV, 27, (11, 0, 0), 6.0),
    (_MINE_PRIV, 27, (12, 0, 0), 7.0),
    (_THEIRS_PUB, 27, (13, 0, 0), 8.0),
    (_THEIRS_PRIV, 27, (14, 0, 0), 9.0),
]

# The owner's files in the undated scan's order: newest day first, node
# directories by name, files by name descending within one.
_MINE_UNDATED = [5.0, 7.0, 6.0, 1.0, 2.0]
# A dated scan orders by modification time, newest first.
_MINE_ON_27TH = [7.0, 6.0, 5.0]

_EMPTY = {"files": [], "count": 0, "total": 0}

# When each owner took their nodes: before every file here, the legacy one
# included. How the tenure bounds an owner's archive is
# tests/test_owner_tenure.py's.
_OWNED_SINCE = datetime(2025, 1, 1, tzinfo=timezone.utc)


def _seed() -> dict[str, str]:
    refs = {node_id: node_auth.mint_node_ref() for node_id in _NODES}

    async def _go():
        async with async_session_maker() as session:
            for node_id, (_, choice) in _NODES.items():
                session.add(Node(node_id=node_id, node_ref=refs[node_id], publication=choice))
            await session.flush()
            for node_id, (owner, _) in _NODES.items():
                session.add(
                    NodeClaim(
                        node_id=node_id,
                        user_id=owner,
                        verified=True,
                        undeliverable=False,
                        owned_since=_OWNED_SINCE,
                        owned_since_user_id=owner,
                    )
                )
            await session.commit()

    asyncio.run(_go())
    # Both caches sit in front of these rows with a TTL; drop what an earlier
    # test left so they answer for these ones.
    publication._reset_for_tests()
    node_refs._reset_for_tests()
    return refs


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """The fleet's rows and files: SimpleNamespace(dir, refs, stored, published).

    `stored` and `published` map each file's marker to its key on disk and to
    the key it goes out under.
    """
    base = tmp_path / "archive"
    base.mkdir()
    monkeypatch.setattr("services.storage._LOCAL_ARCHIVE_DIR", str(base))
    refs = _seed()
    stored, published = {}, {}
    for node_id, day, (h, m, s), marker in _WRITES:
        ts = datetime(2026, 8, day, h, m, s, tzinfo=timezone.utc)
        frames = [{"timestamp": int(ts.timestamp() * 1000), "delay": [marker], "doppler": [12.5], "snr": [9.0]}]
        key = write_detections_parquet(node_id=node_id, frames=frames, base_dir=base, write_ts=ts)
        assert key is not None
        os.utime(base / key, (ts.timestamp(), ts.timestamp()))
        stored[marker] = key
        published[marker] = key.replace(f"node_id={node_id}", f"node_ref={refs[node_id]}")
    return SimpleNamespace(dir=base, refs=refs, stored=stored, published=published)


@pytest.fixture
def public():
    """The real app, without its lifespan: the archive routes need none of it."""
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client
    finally:
        client.close()


def owner_app(user_id: str) -> FastAPI:
    """The owner's archive routes as routes/owner.py makes them, for one caller.

    Behind the real app's middleware, as they are served, so a header that
    middleware adds (CORS's Vary: Origin) lands on both answers alike.
    """
    owned = FastAPI()
    owned.user_middleware = list(app.user_middleware)

    @owned.get("/me/archive")
    async def listing(
        date: str = Query(None),
        node_ref: str = Query(None),
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ):
        scope = owner_scope(await get_user_tenures(user_id))
        return await list_files(scope, date=date, node_ref=node_ref, limit=limit, offset=offset)

    @owned.get("/me/archive/{key:path}")
    async def download(key: str):
        return read_file(owner_scope(await get_user_tenures(user_id)), key)

    return owned


@pytest.fixture
def owner():
    client = TestClient(owner_app(_OWNER), raise_server_exceptions=False)
    try:
        yield client
    finally:
        client.close()


def _recording(calls: list):
    def lister(**kwargs):
        calls.append(kwargs)
        return list_archived_files(**kwargs)

    return lister


def _markers(archive, listing: dict) -> list[float]:
    by_key = {key: marker for marker, key in archive.published.items()}
    return [by_key[f["key"]] for f in listing["files"]]


async def _mine():
    return owner_scope(await get_user_tenures(_OWNER))


# ── The owner's listing ──────────────────────────────────────────────────────


class TestOwnerListing:
    async def test_it_lists_the_owners_private_node_and_nobody_elses(self, archive):
        calls = []
        listing = await list_files(await _mine(), lister=_recording(calls))
        assert _markers(archive, listing) == _MINE_UNDATED
        assert listing["count"] == listing["total"] == len(_MINE_UNDATED)
        assert listing["truncated"] is False
        # The set went into the scan, not over its result.
        assert calls[0]["node_ids"] == frozenset({_MINE_PUB, _MINE_PRIV})

    async def test_its_keys_carry_the_ref_and_never_the_id(self, archive):
        text = json.dumps(await list_files(await _mine()))
        assert "node_id=" not in text
        for node_id in _NODES:
            assert node_id not in text
        assert f"node_ref={archive.refs[_MINE_PRIV]}" in text
        assert archive.refs[_THEIRS_PUB] not in text
        assert archive.refs[_THEIRS_PRIV] not in text

    async def test_pages_are_cut_from_the_owners_own_files(self, archive):
        """Every page is full until the owner's files run out, and the pages
        put back together are the whole listing: the scan kept to the set,
        rather than paging everyone's files and striking the others'."""
        scope = await _mine()
        pages = [await list_files(scope, limit=2, offset=offset) for offset in (0, 2, 4)]
        assert [page["count"] for page in pages] == [2, 2, 1]
        assert {page["total"] for page in pages} == {len(_MINE_UNDATED)}
        assert [m for page in pages for m in _markers(archive, page)] == _MINE_UNDATED
        past = await list_files(scope, offset=5)
        assert past["files"] == []
        assert past["total"] == len(_MINE_UNDATED)

    async def test_a_date_keeps_to_that_day(self, archive):
        scope = await _mine()
        day = await list_files(scope, date="2026/08/27")
        assert _markers(archive, day) == _MINE_ON_27TH
        assert day["count"] == day["total"] == 3
        assert "truncated" not in day
        assert (await list_files(scope, date="2026/08"))["total"] == len(_MINE_UNDATED)
        assert await list_files(scope, date="2026/08/25") == _EMPTY

    async def test_the_filter_takes_the_owners_private_ref(self, archive):
        listing = await list_files(await _mine(), node_ref=archive.refs[_MINE_PRIV])
        assert _markers(archive, listing) == [7.0, 6.0, 2.0]
        assert listing["total"] == 3

    @pytest.mark.parametrize("asked", ["their public node", "their private node", "unknown", "my node's id"])
    async def test_a_ref_outside_the_set_lists_nothing(self, asked, archive):
        """An unknown ref, another owner's, and a private id where a ref belongs
        all read the same, and none of them reaches the store."""
        value = {
            "their public node": archive.refs[_THEIRS_PUB],
            "their private node": archive.refs[_THEIRS_PRIV],
            "unknown": node_auth.mint_node_ref(),
            "my node's id": _MINE_PRIV,
        }[asked]
        calls = []
        assert await list_files(await _mine(), node_ref=value, lister=_recording(calls)) == _EMPTY
        assert calls == []

    async def test_no_nodes_list_nothing_without_a_scan(self, archive):
        calls = []
        assert await list_files(owner_scope({}), lister=_recording(calls)) == _EMPTY
        assert calls == []

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ({}, [5.0, 1.0]),
            ({"date": "2026/08/27"}, [5.0]),
            ({"date": "2026/08"}, [5.0, 1.0]),
            ({"limit": 1}, [5.0]),
            ({"limit": 1, "offset": 1}, [1.0]),
            ({"offset": 2}, []),
        ],
    )
    def test_a_node_both_archives_cover_pages_the_same_in_both(self, query, expected, archive, owner, public):
        """date, limit, offset and the answer's shape mean the same thing in
        both: for an owned public node the two archives agree to the byte."""
        params = {"node_ref": archive.refs[_MINE_PUB], **query}
        mine = owner.get("/me/archive", params=params)
        theirs = public.get("/api/data/archive", params=params)
        assert mine.status_code == theirs.status_code == 200
        assert mine.content == theirs.content
        assert _markers(archive, mine.json()) == expected


# ── The owner's download ─────────────────────────────────────────────────────


class TestOwnerDownload:
    def test_the_owner_downloads_their_private_file(self, archive, owner):
        response = owner.get(f"/me/archive/{archive.published[6.0]}")
        assert response.status_code == 200
        expected = dict(read_archived_file(archive.stored[6.0]))
        del expected["node_id"]
        assert response.json() == {**expected, "node_ref": archive.refs[_MINE_PRIV]}
        assert response.json()["detections"][0]["delay"] == [6.0]
        assert _MINE_PRIV not in response.text

    def test_an_owned_public_file_is_the_public_archives_body(self, archive, owner, public):
        mine = owner.get(f"/me/archive/{archive.published[5.0]}")
        theirs = public.get(f"/api/data/archive/{archive.published[5.0]}")
        assert mine.status_code == theirs.status_code == 200
        assert mine.content == theirs.content
        assert dict(mine.headers) == dict(theirs.headers)

    @staticmethod
    def outside(archive) -> dict[str, str]:
        return {
            "their public file": archive.published[3.0],
            "their private file": archive.published[4.0],
            "my file under its stored key": archive.stored[6.0],
            "a key with no node": "part-120000-0.parquet",
        }

    @staticmethod
    def unknown(archive) -> dict[str, str]:
        return {
            "a ref nobody holds": f"year=2026/month=08/day=27/node_ref={node_auth.mint_node_ref()}/part-120000-0.parquet",
            "no such file of mine": f"year=2026/month=08/day=27/node_ref={archive.refs[_MINE_PRIV]}/part-000000-0.parquet",
        }

    def test_a_key_outside_the_set_is_404_byte_identical_to_an_unknown_key(self, archive, owner, public):
        answers = {
            name: owner.get(f"/me/archive/{key}")
            for name, key in (self.outside(archive) | self.unknown(archive)).items()
        }
        reference = answers["a ref nobody holds"]
        assert reference.status_code == 404
        for name, response in answers.items():
            assert response.status_code == 404, name
            assert response.content == reference.content, name
            assert dict(response.headers) == dict(reference.headers), name
        # And the same body the public archive gives a key it does not have.
        missing = public.get(f"/api/data/archive/{self.unknown(archive)['a ref nobody holds']}")
        assert missing.status_code == 404
        assert reference.content == missing.content == b'{"detail":"Archive file not found"}'

    async def test_the_404_is_decided_before_the_store_is_read(self, archive):
        scope = await _mine()
        read = []
        for key in self.outside(archive).values():
            with pytest.raises(HTTPException) as refused:
                read_file(scope, key, reader=lambda k: read.append(k) or {"node_id": _MINE_PRIV})
            assert refused.value.status_code == 404
        assert read == []

    async def test_a_traversal_under_an_owned_ref_stays_in_the_archive(self, archive):
        """The ref resolves and is in the set, so it is the store's own path
        check that refuses a real file just outside the archive, with the same
        404.  Called directly: an HTTP client folds the `..` away before a
        request is sent."""
        name = archive.stored[6.0].rsplit("/", 1)[-1]
        beside = archive.dir.parent / f"node_id={_MINE_PRIV}"
        beside.mkdir()
        shutil.copy(archive.dir / archive.stored[6.0], beside / name)
        with pytest.raises(HTTPException) as refused:
            read_file(await _mine(), f"../node_ref={archive.refs[_MINE_PRIV]}/{name}")
        assert (refused.value.status_code, refused.value.detail) == (404, "Archive file not found")

    def test_no_nodes_download_nothing(self, archive):
        with pytest.raises(HTTPException) as refused:
            read_file(owner_scope({}), archive.published[6.0])
        assert refused.value.status_code == 404


# ── The date ─────────────────────────────────────────────────────────────────


class TestTheDate:
    """The store joins `date` onto its root as a path, so a date that is not
    one, `../..` above all, would walk whatever directory it names.  Both
    archives take what the console sends (YYYY/MM/DD, a day at a time) and
    the shorter prefixes, and refuse the rest before the store is asked."""

    @pytest.mark.parametrize("date", ["2026", "2026/08", "2026/08/27"])
    def test_a_year_a_month_or_a_day_is_listed(self, date, archive, owner, public):
        assert owner.get("/me/archive", params={"date": date}).status_code == 200
        assert public.get("/api/data/archive", params={"date": date}).status_code == 200

    @pytest.mark.parametrize(
        "date",
        ["..", "../..", "../outside", "2026/08/27/..", "/etc", "2026/08/27/node_id=x", "2026-08-27", "2026/8/27"],
    )
    async def test_anything_else_is_422_and_never_reaches_the_store(self, date, archive):
        calls = []
        for scope in (await _mine(), public_scope()):
            with pytest.raises(HTTPException) as refused:
                await list_files(scope, date=date, lister=_recording(calls))
            assert refused.value.status_code == 422
        assert calls == []

    def test_a_climb_out_of_the_archive_lists_nothing_beside_it(self, archive, owner, public):
        """A file just outside the root, where `../outside` would have found it."""
        outside = archive.dir.parent / "outside" / _MINE_PUB
        outside.mkdir(parents=True)
        (outside / "detections_120000.json").write_text(json.dumps({"node_id": _MINE_PUB, "detections": []}))
        for client, path in ((owner, "/me/archive"), (public, "/api/data/archive")):
            response = client.get(path, params={"date": "../outside"})
            assert response.status_code == 422
            assert "outside" not in response.text


# ── The legacy layout ────────────────────────────────────────────────────────


def test_a_legacy_file_lists_and_downloads_under_the_owners_ref(archive, owner, public):
    """A bare node directory (YYYY/MM/DD/<node>/) is a node segment as well."""
    legacy = archive.dir / "2025" / "06" / "21" / _MINE_PRIV
    legacy.mkdir(parents=True)
    body = {"node_id": _MINE_PRIV, "timestamp": "", "count": 0, "detections": []}
    (legacy / "detections_120000.json").write_text(json.dumps(body))
    published = f"2025/06/21/{archive.refs[_MINE_PRIV]}/detections_120000.json"

    listing = owner.get("/me/archive", params={"date": "2025/06/21"}).json()
    assert [f["key"] for f in listing["files"]] == [published]
    downloaded = owner.get(f"/me/archive/{published}")
    assert downloaded.json() == {"node_ref": archive.refs[_MINE_PRIV], "timestamp": "", "count": 0, "detections": []}

    assert public.get("/api/data/archive", params={"date": "2025/06/21"}).json()["files"] == []
    assert public.get(f"/api/data/archive/{published}").status_code == 404


# ── The public archive, unchanged ────────────────────────────────────────────


class TestPublicArchive:
    def test_the_listing_still_leaves_out_every_private_node(self, archive, public):
        listing = public.get("/api/data/archive").json()
        # Undated scan order over all four nodes, private ones struck after
        # paging: `count` is what came back, `total` what the scan saw.
        assert _markers(archive, listing) == [5.0, 8.0, 1.0, 3.0]
        assert listing["count"] == 4
        assert listing["total"] == len(_WRITES)
        assert listing["truncated"] is False
        for node_id in (_MINE_PRIV, _THEIRS_PRIV):
            assert archive.refs[node_id] not in json.dumps(listing)

    @pytest.mark.parametrize("node_id", [_MINE_PRIV, _THEIRS_PRIV])
    def test_a_private_nodes_ref_still_lists_nothing(self, node_id, archive, public):
        assert public.get("/api/data/archive", params={"node_ref": archive.refs[node_id]}).json() == _EMPTY

    @pytest.mark.parametrize("marker", [2.0, 4.0, 6.0, 9.0])
    def test_a_private_file_still_does_not_download(self, marker, archive, public):
        withheld = public.get(f"/api/data/archive/{archive.published[marker]}")
        missing = public.get(
            f"/api/data/archive/year=2026/month=08/day=27/node_ref={node_auth.mint_node_ref()}/part-0.parquet"
        )
        assert withheld.status_code == missing.status_code == 404
        assert withheld.content == missing.content

    def test_a_public_file_still_downloads_under_its_ref(self, archive, public):
        response = public.get(f"/api/data/archive/{archive.published[8.0]}")
        assert response.status_code == 200
        assert response.json()["node_ref"] == archive.refs[_THEIRS_PUB]
        assert response.json()["detections"][0]["delay"] == [8.0]
        assert "node_id" not in response.json()
