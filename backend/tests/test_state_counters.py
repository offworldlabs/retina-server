"""Tests for the counter roster in core.state.

The reset is derived from the declarations (state._COUNTER_ZEROS), so the risk
these guard is the derivation going wrong rather than an entry being forgotten:
a filter that quietly stops matching leaves counters leaking between tests, and
one that restores the wrong zero silently retypes the float timings.
"""

import ast

from core import state


def _declared_in_source() -> dict[str, str]:
    """Every module-level ``name: int = 0`` / ``name: float = 0.0`` in state.py.

    Read from the source rather than from __annotations__, which is the same
    mechanism the roster is built from and so would agree with it either way.
    """
    with open(state.__file__) as fh:
        tree = ast.parse(fh.read())
    out = {}
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.annotation, ast.Name)
            and node.annotation.id in ("int", "float")
            and isinstance(node.value, ast.Constant)
            and node.value.value == 0
        ):
            out[node.target.id] = node.annotation.id
    return out


def test_roster_covers_every_counter_declared_in_the_source():
    declared = _declared_in_source()
    assert set(declared) == set(state._COUNTER_ZEROS)
    # A filter that matched nothing would satisfy the equality above only by
    # emptying both sides, which is the leak this change exists to prevent.
    assert len(declared) > 100
    for name, annotation in declared.items():
        assert type(state._COUNTER_ZEROS[name]).__name__ == annotation, name


def test_reset_zeroes_every_counter_and_keeps_its_type():
    for name in state._COUNTER_ZEROS:
        setattr(state, name, 7)

    state._reset_for_tests()

    for name, zero in state._COUNTER_ZEROS.items():
        value = getattr(state, name)
        assert value == 0, name
        # The four float counters must not come back as int 0: routes/test.py
        # and routes/admin.py serialise them through round(), which preserves
        # the type, so an int zero would publish 0 where the payload has
        # always carried 0.0.
        assert type(value) is type(zero), name
