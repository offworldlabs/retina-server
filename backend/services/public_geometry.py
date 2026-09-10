"""Receiver-relative measurements withheld from unauthenticated payloads.

Every published receiver coordinate is displaced by services/public_location.py.
A quantity measured from a node's TRUE receiver to a point the same payload
gives the position of hands that displacement straight back, and enough of
them intersect well inside it. The line, applied field by field below: a
per-node constant is the envelope and may be published; a per-record value
that varies with the true geometry is a measurement and is withheld. So a beam
entry keeps `max_range_km`, `max_bistatic_range_km` and `half_width_deg`,
which /api/radar/analytics already carries per node, and `rule`, which names
which of them a refused solve breached and so refines an exclusion rather than
bounding the receiver.

The pass is structural over containers, so a field named below is withheld at
any nesting depth. It matches on the leaf key alone, though, not on shape or
position: the same quantity published under a name this module has not seen
stays published until that name is added.
"""

from __future__ import annotations

from typing import Any

# Withheld wherever they appear.
_RECEIVER_RELATIVE = frozenset(
    {
        # Beam-gate margins (solve records, beam_failures[]): a range and a
        # bearing from the true receiver to an aircraft the same record
        # locates. bistatic_km joins them because the transmitter is published
        # untranslated, so the differential range fixes the receiver on a
        # hyperbola through two known foci.
        "range_km",
        "bearing_off_deg",
        "bistatic_km",
        # Learned-FOV read-outs at the true bearing. The curve they are read
        # off is itself published, as `empirical_polygon` on
        # /api/radar/analytics, so a value off it inverts to the bearing it was
        # read at; "closed" is the same channel at three-value resolution.
        "fov_limit_km",
        "fov_state",
        # FOV shadow-mode verdicts. Containment tests at the true bearing and
        # range, stamped for the nodes that PASSED as well as the ones that
        # failed, so each bounds the receiver to a region where a failure would
        # only exclude one.
        "fov_verdict",
        "today_pass",
        # Cluster contamination. Membership is _point_in_beam against the true
        # geometry and the record publishes that point beside the verdict, so
        # it is another labelled sample of the published beam.
        #
        # /api/test/solver-stats does not route through this pass, and must
        # not: its contamination.contaminated is a windowed count sharing the
        # name rather than this field, and the pass would delete it.
        "foreign_node_ids",
        "contaminated",
        # Real aircraft fixes each carrying their distance from the true
        # receiver: a ranging circle per detection, and three intersect.
        "furthest_detections",
        # Per-node verification tracks. measured_delay_us is the bistatic range
        # from the true receiver to the truth position beside it, and
        # delay_match_us its residual against the range predicted from that
        # position, so the predicted one follows from the pair.
        "measured_delay_us",
        "delay_match_us",
        # The angle at the truth position between the published transmitter and
        # the true receiver of the worst-geometry contributing node, which
        # names the direction from a known point towards a receiver.
        "max_bistatic_angle_deg",
    }
)

# Withheld additionally from a payload scoped to ONE node, where a track
# position is that node's own single-node solve. The aircraft feed publishes
# those same two fields displaced with the icon (services/track_gates.py), so
# the true frame beside them is the displacement by one subtraction. A
# multinode payload keeps them: no single receiver is behind that position.
_NODE_SCOPED = _RECEIVER_RELATIVE | {"solver_lat", "solver_lon"}


def _stripped(value: Any, fields: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return {k: _stripped(v, fields) for k, v in value.items() if k not in fields}
    if isinstance(value, list | tuple):
        return [_stripped(v, fields) for v in value]
    return value


def without_receiver_geometry(value: Any, *, node_scoped: bool = False) -> Any:
    """`value` with the receiver-relative measurements taken out, at any depth.

    `node_scoped` for a payload that answers for one node, where a track
    position is that node's own.

    Copies rather than editing in place, so the caller's own structure keeps
    the geometry: the per-node verification store and the rolling MLAT sample
    buffer hold what the route may not publish. Tuples come back as lists,
    which is what they would have serialised as anyway.
    """
    return _stripped(value, _NODE_SCOPED if node_scoped else _RECEIVER_RELATIVE)
