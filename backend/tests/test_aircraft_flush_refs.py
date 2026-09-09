"""The aircraft feeds publish node_ref, and substitution runs last.

Every filter on the way out — the private-node redaction, the real-only
filter, the per-owner one — matches node_id against a set of node_id.  These
pin the ordering that keeps them working: a payload substituted before them
would match nothing and each feed would come back empty.
"""

import os

os.environ.setdefault("RETINA_ENV", "test")

import asyncio  # noqa: E402

import orjson  # noqa: E402
import pytest  # noqa: E402

from core import state  # noqa: E402
from core.nodes import Node  # noqa: E402
from core.users import async_session_maker  # noqa: E402
from services import node_refs  # noqa: E402

_ID = "ret1a2b3c4d"
_REF = "nde1a2b3c4d00"


@pytest.fixture()
def seeded():
    async def _go():
        async with async_session_maker() as session:
            session.add(Node(node_id=_ID, node_ref=_REF))
            await session.commit()

    asyncio.run(_go())
    # asyncio.run() clears the loop on exit (3.12); conftest's _clean_db
    # restores one for the same reason.
    asyncio.set_event_loop(asyncio.new_event_loop())
    node_refs._reset_for_tests()


@pytest.fixture()
def connected():
    state.connected_nodes[_ID] = {"status": "active", "is_synthetic": False, "config": {"node_id": _ID}}
    yield
    state.connected_nodes.pop(_ID, None)


def _frame() -> dict:
    return {
        "now": 0,
        "messages": 1,
        "aircraft": [{"hex": "abc123", "node_id": _ID}],
        "detection_arcs": [{"id": "arc1", "node_id": _ID}],
        "ground_truth": {},
    }


class TestFlushOnce:
    def test_the_real_only_feed_still_finds_its_nodes_after_substitution(self, seeded, connected):
        from services.tasks.aircraft_flush import _flush_once

        _flush_once(_frame())

        real = orjson.loads(state.latest_real_aircraft_json_bytes)
        assert [ac["node_id"] for ac in real["aircraft"]] == [_REF]
        assert [arc["node_id"] for arc in real["detection_arcs"]] == [_REF]

    def test_the_public_feed_is_published_under_the_ref(self, seeded, connected):
        from services.tasks.aircraft_flush import _flush_once

        _flush_once(_frame())

        public = orjson.loads(state.latest_aircraft_json_bytes)
        assert [ac["node_id"] for ac in public["aircraft"]] == [_REF]
        assert state.latest_aircraft_json_public["aircraft"][0]["node_id"] == _REF

    def test_the_frame_kept_on_state_is_neither_redacted_nor_substituted(self, seeded, connected):
        """The owner filter reads it from there and matches on node_id."""
        from services.tasks.aircraft_flush import _flush_once

        frame = _frame()
        _flush_once(frame)

        assert state.latest_aircraft_json is frame
        assert state.latest_aircraft_json["aircraft"][0]["node_id"] == _ID

    def test_an_unregistered_node_reaches_no_public_feed(self, connected):
        """No ref means no publication: the private id is never the fallback."""
        from services.tasks.aircraft_flush import _flush_once

        _flush_once(_frame())

        assert orjson.loads(state.latest_aircraft_json_bytes)["aircraft"] == []
        assert orjson.loads(state.latest_real_aircraft_json_bytes)["aircraft"] == []


class TestOwnerFeed:
    def test_the_owner_snapshot_is_filtered_by_id_then_published_as_a_ref(self, seeded):
        from services.tasks.aircraft_flush import filter_payload_to_nodes, published_bytes

        owned = filter_payload_to_nodes(_frame(), {_ID})
        assert [ac["node_id"] for ac in owned["aircraft"]] == [_ID]

        out = orjson.loads(published_bytes(owned))
        assert [ac["node_id"] for ac in out["aircraft"]] == [_REF]

    def test_substituting_first_would_empty_it(self, seeded):
        """The failure this ordering exists to prevent, stated as a test."""
        from services.tasks.aircraft_flush import filter_payload_to_nodes

        substituted = node_refs.substitute_identities(_frame())
        assert filter_payload_to_nodes(substituted, {_ID})["aircraft"] == []
