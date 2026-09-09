import asyncio
import os
import sys
import threading

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


class TestBackoff:
    def test_a_failed_first_load_does_not_requery_every_call(self, monkeypatch):
        """Before any load has ever succeeded, the backoff must still hold.

        Gating the early-return checks on "have we ever loaded data" (rather
        than on time alone) meant this state never backed off: every call
        re-entered the lock and re-ran `_load`.
        """
        calls = []

        def _boom():
            calls.append(1)
            raise RuntimeError("database is gone")

        monkeypatch.setattr(node_refs, "_load", _boom)
        assert node_refs.ref_for("ret1a2b3c4d") is None
        assert node_refs.ref_for("ret1a2b3c4d") is None
        assert node_refs.ref_for("ret1a2b3c4d") is None
        assert len(calls) == 1


class TestRefreshIsAtomic:
    """A refresh must never be observable as a partially populated map.

    `_refresh` used to `.clear()` then `.update()` the module-level dicts in
    place, so a reader without the lock could see an empty or half-filled map
    mid-refresh.  A rebind (`_forward = forward`) cannot expose that state, so
    these can only fail if the swap goes back to mutating in place.
    """

    def test_a_reader_never_sees_a_partial_map_during_concurrent_refreshes(self, seed, monkeypatch):
        """Drives `_refresh` from many threads while another hammers `ref_for`.

        CPython only checks for a thread switch every `sys.getswitchinterval()`
        (5ms by default), and a `.clear()` followed by `.update()` completes
        well inside that window almost every time, so a plain busy loop at the
        default interval essentially never lands on the bug this guards
        against. Cutting the interval drastically is what turns this from a
        hopeful test into one that reliably reproduces the old failure: with
        it reverted to `.clear()`/`.update()`, this test reliably fails; with
        the rebind, it cannot, because there is no dict ever left half full
        for a reader to observe.
        """
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.ref_for("ret1a2b3c4d") == "nde1a2b3c4d00"
        # A real query briefly releases the GIL for the underlying C call,
        # which resets the interpreter's switch bookkeeping and masks the
        # race; a plain Python return does not, so it is what actually
        # exercises the window between building the new maps and swapping
        # them in.
        monkeypatch.setattr(
            node_refs, "_load", lambda: ({"ret1a2b3c4d": "nde1a2b3c4d00"}, {"nde1a2b3c4d00": "ret1a2b3c4d"})
        )

        old_interval = sys.getswitchinterval()
        sys.setswitchinterval(1e-8)
        try:
            stop = threading.Event()
            saw_none = threading.Event()

            def _hammer():
                while not stop.is_set():
                    if node_refs.ref_for("ret1a2b3c4d") is None:
                        saw_none.set()
                        return

            readers = [threading.Thread(target=_hammer) for _ in range(12)]
            for r in readers:
                r.start()

            try:
                for _ in range(300):
                    monkeypatch.setattr(node_refs, "_expires_at", 0.0)
                    node_refs._refresh()
            finally:
                stop.set()
                for r in readers:
                    r.join(timeout=5)
        finally:
            sys.setswitchinterval(old_interval)

        assert not saw_none.is_set()

    def test_a_successful_refresh_rebinds_rather_than_mutates(self, seed, monkeypatch):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.ref_for("ret1a2b3c4d") == "nde1a2b3c4d00"
        old_forward, old_reverse = node_refs._forward, node_refs._reverse

        monkeypatch.setattr(node_refs, "_expires_at", 0.0)
        node_refs._refresh()

        assert node_refs._forward is not old_forward
        assert node_refs._reverse is not old_reverse
        # The old dicts must be untouched by the refresh, not cleared out from
        # under whoever still holds a reference to them.
        assert old_forward == {"ret1a2b3c4d": "nde1a2b3c4d00"}
        assert old_reverse == {"nde1a2b3c4d00": "ret1a2b3c4d"}
