"""services/id_utils.py — the transponder-hex shape rule.

is_transponder_hex is the single gate that keeps non-transponder ids (above
all the simulator's ``obj-NNNNN`` object ids) out of the ADS-B world: it
guards the sim adsb push (routes/sim_ingest.py) and the mn-adsb-* keying rule
(services/tasks/solver.py:multinode_key_decision).  Callers pass values that
already went through normalize_hex_key, so the rule is defined over stripped
lowercase input.
"""

import pytest

from services.id_utils import is_transponder_hex


class TestIsTransponderHex:
    @pytest.mark.parametrize("value", ["a1b2c3", "abcdef", "000001", "~a1b2c3"])
    def test_transponder_shapes_pass(self, value):
        assert is_transponder_hex(value) is True

    @pytest.mark.parametrize(
        "value",
        [
            "obj-01373",  # the simulator object id that poisoned the lane
            "",
            None,
            "a1b2c",  # too short
            "a1b2c3d",  # too long
            "A1B2C3",  # not normalized — callers must normalize first
            "g1b2c3",  # not hex
            "~~a1b2c3",
            "mn-adsb-a1b2c3",
        ],
    )
    def test_everything_else_is_refused(self, value):
        assert is_transponder_hex(value) is False
