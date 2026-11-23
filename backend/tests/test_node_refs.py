import asyncio
import logging
import sys
import threading

import pytest

from core import state
from core.nodes import Node
from core.users import async_session_maker
from services import node_refs


@pytest.fixture()
def seed():
    """seed(node_id=node_ref, ...): write rows and drop the cache."""

    def _seed(**pairs: str) -> None:
        async def _go():
            async with async_session_maker() as session:
                for nid, ref in pairs.items():
                    session.add(Node(node_id=nid, node_ref=ref))
                await session.commit()

        asyncio.run(_go())
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


class TestUnresolvedLogging:
    """An unauthenticated route can call `public_identity` once per connected
    node on every request it serves, so a repeated unresolvable id must not
    turn into a repeated log line."""

    def test_a_repeated_unresolvable_id_logs_once(self, seed, caplog):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        with caplog.at_level(logging.ERROR, logger="services.node_refs"):
            for _ in range(5):
                assert node_refs.public_identity("ret0badcafe") is None
        assert len(caplog.records) == 1

    def test_a_different_unresolvable_id_still_logs_its_own_line(self, seed, caplog):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        with caplog.at_level(logging.ERROR, logger="services.node_refs"):
            assert node_refs.public_identity("ret0badcafe") is None
            assert node_refs.public_identity("ret0badcafe") is None
            assert node_refs.public_identity("retffffffff") is None
        assert len(caplog.records) == 2

    def test_a_refresh_lets_it_log_again(self, seed, caplog, monkeypatch):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        with caplog.at_level(logging.ERROR, logger="services.node_refs"):
            assert node_refs.public_identity("ret0badcafe") is None
            assert node_refs.public_identity("ret0badcafe") is None
            assert len(caplog.records) == 1

            monkeypatch.setattr(node_refs, "_expires_at", 0.0)
            node_refs._refresh()

            assert node_refs.public_identity("ret0badcafe") is None
        assert len(caplog.records) == 2


_SYNTH = "synth-GVL-0002"


