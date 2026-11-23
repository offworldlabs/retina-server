/* ------------------------------------------------------------------ */
/*  The detection fixtures: cuts of real GET /api/data/detections     */
/*  answers, captured in process (FastAPI's TestClient) from a store  */
/*  seeded with seed_tracks.py --light and seed_detections.py (day    */
/*  2026-10-18, rng seed 20261018), the seed the tracks fixtures in   */
/*  scenarios.ts come from. Each file is one captured answer: from,   */
/*  to, fleet, truncated, local and pending as captured, the rows of  */
/*  the chosen nodes and hexes in a short sub-window, count the rows  */
/*  kept, each row the server's bytes.                                */
/*                                                                    */
/*  All fleet=real (the explorer's only fleet):                       */
/*    2000z-detections.json                 [19:57:00Z, 20:07:00Z),   */
/*        the window replay asks for at 19:57:05Z. Rows of            */
/*        [19:57:00Z, 19:57:15Z), every node: tagged a1dda8 (track A1 */
/*        of 2000z-known.json), tagged a7a48e (the aircraft of the    */
/*        private node's track, which the tracks route drops while    */
/*        public nodes' detections of it are served), and untagged.   */
/*    2000z-detections-truncated.json       [19:45:00Z, 19:55:00Z),   */
/*        the route's row cap patched to 100 in process: cut, kept   */
/*        whole. Its last row is at 19:48:50.632Z, with three rows.   */
/*    2000z-detections-after-truncation.json [19:48:50.632Z,          */
/*        19:52:40.632Z), the window replay asks for next (from the   */
/*        last row, as long as the stretch served), whole. Rows of    */
/*        its first 10 s, every node and hex.                         */
/*    2000z-detections-not-local.json       [19:57:00Z, 20:07:00Z),   */
/*        with an object store configured (the seeded day is past     */
/*        the 3-day local retention): local false. Rows of one node   */
/*        in [19:57:00Z, 19:57:05Z).                                  */
/*    2000z-detections-pending.json         [19:57:00Z, 20:07:00Z),   */
/*        answered with the server's clock patched to 20:00:01Z and   */
/*        its last archive write to 19:00:04Z, the 20:00 write not    */
/*        run yet: pending true. Rows of what the archive held then,  */
/*        frames before that write: none.                             */
/*    2000z-detection-nodes.json  the six public nodes as an owner's  */
/*        map holds them in its registry (nodeListing.ts): the true   */
/*        receiver, the transmitter, a 360-degree beam and 160 to     */
/*        190 km of range, from the seeded node_configs rows. A       */
/*        visitor's registry holds the published receiver the rows    */
/*        carry; with the fleet's default 42-degree, 50 km area most  */
/*        of these arcs miss their aircraft.                          */
/* ------------------------------------------------------------------ */

import type { NodeGeometry } from "../../bistaticArc";
import type { DetectionsResponse } from "../types";
import tagged from "./2000z-detections.json";
import truncated from "./2000z-detections-truncated.json";
import afterTruncation from "./2000z-detections-after-truncation.json";
import notLocal from "./2000z-detections-not-local.json";
import pending from "./2000z-detections-pending.json";
import nodes from "./2000z-detection-nodes.json";

const response = (json: unknown) => json as DetectionsResponse;

export const DETECTIONS = {
  /** Tagged (a replayed track's and a dropped track's aircraft) and untagged rows, whole and local. */
  tagged: response(tagged),
  /** An answer the route cut at its row cap. */
  truncated: response(truncated),
  /** The whole answer for the window after the cut one. */
  afterTruncation: response(afterTruncation),
  /** An answer for a window past the server's local retention. */
  notLocal: response(notLocal),
  /** The answer for the tagged window before the archive's hourly write reached it: pending, no rows yet. */
  pending: response(pending),
};

/** The instant replay asks for 2000z-detections.json's window at. */
export const DETECTIONS_CLOCK_MS = Date.UTC(2026, 9, 18, 19, 57, 5);

/** The node registry of the six public nodes, as an owner's map holds it. */
export const DETECTION_NODES = nodes as Record<string, NodeGeometry>;
