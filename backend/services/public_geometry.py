"""Receiver-relative measurements withheld from unauthenticated payloads.

Every published receiver coordinate is displaced by services/public_location.py.
A quantity measured from a node's TRUE receiver to a point the same payload
gives the position of hands that displacement straight back: a range and a
bearing to a known point is a fix, and an in-or-out verdict against a shape
/api/radar/analytics publishes per node is a labelled sample of the same
thing. Enough of either intersect well inside the displacement.

The line, applied field by field below: a per-node constant is the envelope and
may be published; a per-record value that varies with the true geometry is a
measurement and is withheld. Every unauthenticated diagnostic payload that
names a node passes through `without_receiver_geometry`, and the pass is
structural, so a field that appears under a new key or one level deeper than
the fields here is withheld by the same walk rather than by a per-route list
someone has to remember to widen.

What stays on a beam entry is `max_range_km`, `max_bistatic_range_km`,
`half_width_deg` and `rule`. The first three are the per-node constants
/api/radar/analytics already carries in its `detection_area` block.  `rule`
names which of them the entry breached, on an entry that exists only because
the node refused the solve: it refines an exclusion rather than bounding the
receiver, which is what separates it from the verdicts below.
"""

from __future__ import annotations

# Withheld wherever they appear.
_RECEIVER_RELATIVE = frozenset(
    {
        # Beam-gate margins (solve records, beam_failures[]): a range and a
        # bearing from the true receiver to an aircraft the same record
        # locates. bistatic_km goes with them because the transmitter is
        # published untranslated, so the differential range fixes the receiver
        # on a hyperbola through two known foci.
        "range_km",
        "bearing_off_deg",
        "bistatic_km",
        # Learned-FOV read-outs at the true bearing (beam_failures[]). The
        # coverage they are read out of is itself published, as
        # `empirical_polygon` on /api/radar/analytics, so a value read off that
        # curve inverts to the bearing it was read at; "closed" is the same
        # channel at three-value resolution.
        "fov_limit_km",
        "fov_state",
        # FOV shadow-mode verdicts (solve records, fov_verdict[]). Both are
        # containment tests at the true bearing and range, stamped for the
        # nodes that PASSED as well as the ones that failed, so each bounds the
        # receiver to a region where a failure only excludes one.
        "fov_verdict",
        "today_pass",
        # Cluster contamination (solve records). Membership is
        # _point_in_beam(gt_lat, gt_lon, true NodeGeometry) and the record
        # publishes that point beside the verdict, which is another labelled
        # sample against the published beam. /api/test/solver-stats reads the
        # stamp from the store rather than from a published record, so the
        # contamination block is unaffected.
        "foreign_node_ids",
        "contaminated",
        # Per-node verification tracks: measured_delay_us is the bistatic range
        # from the true receiver to the truth position beside it, and
        # delay_match_us its residual against the range predicted from that
        # position, so the predicted one follows from the pair. Each entry is a
        # locus the receiver sits on.
        "measured_delay_us",
        "delay_match_us",
        # MLAT verification tracks: the angle at the truth position between the
        # published transmitter and the true receiver of the worst-geometry
        # contributing node, so it names the direction from a known point
        # towards a receiver.
        "max_bistatic_angle_deg",
    }
)

# Withheld additionally from a payload scoped to ONE node, where a track
# position is that node's own single-node solve. The aircraft feed publishes
# those same two fields displaced with the icon (services/track_gates.py), so
# the true frame beside them is the displacement by one subtraction. A
# multinode payload keeps them: no single receiver is behind that position.
_NODE_SCOPED = _RECEIVER_RELATIVE | {"solver_lat", "solver_lon"}


def _stripped(value, fields: frozenset[str]):
    if isinstance(value, dict):
        return {k: _stripped(v, fields) for k, v in value.items() if k not in fields}
    if isinstance(value, list | tuple):
        return [_stripped(v, fields) for v in value]
    return value


def without_receiver_geometry(value, *, node_scoped: bool = False):
    """`value` with the receiver-relative measurements taken out, at any depth.

    `node_scoped` for a payload that answers for one node, where a track
    position is that node's own.

    Copies rather than editing in place: the caller's own structure keeps the
    geometry, which is what lets the per-node verification store and the
    rolling MLAT sample buffer hold what the route may not publish. Tuples come
    back as lists, which is what they would have serialised as anyway.
    """
    return _stripped(value, _NODE_SCOPED if node_scoped else _RECEIVER_RELATIVE)
