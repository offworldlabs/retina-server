/* ------------------------------------------------------------------ */
/*  The replay fixtures: cuts of real GET /api/data/tracks responses, */
/*  captured from a local stack seeded with seed_tracks.py (day       */
/*  2026-10-18, rng seed 20261018). Each file is one captured         */
/*  response kept to some of its tracks and to the rows solved in the */
/*  file's own [from, to): fleet and truncated as captured, count the */
/*  rows kept, each row the server's bytes. Tracks are picked by      */
/*  track_key or adsb_hex; legacy dark rows carry neither, and are    */
/*  picked as the seed manifest's track.                              */
/*                                                                    */
/*  Captured 2026-10-18 [19:45Z, 20:15Z), fleet=real (new schema):    */
/*    2000z-known.json          [19:55:00Z, 20:00:00Z)  A1, A2        */
/*    2000z-dark.json           [19:54:30Z, 20:01:45Z)  A5 to its     */
/*                                                      gap, A8       */
/*    2000z-dark-to-follow.json [19:48:00Z, 19:51:00Z)  A3            */
/*    2000z-anomalous.json      [20:01:30Z, 20:05:30Z)  A4            */
/*    2000z-dark-remint.json    [20:00:00Z, 20:03:00Z)  A5, both keys */
/*    2000z-known-gap.json      [19:57:00Z, 20:01:30Z)  A6            */
/*  The same window, fleet=all:                                       */
/*    2000z-all-mixed.json      [19:53:00Z, 19:55:00Z)  A_mixed, A1,  */
/*                       mn-adsb-ab2139 and its follow-lane neighbour */
/*                       mn-dark-1792352756862-35.251--81.239, both   */
/*                       synthetic                                    */
/*  Captured 2026-10-18 [09:45Z, 10:15Z), fleet=real (legacy rows):   */
/*    1000z-legacy-adsb.json    [09:57:00Z, 10:00:30Z)  B1, B2        */
/*    1000z-legacy-linking.json [09:59:00Z, 10:02:30Z)  B3, B6        */
/*    1000z-legacy-split.json   [09:58:00Z, 10:01:30Z)  B4, B5        */
/*  Captured 2026-10-18 [14:00Z, 14:30Z), fleet=real (500 tracks):    */
/*    1400z-500-sample.json     [14:15:00Z, 14:15:30Z)  a known, a    */
/*                           dark and a dark-to-follow track, all     */
/*                           anomalous                                */
/*                                                                    */
/*  A1..A8, B1..B6 and A_mixed are the manifest's labels. What the    */
/*  tests cannot read off the rows is below: which file is which      */
/*  scenario, and what must never be in one. The rest (identities,    */
/*  gaps, counts) the tests derive from the rows.                     */
/* ------------------------------------------------------------------ */

import type { TracksResponse } from "../types";
import known from "./2000z-known.json";
import dark from "./2000z-dark.json";
import follow from "./2000z-dark-to-follow.json";
import anomalous from "./2000z-anomalous.json";
import darkRemint from "./2000z-dark-remint.json";
import knownGap from "./2000z-known-gap.json";
import allMixed from "./2000z-all-mixed.json";
import legacyAdsb from "./1000z-legacy-adsb.json";
import legacyLinking from "./1000z-legacy-linking.json";
import legacySplit from "./1000z-legacy-split.json";
import benchSample from "./1400z-500-sample.json";

const response = (json: unknown) => json as TracksResponse;

/** Keyed rows (track_key set), fleet=real. */
export const KEYED = {
  /** A known track (mn-adsb-*, lane known) and a dark track alive at the same time. */
  known: response(known),
  /** Two dark tracks; one ends mid-window (its key's last solve) while the other carries on. */
  dark: response(dark),
  /** A dark key whose solves move to the follow lane after its first few: one key, two lanes. */
  follow: response(follow),
  /** A known track that turns anomalous mid-cut and later adds a second anomaly type. */
  anomalous: response(anomalous),
  /** A dark track silent past the dark expiry: the server mints a new key for its solves after the silence. */
  darkRemint: response(darkRemint),
  /** One known track silent for longer than the ADS-B expiry, shorter than a trail's life, under one key. */
  knownGap: response(knownGap),
};

/** Legacy rows (no track_key, no lane), fleet=real. */
export const LEGACY = {
  /** Two aircraft known only by adsb_hex; one is silent past every link limit mid-cut. */
  adsb: response(legacyAdsb),
  /** Two concurrent dark targets more than 3 km apart; one is silent over 30 s, which must link. */
  linking: response(legacyLinking),
  /** Two dark runs within 3 km of each other but more than 60 s apart. */
  split: response(legacySplit),
};

/** Keyed rows of fleet=all: real, synthetic and mixed (real and synthetic contributors) tracks together. */
export const FLEET_ALL = {
  mixed: response(allMixed),
};

/** Real rows of the 500-track window generate500 stands in for. */
export const BENCH_SAMPLE = response(benchSample);

/** Every fixture file. */
export const FIXTURES: Record<string, TracksResponse> = {
  ...Object.fromEntries(Object.entries(KEYED).map(([name, resp]) => [`keyed ${name}`, resp])),
  ...Object.fromEntries(Object.entries(LEGACY).map(([name, resp]) => [`legacy ${name}`, resp])),
  "fleet=all mixed": FLEET_ALL.mixed,
  "bench sample": BENCH_SAMPLE,
};

/** How many tracks linking should find in each linking scenario. */
export const LINKED_TRACKS = { linking: 2, split: 2 };

/**
 * The seed's track whose every solve includes its private node (manifest,
 * absent_from_every_fleet): the route drops all its rows, so no capture, and
 * no fixture, may hold any of it.
 */
export const PRIVATE_TRACK = { nodeRef: "ndegu1bjzzw6pde", trackKey: "mn-adsb-a7a48e", adsbHex: "a7a48e" };

/**
 * generate500's bench instant (2026-10-18T14:15:00Z): every one of its tracks
 * is visible here with at least 90 earlier rows, and stays visible for 25 s
 * either side.
 */
export const BENCH_INSTANT_MS = Date.UTC(2026, 9, 18, 14, 15, 0);
/** Half the span around BENCH_INSTANT_MS over which every generated track is visible. */
export const BENCH_HALF_SPAN_MS = 25_000;
