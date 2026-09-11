# Node sites: a receiver near a site is that site

Date: 2026-09-11. Branch `feat/node-site-proximity-gate`.

## Problem

`services/node_sites.py` (#286) publishes co-located receivers at one point by
hashing the *site* rather than the node, where a site was defined as exact
equality of the configured rx coordinates at 6 decimals. The rule was chosen
over proximity on purpose: a node's offset must not depend on its neighbours.

The fleet then produced the case the rule cannot see. On the test droplet the
co-location audit has logged, every cycle, a fourth receiver configured 56 m
from three at one address, and a receiver 16 m from a pair at another. Each is
a separate site under exact equality, so each publishes its own donut draw:
the map shows a second marker about 1 km from the first (the difference of two
independent draws, not the true 56 m), and an attacker holds two samples of one
address. At the shipped [0.5, 1.0] km donut that shrinks the consistent region
from 2.36 km² to 0.69 km² — the loss the module exists to prevent.

The audit's own advice, "align the configured rx_lat/rx_lon", fixes the
symptom by editing the physics input: the node would then be solved against a
position tens of metres wrong. The fix belongs at the publication edge.

## Decision

Two rules, applied in order, resolve every node to a site:

1. **Exact equality**, unchanged: nodes configured at the same coordinates
   form a site anchored on the lowest node id. These sites are formed first
   and their anchors are frozen.
2. **Proximity**: a node alone at its coordinates joins the nearest existing
   anchor within `NODE_FUZZ_SITE_KM` (150 m, the distance the audit already
   used), or becomes an anchor itself. The pass is greedy over node ids in
   sorted order, not a transitive closure.

A node joined by rule 2 is **published from the anchor's position**, not from
its own with a shared offset. Sharing only the offset would leave two markers
56 m apart and put the true baseline between the receivers on the wire.
Everything derived from the node's true position — coverage polygon,
ambiguity arc, single-node track trail — moves by the same total delta (join
shift plus fuzz offset), so the published artefacts stay rigid around the
published marker.

`location_uncertainty_km` widens by the radius for every member of a site that
has a joined member. A joined receiver can be up to 150 m further from the
published point than the donut allows, the site publishes one point, and one
honest radius belongs to it.

## Why the original objection no longer holds

*A neighbour disconnecting would move a node.* Positions are remembered, never
dropped, since #286; a disconnect changes nothing.

*A chain of neighbours would merge a street.* Greedy assignment bounds every
member to within the radius of its own anchor. Four houses 100 m apart in a
line become two sites of two, not one site spanning 300 m.

*Connection order would decide the anchor.* The pass runs over sorted ids from
the remembered position map, so the result is a function of configuration
alone. What remains is the case exact equality always had: a newcomer with a
lower id at an existing site would become its anchor and move it. Rule 1
runs first precisely so that a site already published at one point cannot be
re-anchored by a lower-id near neighbour; only a site that was already
publishing two samples can be moved, once, to publish one.

## What changes on deploy

Only nodes the audit was already reporting as near misses move: on the test
droplet, exactly the 56 m and 16 m cases. Every node alone at its coordinates
hashes its own id from its own position as before, and every exact-equality
site keeps its anchor and its point. Before deploying to staging or
production, read that environment's `node_sites:` log lines: the near-miss
list is exactly the set of nodes that will move.

Rejected alternatives: quantising coordinates to a grid (boundary splits, and
strangers in one cell merge for no reason); frontend-only clustering (hides
the marker, leaves both samples in every API and the permanent archive); a
persistent site table (the right eventual shape, but `node_sites` has no write
path today and the stability it buys is what the frozen first pass already
gives). An owner-declared "same site as node X" remains the intended override
for the case no rule can decide — two receivers 60 m apart that really are two
houses — and is not blocked by this change.

`NODE_FUZZ_SITE_AUDIT_KM` is still read as the older name of the radius.
Setting the radius to 0 restores exact equality alone. `NODE_FUZZ_SALT`,
`_ORIGINAL_FRAME` and the lone-node identity are untouched.
