import re

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from core import node_ids
from core.node_ids import FLEET, MINT_ATTEMPTS, POLLED_BLAH2, SYSTEMS, add_with_minted_id, system_of
from core.nodes import Node
from services.node_auth import mint_node_ref
from services.tcp_handler import SYNTHETIC_NODE_PREFIXES


@pytest.mark.parametrize(
    ("node_id", "system"),
    [
        ("ret1a2b3c4d", FLEET),
        ("bla1a2b3c4d", POLLED_BLAH2),
        ("xyz1a2b3c4d", None),
        ("ret1A2B3C4D", None),
        ("ret1a2b3c4", None),
        ("ret1a2b3c4d5", None),
        ("synth-0a1b2c3d", None),
        ("", None),
    ],
)
def test_system_of_reads_the_prefix_of_a_well_formed_id_only(node_id, system):
    assert system_of(node_id) == system


@pytest.mark.parametrize("system", SYSTEMS)
def test_every_system_prefix_is_three_lowercase_letters(system):
    assert re.fullmatch(r"[a-z]{3}", system)


# services/node_pipeline.py registers every node it is handed as real, polled
# nodes included, which is sound only while no system prefix can look synthetic.
@pytest.mark.parametrize("system", SYSTEMS)
@pytest.mark.parametrize("synthetic", SYNTHETIC_NODE_PREFIXES)
def test_no_system_prefix_overlaps_a_synthetic_one(system, synthetic):
    assert not synthetic.startswith(system)
    assert not system.startswith(synthetic)


def _polled_node(node_id: str) -> Node:
    return Node(node_id=node_id, node_ref=mint_node_ref(), board_model="blah2")


def _hex_sequence(monkeypatch, *values: str) -> list[str]:
    calls: list[str] = []
    pending = list(values)

    def token_hex(nbytes):
        assert nbytes == 4
        value = pending.pop(0) if len(pending) > 1 else pending[0]
        calls.append(value)
        return value

    monkeypatch.setattr(node_ids.secrets, "token_hex", token_hex)
    return calls


async def test_a_minted_id_is_the_prefix_and_eight_hex(node_session):
    node = await add_with_minted_id(node_session, POLLED_BLAH2, _polled_node)
    await node_session.commit()

    assert system_of(node.node_id) == POLLED_BLAH2
    stored = (await node_session.execute(select(Node.node_id))).scalars().all()
    assert stored == [node.node_id]


async def test_a_clash_mints_again_and_keeps_the_callers_work(node_session, monkeypatch):
    node_session.add(_polled_node("bla0a1b2c3d"))
    node_session.add(Node(node_id="ret1a2b3c4d", node_ref=mint_node_ref(), board_model="raspberrypi5-4gb"))
    await node_session.flush()
    calls = _hex_sequence(monkeypatch, "0a1b2c3d", "4e5f6a7b")

    node = await add_with_minted_id(node_session, POLLED_BLAH2, _polled_node)
    await node_session.commit()

    assert node.node_id == "bla4e5f6a7b"
    assert calls == ["0a1b2c3d", "4e5f6a7b"]
    stored = (await node_session.execute(select(Node.node_id).order_by(Node.node_id))).scalars().all()
    assert stored == ["bla0a1b2c3d", "bla4e5f6a7b", "ret1a2b3c4d"]


async def test_a_clash_on_the_first_statement_of_a_transaction_mints_again(node_session, monkeypatch):
    # A registration request's session is fresh, so the savepoint is what opens
    # the transaction: the case pysqlite's deferred BEGIN is known to mishandle.
    node_session.add(_polled_node("bla0a1b2c3d"))
    await node_session.commit()
    _hex_sequence(monkeypatch, "0a1b2c3d", "4e5f6a7b")

    node = await add_with_minted_id(node_session, POLLED_BLAH2, _polled_node)
    await node_session.commit()

    assert node.node_id == "bla4e5f6a7b"
    stored = (await node_session.execute(select(Node.node_id).order_by(Node.node_id))).scalars().all()
    assert stored == ["bla0a1b2c3d", "bla4e5f6a7b"]


async def test_the_last_clash_is_raised_once_the_attempts_run_out(node_session, monkeypatch):
    node_session.add(_polled_node("bla0a1b2c3d"))
    await node_session.flush()
    calls = _hex_sequence(monkeypatch, "0a1b2c3d")

    with pytest.raises(IntegrityError):
        await add_with_minted_id(node_session, POLLED_BLAH2, _polled_node)

    assert len(calls) == MINT_ATTEMPTS
    await node_session.commit()
    stored = (await node_session.execute(select(Node.node_id))).scalars().all()
    assert stored == ["bla0a1b2c3d"]


async def test_a_fault_in_the_callers_own_rows_is_not_retried_as_a_clash(node_session, monkeypatch):
    node_session.add(_polled_node("bla0a1b2c3d"))
    await node_session.commit()
    node_session.add(_polled_node("bla0a1b2c3d"))
    calls = _hex_sequence(monkeypatch, "4e5f6a7b")

    with pytest.raises(IntegrityError):
        await add_with_minted_id(node_session, POLLED_BLAH2, _polled_node)

    assert calls == []


async def test_fleet_ids_are_never_minted(node_session):
    with pytest.raises(ValueError, match="not minted"):
        await add_with_minted_id(node_session, FLEET, _polled_node)
