import { describe, expect, it } from "vitest";

import { isSynthetic, type RegistryNode } from "../../pages/user/dataExplorer/nodes";
import { type MapScope, showOnMap } from "../../pages/user/dataExplorer/showOnMap";

const node = (id: string, synthetic: boolean): RegistryNode => ({
  id,
  name: id,
  status: "online",
  synthetic,
  lat: null,
  lon: null,
  uncertaintyKm: null,
});

/** The scope's registry: one real node and one the server flags synthetic. */
const REGISTRY = new Map([
  ["ndereal00000001", node("ndereal00000001", false)],
  ["ndesim000000001", node("ndesim000000001", true)],
]);

const scope = (over: Partial<MapScope> = {}): MapScope => ({
  scope: "network",
  privateRefs: new Set(),
  synthetic: (id) => isSynthetic(REGISTRY, id),
  ...over,
});

/** A file from a real, public node, published and with its frame times. */
const file = (over: Partial<Parameters<typeof showOnMap>[0]> = {}) => ({
  node: "ndereal00000001",
  firstFrameMs: Date.UTC(2026, 9, 18, 22, 14, 7, 500),
  publishedLat: 34.8521,
  publishedLon: -82.394,
  ...over,
});

const SYNTHETIC = "Synthetic node: the archive map shows the real fleet only.";
const PRIVATE = "Private node: the public map leaves it out.";
const NO_TIME = "This file's frame times are unknown.";
const NO_POSITION = "This node has no published position.";

describe("showOnMap", () => {
  it("opens the published position at the first frame, to the second", () => {
    expect(showOnMap(file(), scope())).toEqual({ href: "/map#lat=34.8521&lon=-82.3940&z=9&t=2026-10-18T22:14:07Z" });
  });

  it("leaves out a synthetic node, by the registry's flag or, unlisted, by its prefix", () => {
    expect(showOnMap(file({ node: "ndesim000000001" }), scope())).toEqual({ reason: SYNTHETIC });
    expect(showOnMap(file({ node: "synth-0001" }), scope())).toEqual({ reason: SYNTHETIC });
  });

  it("leaves out the owner's private node in their own scope, whatever its row says", () => {
    const own = scope({ scope: "mine", privateRefs: new Set(["ndereal00000001"]) });
    expect(showOnMap(file(), own)).toEqual({ reason: PRIVATE });
  });

  it("has no time for a file without a first frame, or one stamped at 0 or before 2020", () => {
    for (const firstFrameMs of [null, Number.NaN, Number.POSITIVE_INFINITY, 0, Date.UTC(2020, 0, 1) - 1]) {
      expect(showOnMap(file({ firstFrameMs }), scope())).toEqual({ reason: NO_TIME });
    }
    expect(showOnMap(file({ firstFrameMs: Date.UTC(2020, 0, 1) }), scope())).toEqual({
      href: "/map#lat=34.8521&lon=-82.3940&z=9&t=2020-01-01T00:00:00Z",
    });
  });

  it("has no place for a node without both published coordinates", () => {
    expect(showOnMap(file({ publishedLat: null, publishedLon: null }), scope())).toEqual({ reason: NO_POSITION });
    expect(showOnMap(file({ publishedLon: null }), scope())).toEqual({ reason: NO_POSITION });
    expect(showOnMap(file({ publishedLat: Number.NaN }), scope())).toEqual({ reason: NO_POSITION });
  });

  it("gives the first reason that applies", () => {
    const own = scope({ scope: "mine", privateRefs: new Set(["ndesim000000001", "ndereal00000001"]) });
    expect(showOnMap(file({ node: "ndesim000000001", firstFrameMs: null }), own)).toEqual({ reason: SYNTHETIC });
    expect(showOnMap(file({ firstFrameMs: null, publishedLat: null }), own)).toEqual({ reason: PRIVATE });
    expect(showOnMap(file({ firstFrameMs: 0, publishedLat: null }), scope())).toEqual({ reason: NO_TIME });
  });
});
