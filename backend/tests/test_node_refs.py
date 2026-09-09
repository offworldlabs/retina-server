import asyncio
import os

os.environ.setdefault("RETINA_ENV", "test")

import pytest  # noqa: E402

from core.nodes import Node  # noqa: E402
from core.users import async_session_maker  # noqa: E402
from services import node_refs  # noqa: E402


@pytest.fixture()
def seed():
    """seed(node_id=node_ref, ...) — write rows and drop the cache."""

    def _seed(**pairs: str) -> None:
        async def _go():
            async with async_session_maker() as session:
                for nid, ref in pairs.items():
                    session.add(Node(node_id=nid, node_ref=ref))
                await session.commit()

        asyncio.run(_go())
        asyncio.set_event_loop(asyncio.new_event_loop())
        node_refs._reset_for_tests()

    return _seed


class TestRefFor:
    def test_a_registered_node_resolves_to_its_ref(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.ref_for("ret1a2b3c4d") == "nde1a2b3c4d00"

    def test_an_unregistered_node_resolves_to_none(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.ref_for("ret9f8e7d6c") is None

    def test_a_missing_id_resolves_to_none(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.ref_for(None) is None
        assert node_refs.ref_for("") is None


class TestIdForRef:
    def test_a_ref_resolves_back_to_its_node_id(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.id_for_ref("nde1a2b3c4d00") == "ret1a2b3c4d"

    def test_an_unknown_ref_resolves_to_none(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.id_for_ref("ndeffffffffff") is None

    def test_a_node_id_is_not_accepted_as_a_ref(self, seed):
        """The reverse map must not answer for the private identifier."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.id_for_ref("ret1a2b3c4d") is None