class TestSubstituteIdentities:
    def test_a_single_node_entry_carries_the_ref(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        out = node_refs.substitute_identities({"aircraft": [{"hex": "abc123", "node_id": "ret1a2b3c4d"}]})
        assert out["aircraft"] == [{"hex": "abc123", "node_ref": "nde1a2b3c4d00"}]

    def test_contributing_ids_are_substituted(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00", ret9f8e7d6c="nde9f8e7d6c00")
        out = node_refs.substitute_identities(
            {
                "aircraft": [
                    {
                        "hex": "abc123",
                        "multinode": True,
                        "contributing_node_ids": ["ret1a2b3c4d", "ret9f8e7d6c"],
                    }
                ]
            }
        )
        assert out["aircraft"][0]["contributing_node_refs"] == ["nde1a2b3c4d00", "nde9f8e7d6c00"]
        assert "contributing_node_ids" not in out["aircraft"][0]

    def test_a_synthetic_node_passes_through_unchanged(self, seed):
        """Unchanged in value; the field is still renamed, as it is for every entry."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        out = node_refs.substitute_identities({"aircraft": [{"hex": "abc123", "node_id": _SYNTH}]})
        assert out["aircraft"] == [{"hex": "abc123", "node_ref": _SYNTH}]

    def test_a_null_node_id_is_published_as_a_null_ref(self, seed):
        """A multinode entry has no single detector; the key still has to go."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        out = node_refs.substitute_identities(
            {
                "aircraft": [
                    {
                        "hex": "abc123",
                        "node_id": None,
                        "multinode": True,
                        "contributing_node_ids": ["ret1a2b3c4d"],
                    }
                ]
            }
        )
        assert out["aircraft"] == [
            {
                "hex": "abc123",
                "node_ref": None,
                "multinode": True,
                "contributing_node_refs": ["nde1a2b3c4d00"],
            }
        ]

    def test_an_empty_contributor_list_is_renamed_not_dropped(self, seed):
        """An entry with no contributors is a single-node one, not a redacted one."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        out = node_refs.substitute_identities(
            {"aircraft": [{"hex": "abc123", "node_id": "ret1a2b3c4d", "contributing_node_ids": []}]}
        )
        assert out["aircraft"] == [{"hex": "abc123", "node_ref": "nde1a2b3c4d00", "contributing_node_refs": []}]

    def test_an_unresolvable_real_node_is_dropped_not_published(self, seed):
        """Fail closed: publishing the private id as a fallback is the bug."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        out = node_refs.substitute_identities({"aircraft": [{"hex": "abc123", "node_id": "ret0badcafe"}]})
        assert out["aircraft"] == []

    def test_arcs_and_detecting_nodes_are_substituted(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        out = node_refs.substitute_identities(
            {
                "aircraft": [],
                "detection_arcs": [{"hex": "abc123", "node_id": "ret1a2b3c4d"}],
                "detecting_nodes": {"abc123": ["ret1a2b3c4d", "ret0badcafe"]},
            }
        )
        assert out["detection_arcs"] == [{"hex": "abc123", "node_ref": "nde1a2b3c4d00"}]
        # detecting_nodes keeps its name: it claims no identifier type, and only
        # its values changed.
        assert out["detecting_nodes"] == {"abc123": ["nde1a2b3c4d00"]}

    def test_a_hex_left_with_no_nodes_loses_its_entry(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        out = node_refs.substitute_identities({"aircraft": [], "detecting_nodes": {"abc123": ["ret0badcafe"]}})
        assert out["detecting_nodes"] == {}

    def test_the_messages_count_follows_what_survives(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        out = node_refs.substitute_identities(
            {
                "messages": 2,
                "aircraft": [
                    {"hex": "a", "node_id": "ret1a2b3c4d"},
                    {"hex": "b", "node_id": "ret0badcafe"},
                ],
            }
        )
        assert out["messages"] == 1


class TestPublicRecords:
    """The structural walk over diagnostic records.

    A tuple serialises as a JSON array, so an id inside one publishes exactly
    as an id inside a list would.
    """

    def test_a_node_id_inside_a_tuple_is_republished(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        (out,) = node_refs.public_records([{"contributing_node_ids": ("ret1a2b3c4d",)}])
        assert out == {"contributing_node_refs": ["nde1a2b3c4d00"]}

    def test_an_unresolvable_id_inside_a_tuple_loses_its_place(self, seed):
        """Also pins the vocabulary half: the second id has no registry row, so
        it is dropped only if the pass that collects named ids reads tuples."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        (out,) = node_refs.public_records([{"contributing_node_ids": ("ret1a2b3c4d", "ret0badcafe")}])
        assert out == {"contributing_node_refs": ["nde1a2b3c4d00"]}


class TestPublicAnalyticsWithholdsPrivateNodes:
    """public_analytics names a private node nowhere, as it names a node with
    no handle: a pair naming it goes, it leaves the blocked list, and prose
    naming it is redacted. Its ref would say it exists and whom it sits beside."""

    _PRIV = "ret1a2b3c4d"
    _PUB = "ret2b3c4d5e"

    def _cross(self, *ids: str) -> dict:
        a, b = ids
        return {"pair_overlaps": [{"node_a": a, "node_b": b, "overlap_ratio": 0.4}], "blocked_nodes": [a]}

    @pytest.fixture()
    def private(self, seed, monkeypatch):
        seed(**{self._PRIV: "nde1a2b3c4d00", self._PUB: "nde2b3c4d5e00"})

        def _private(*ids: str) -> None:
            monkeypatch.setattr(node_refs, "private_node_ids", lambda: frozenset(ids))

        return _private

    def test_a_private_node_is_in_no_pair_and_not_blocked(self, private):
        private(self._PRIV)
        out = node_refs.public_analytics({}, self._cross(self._PRIV, self._PUB))
        assert out["cross_node"] == {"pair_overlaps": [], "blocked_nodes": []}

    def test_a_public_pair_is_still_named_by_its_refs(self, private):
        private()
        out = node_refs.public_analytics({}, self._cross(self._PRIV, self._PUB))
        assert out["cross_node"]["pair_overlaps"][0]["node_a"] == "nde1a2b3c4d00"
        assert out["cross_node"]["blocked_nodes"] == ["nde1a2b3c4d00"]

    def test_prose_naming_it_is_redacted_and_its_own_summary_skipped(self, private):
        private(self._PRIV)
        reason = f"Inconsistent with trusted neighbour {self._PRIV} (overlap=0.00)"
        summaries = {self._PUB: {"reputation": {"recent_penalties": [{"reason": reason}]}}, self._PRIV: {"metrics": {}}}
        out = node_refs.public_analytics(summaries, {})
        assert list(out["nodes"]) == ["nde2b3c4d5e00"]
        assert out["nodes"]["nde2b3c4d5e00"]["reputation"]["recent_penalties"] == [
            {"reason": "Inconsistent with trusted neighbour [unpublished node] (overlap=0.00)"}
        ]

    def test_a_private_synthetic_node_is_withheld_too(self, private):
        """Another synthetic node publishes as itself and is left out of the
        vocabulary; a private one is not left out of it."""
        private("synth-quiet-01")
        cross = self._cross("synth-quiet-01", "synth-loud-02")
        cross["blocked_nodes"] = ["synth-quiet-01", "synth-loud-02"]
        out = node_refs.public_analytics({}, cross, ["synth-quiet-01", "synth-loud-02"])
        assert out["cross_node"] == {"pair_overlaps": [], "blocked_nodes": ["synth-loud-02"]}

    def test_before_the_policy_is_known_no_node_is_named(self, seed, monkeypatch):
        from services.publication import PublicationUnavailable

        seed(**{self._PRIV: "nde1a2b3c4d00", self._PUB: "nde2b3c4d5e00"})

        def unavailable():
            raise PublicationUnavailable("not yet")

        monkeypatch.setattr(node_refs, "private_node_ids", unavailable)
        out = node_refs.public_analytics({self._PUB: {"metrics": {}}}, self._cross(self._PRIV, self._PUB))
        assert out == {"nodes": {}, "cross_node": {"pair_overlaps": [], "blocked_nodes": []}}


_MIRRORED = "ret0badcafe"


class TestMirroredRef:
    """A node whose detections are mirrored in from another environment.

    It has no registry row here, so the registry cannot answer for it. The
    mirror sends the ref the owning environment resolved, and the boundary
    publishes that rather than dropping the node.
    """

    def _connected(self, **entry):
        from core import state

        with state.connected_nodes_lock:
            state.connected_nodes[_MIRRORED] = {"is_synthetic": False, "status": "active", **entry}

    def teardown_method(self):
        from core import state

        with state.connected_nodes_lock:
            state.connected_nodes.pop(_MIRRORED, None)

    def test_a_mirrored_ref_is_published_when_the_registry_has_none(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        self._connected(node_ref="ndemirrored001")
        assert node_refs.public_identity(_MIRRORED) == "ndemirrored001"

    def test_without_one_the_node_is_still_dropped(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        self._connected()
        assert node_refs.public_identity(_MIRRORED) is None

    def test_the_registry_wins_over_a_mirrored_ref(self, seed):
        """A local row is the authority; a mirrored value cannot override it."""
        seed(**{_MIRRORED: "ndelocalrow001"})
        self._connected(node_ref="ndemirrored001")
        assert node_refs.public_identity(_MIRRORED) == "ndelocalrow001"

    def test_a_mirrored_ref_resolves_back_to_its_node(self, seed):
        """Public path parameters take the ref a node is published under."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        self._connected(node_ref="ndemirrored001")
        assert node_refs.id_for_identity("ndemirrored001") == _MIRRORED

    def test_a_mirrored_ref_the_registry_outranks_resolves_to_nothing(self, seed):
        seed(**{_MIRRORED: "ndelocalrow001"})
        self._connected(node_ref="ndemirrored001")
        assert node_refs.id_for_identity("ndemirrored001") is None
        assert node_refs.id_for_identity("ndelocalrow001") == _MIRRORED

    def test_a_mirrored_node_reaches_the_published_feed(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        self._connected(node_ref="ndemirrored001")
        out = node_refs.substitute_identities({"aircraft": [{"hex": "abc123", "node_id": _MIRRORED}]})
        assert out["aircraft"] == [{"hex": "abc123", "node_ref": "ndemirrored001"}]


class TestRefToIdMap:
    """The inverse of the boundary, for the admin routes allowed to cross it."""

    def _connected(self, node_id, **entry):
        from core import state

        with state.connected_nodes_lock:
            state.connected_nodes[node_id] = {"is_synthetic": False, "status": "active", **entry}

    def teardown_method(self):
        from core import state

        with state.connected_nodes_lock:
            state.connected_nodes.clear()

    def test_it_names_the_node_behind_every_registered_ref(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00", ret9f8e7d6c="nde9f8e7d6c00")
        assert node_refs.ref_to_id_map() == {
            "nde1a2b3c4d00": "ret1a2b3c4d",
            "nde9f8e7d6c00": "ret9f8e7d6c",
        }

    def test_a_mirrored_node_resolves_through_the_ids_it_is_given(self, seed):
        """It has no row here, so only a caller holding the id can place it."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        self._connected(_MIRRORED, node_ref="ndemirrored001")
        assert node_refs.ref_to_id_map([_MIRRORED])["ndemirrored001"] == _MIRRORED

    def test_the_registry_wins_over_a_mirrored_ref(self, seed):
        seed(**{_MIRRORED: "ndelocalrow001"})
        self._connected(_MIRRORED, node_ref="ndemirrored001")
        mapping = node_refs.ref_to_id_map([_MIRRORED])
        assert mapping["ndelocalrow001"] == _MIRRORED
        assert "ndemirrored001" not in mapping

    def test_a_synthetic_node_maps_to_itself(self, seed):
        """It publishes under its own id, so that id is both halves."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        self._connected("synth-node-1", is_synthetic=True)
        assert node_refs.ref_to_id_map(["synth-node-1"])["synth-node-1"] == "synth-node-1"

    def test_a_node_with_no_handle_at_all_is_left_out(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        self._connected(_MIRRORED)
        assert node_refs.ref_to_id_map([_MIRRORED]) == {"nde1a2b3c4d00": "ret1a2b3c4d"}


class TestCachedOwnerIdentity:
    """The solver threads' lookup: owner_identity over the maps as they stand.

    A refresh holds `_lock` across `_load`, a registry read, so a lookup that
    refreshes can queue every solver thread behind one slow database call.
    """

    def test_it_does_not_wait_on_a_refresh_in_progress(self, seed, monkeypatch):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.ref_for("ret1a2b3c4d") == "nde1a2b3c4d00"
        entered, release = threading.Event(), threading.Event()

        def _slow_load():
            entered.set()
            release.wait(timeout=30)
            return {"ret1a2b3c4d": "nde1a2b3c4d00"}, {"nde1a2b3c4d00": "ret1a2b3c4d"}

        monkeypatch.setattr(node_refs, "_load", _slow_load)
        monkeypatch.setattr(node_refs, "_expires_at", 0.0)
        answers = []
        refresher = threading.Thread(target=node_refs.ref_for, args=("ret9f8e7d6c",))
        reader = threading.Thread(target=lambda: answers.append(node_refs.cached_owner_identity("ret1a2b3c4d")))
        refresher.start()
        try:
            assert entered.wait(timeout=10)
            reader.start()
            reader.join(timeout=5)
            assert not reader.is_alive(), "cached_owner_identity waited on the refresh"
            assert answers == ["nde1a2b3c4d00"]
        finally:
            release.set()
            refresher.join(timeout=10)
            if reader.ident is not None:
                reader.join(timeout=10)

    def test_it_answers_from_the_maps_without_a_load(self, seed, monkeypatch):
        """A refresh is due, so a lookup that ran one would be counted here,
        and would answer from the empty maps it loads."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.ref_for("ret1a2b3c4d") == "nde1a2b3c4d00"
        loads = []
        monkeypatch.setattr(node_refs, "_load", lambda: loads.append(1) or ({}, {}))
        monkeypatch.setattr(node_refs, "_expires_at", 0.0)
        monkeypatch.setitem(state.connected_nodes, _MIRRORED, {"is_synthetic": False, "node_ref": "ndemirrored001"})

        assert node_refs.cached_owner_identity("ret1a2b3c4d") == "nde1a2b3c4d00"
        assert node_refs.cached_owner_identity(_MIRRORED) == "ndemirrored001"
        assert node_refs.cached_owner_identity(_SYNTH) == _SYNTH
        assert node_refs.cached_owner_identity("ret9f8e7d6c") is None
        assert node_refs.cached_owner_identity(None) is None
        assert node_refs.cached_owner_identity("") is None
        assert loads == []

    def test_it_reads_the_map_a_refresh_rebinds(self, seed, monkeypatch):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.ref_for("ret1a2b3c4d") == "nde1a2b3c4d00"
        monkeypatch.setattr(
            node_refs, "_load", lambda: ({"ret1a2b3c4d": "nderotated0001"}, {"nderotated0001": "ret1a2b3c4d"})
        )
        monkeypatch.setattr(node_refs, "_expires_at", 0.0)
        node_refs._refresh()
        assert node_refs.cached_owner_identity("ret1a2b3c4d") == "nderotated0001"


class TestContributorHandle:
    """What a solve's contributor is published as, the one rule the live feed
    and the tracks archive share."""

    def test_a_synthetic_node_holds_its_own_id_and_nothing_else(self):
        assert node_refs.contributor_handle(_SYNTH, _SYNTH) == _SYNTH
        assert node_refs.contributor_handle(_SYNTH, "nde1a2b3c4d00") is None

    def test_a_real_node_never_holds_its_own_id(self):
        assert node_refs.contributor_handle("ret1a2b3c4d", "ret1a2b3c4d") is None
        assert node_refs.contributor_handle("ret1a2b3c4d", "nde1a2b3c4d00") == "nde1a2b3c4d00"

    def test_a_missing_id_holds_nothing(self):
        assert node_refs.contributor_handle("", "nde1a2b3c4d00") is None
        assert node_refs.contributor_handle(None, "nde1a2b3c4d00") is None

    @pytest.mark.parametrize("recorded", [None, ""])
    def test_with_nothing_recorded_it_is_resolved_now(self, seed, recorded):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.contributor_handle("ret1a2b3c4d", recorded) == "nde1a2b3c4d00"
        assert node_refs.contributor_handle(_SYNTH, recorded) == _SYNTH
        assert node_refs.contributor_handle("ret0badcafe", recorded) is None

    def test_it_keeps_exactly_what_owner_identity_gives(self, seed, monkeypatch):
        """A handle owner_identity gives is one the predicate keeps, and no
        handle stays no handle, for every kind of node."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        monkeypatch.setitem(state.connected_nodes, _MIRRORED, {"is_synthetic": False, "node_ref": "ndemirrored001"})
        nodes = (_SYNTH, "ret1a2b3c4d", _MIRRORED, "ret9f8e7d6c")
        owned = [node_refs.owner_identity(n) for n in nodes]
        assert owned == [_SYNTH, "nde1a2b3c4d00", "ndemirrored001", None]
        assert [node_refs.contributor_handle(n, o) for n, o in zip(nodes, owned)] == owned


class TestPublishedIdentity:
    """Whether, and as what, a node may be published now: the one rule the
    ADS-B archive and the public analytics payloads share."""

    def test_a_private_node_is_withheld_without_being_looked_up(self, seed, caplog):
        """The registered one would resolve, and the one with no row would be
        logged as unresolvable, had either been looked up."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        private = frozenset({"ret1a2b3c4d", "ret0badcafe"})
        with caplog.at_level(logging.ERROR, logger="services.node_refs"):
            for node_id in sorted(private):
                assert node_refs.published_identity(node_id, private) is None
                assert node_refs.publishable(node_id, private) is False
        assert caplog.records == []

    def test_a_real_node_with_no_identity_is_withheld(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.published_identity("ret0badcafe", frozenset()) is None
        assert node_refs.publishable("ret0badcafe", frozenset()) is False
        assert node_refs.published_identity("ret1a2b3c4d", frozenset()) == "nde1a2b3c4d00"
        assert node_refs.publishable("ret1a2b3c4d", frozenset()) is True

    def test_a_synthetic_node_is_published_as_itself(self, seed):
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        assert node_refs.published_identity(_SYNTH, frozenset()) == _SYNTH
        assert node_refs.publishable(_SYNTH, frozenset()) is True

    def test_a_node_whose_only_handle_is_empty_is_withheld(self, seed, monkeypatch):
        """Never an empty identity, which a caller testing for truth would
        withhold and one testing for None would publish."""
        seed(ret1a2b3c4d="nde1a2b3c4d00")
        monkeypatch.setitem(state.connected_nodes, _MIRRORED, {"is_synthetic": False, "node_ref": ""})
        assert node_refs.published_identity(_MIRRORED, frozenset()) is None
        assert node_refs.publishable(_MIRRORED, frozenset()) is False
