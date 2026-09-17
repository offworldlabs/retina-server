import { describe, expect, it } from "vitest";

import { FILE_SPAN_MS, parseKey } from "../../pages/user/dataExplorer/keys";

const MODIFIED = "2026-09-17T14:00:00Z";

describe("parseKey", () => {
  it("reads a Hive-partitioned key", () => {
    const f = parseKey(
      "year=2026/month=09/day=17/node_id=ret-abc/part-000.parquet",
      2048,
      MODIFIED,
    );
    expect(f).toMatchObject({
      node: "ret-abc",
      day: "2026-09-17",
      name: "part-000.parquet",
      size: 2048,
    });
  });

  it("reads a legacy key with no partition prefixes", () => {
    const f = parseKey("2026/09/17/ret-abc/detections.json", 10, MODIFIED);
    expect(f).toMatchObject({ node: "ret-abc", day: "2026-09-17" });
  });

  it("covers the hour ending at the flush, because only `modified` is known", () => {
    const f = parseKey("2026/09/17/ret-abc/a.json", 1, MODIFIED);
    expect(f.endMs).toBe(Date.parse(MODIFIED));
    expect(f.startMs).toBe(Date.parse(MODIFIED) - FILE_SPAN_MS);
  });

  it("rejects a key too short to name a node", () => {
    expect(parseKey("orphan.json", 1, MODIFIED)).toBeNull();
  });

  it("rejects a key with no day to bucket it under", () => {
    expect(parseKey("ret-abc/a.json", 1, MODIFIED)).toBeNull();
  });

  it("rejects a file whose modified time does not parse", () => {
    expect(parseKey("2026/09/17/ret-abc/a.json", 1, "not a date")).toBeNull();
  });

  it("treats a missing size as zero rather than NaN", () => {
    expect(parseKey("2026/09/17/ret-abc/a.json", undefined, MODIFIED).size).toBe(0);
  });

  it("ignores empty segments from a leading or doubled slash", () => {
    const f = parseKey("/2026/09/17//ret-abc/a.json", 1, MODIFIED);
    expect(f).toMatchObject({ node: "ret-abc", day: "2026-09-17" });
  });

  it("takes the node from the second-to-last segment, whatever the depth", () => {
    // Six segments, so the fourth is no longer the node. The archive route
    // reads the second-to-last and this has to match it.
    const f = parseKey(
      "year=2026/month=09/day=17/node_id=ret-abc/extra/part-000.parquet",
      1,
      MODIFIED,
    );
    expect(f.node).toBe("extra");
  });

  it("keeps everything after the first equals, as the route does", () => {
    const f = parseKey("2026/09/17/node_id=ret=abc/a.json", 1, MODIFIED);
    expect(f.node).toBe("ret=abc");
  });
});
