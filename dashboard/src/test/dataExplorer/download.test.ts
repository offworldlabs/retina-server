import { describe, expect, it } from "vitest";

import {
  CURL_EXCERPT_LINES,
  curlExcerpt,
  curlLine,
  curlScript,
  downloadUrl,
  manifest,
} from "../../pages/user/dataExplorer/download";
import type { ArchiveFile } from "../../pages/user/dataExplorer/keys";

const ORIGIN = "https://app.retina.fm";

const file = (node: string, name: string, endHour: number): ArchiveFile => ({
  key: `year=2026/month=09/day=17/node_ref=${node}/${name}`,
  name,
  node,
  day: "2026-09-17",
  size: 1024,
  endMs: Date.parse(`2026-09-17T${String(endHour).padStart(2, "0")}:00:00Z`),
  startMs: Date.parse(`2026-09-17T${String(endHour - 1).padStart(2, "0")}:00:00Z`),
});

describe("downloadUrl", () => {
  it("is absolute, on the download route, with the key unencoded", () => {
    expect(downloadUrl(file("ret-a", "part-000001.parquet", 6), ORIGIN)).toBe(
      "https://app.retina.fm/api/data/archive/year=2026/month=09/day=17/node_ref=ret-a/part-000001.parquet",
    );
  });

  it("defaults to the page's own origin", () => {
    expect(downloadUrl(file("ret-a", "a.parquet", 6))).toBe(
      `${window.location.origin}/api/data/archive/year=2026/month=09/day=17/node_ref=ret-a/a.parquet`,
    );
  });
});

describe("curlLine", () => {
  it("names the output after the node and the file's stem, as .json", () => {
    // Byte-significant: the route sets no Content-Disposition, so the explicit
    // -o is what stops a JSON body being written as part-000001.parquet.
    expect(curlLine(file("ret-a", "part-000001.parquet", 6), ORIGIN)).toBe(
      'curl -sS -o ret-a-part-000001.json "https://app.retina.fm/api/data/archive/year=2026/month=09/day=17/node_ref=ret-a/part-000001.parquet"',
    );
  });

  it("strips only the last extension", () => {
    expect(curlLine(file("ret-a", "part.v2.parquet", 6), ORIGIN)).toContain("-o ret-a-part.v2.json ");
  });
});

describe("manifest and curlScript", () => {
  const files = [file("ret-a", "a.parquet", 6), file("ret-b", "b.parquet", 7)];

  it("is one URL per line, in the order given", () => {
    expect(manifest(files, ORIGIN)).toBe(
      `${downloadUrl(files[0], ORIGIN)}\n${downloadUrl(files[1], ORIGIN)}`,
    );
  });

  it("is one curl per line, in the order given", () => {
    expect(curlScript(files, ORIGIN)).toBe(`${curlLine(files[0], ORIGIN)}\n${curlLine(files[1], ORIGIN)}`);
  });

  it("is empty for an empty basket", () => {
    expect(manifest([], ORIGIN)).toBe("");
    expect(curlScript([], ORIGIN)).toBe("");
  });
});

describe("curlExcerpt", () => {
  it("shows every line when there are few", () => {
    const files = [file("ret-a", "a.parquet", 6), file("ret-b", "b.parquet", 7)];
    expect(curlExcerpt(files, ORIGIN)).toBe(curlScript(files, ORIGIN));
  });

  it("shows the first few and says how many the copy takes", () => {
    const files = ["a", "b", "c", "d", "e"].map((n, i) => file(`ret-${n}`, `${n}.parquet`, 6 + i));
    const lines = curlExcerpt(files, ORIGIN).split("\n");
    expect(lines).toHaveLength(CURL_EXCERPT_LINES + 1);
    expect(lines.slice(0, CURL_EXCERPT_LINES)).toEqual(
      files.slice(0, CURL_EXCERPT_LINES).map((f) => curlLine(f, ORIGIN)),
    );
    expect(lines[CURL_EXCERPT_LINES]).toBe("# … 2 more; Copy curl takes all 5");
  });

  it("is empty for an empty basket", () => {
    expect(curlExcerpt([], ORIGIN)).toBe("");
  });
});
