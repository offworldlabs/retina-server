import { describe, expect, it } from "vitest";

import { ownedNode } from "../../test/fixtures/ownedNode";
import { isOwnerEntry, ownedMapNodes, radarNodeFrom, withOwnedNodes } from "./nodeListing";

const area = (lat: number, lon: number, rx: object = {}) => ({
  rx: { lat, lon, ...rx },
  tx: { lat: lat + 0.2, lon: lon - 0.1 },
  beam_azimuth_deg: 90,
  beam_width_deg: 40,
  max_range_km: 60,
});

const OTHER = { is_synthetic: false, detection_area: area(51, -1, { location_uncertainty_km: 3 }) };
const ROOF_PUBLISHED = { is_synthetic: false, detection_area: area(52.004, -0.512, { location_uncertainty_km: 3 }) };
const OWNER_ANALYTICS = {
  nodes: {
    "nde-roof": { detection_area: area(52.1357, -0.4321) },
    "nde-shed": { detection_area: area(53.1234, -1.5678) },
  },
};
const ROOF = ownedNode({ node_id: "retroof0001", node_ref: "nde-roof", location_private: false });
const SHED = ownedNode({ node_id: "retshed0001", node_ref: "nde-shed", location_private: true });

describe("isOwnerEntry", () => {
  it("tells the owner's entries, the only ones at a true position, from the public listing's", () => {
    const listed = [radarNodeFrom("nde-other", OTHER, false)!, radarNodeFrom("nde-roof", ROOF_PUBLISHED, false)!];
    const owned = ownedMapNodes([ROOF, SHED], OWNER_ANALYTICS as never, "real");
    expect(listed.map(isOwnerEntry)).toEqual([false, false]);
    // The node the owner publishes is the owner's entry all the same: the map
    // holds it at its true position, not the published one.
    expect(owned.map((n) => [n.node_ref, n.location_private, isOwnerEntry(n)])).toEqual([
      ["nde-roof", false, true],
      ["nde-shed", true, true],
    ]);
    // Merged into the listing, each keeps what it is.
    expect(withOwnedNodes(listed, owned).map((n) => [n.node_ref, isOwnerEntry(n)])).toEqual([
      ["nde-other", false],
      ["nde-roof", true],
      ["nde-shed", true],
    ]);
  });
});
