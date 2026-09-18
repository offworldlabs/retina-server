import { describe, expect, it } from "vitest";

import {
  defaultFilters,
  type ExplorerFilters,
  readFilters,
  writeFilters,
} from "../../pages/user/dataExplorer/urlState";

const TODAY = "2026-09-17";

describe("defaultFilters", () => {
  it("opens on the last three days, everything else unset", () => {
    const f = defaultFilters(TODAY);
    expect(f).toEqual({
      nodeSel: null,
      from: "2026-09-15",
      to: TODAY,
      todFrom: "00:00",
      todTo: "23:59",
      minSize: 0,
      near: null,
    });
  });
});

describe("readFilters", () => {
  it("falls back to the defaults for an empty query", () => {
    expect(readFilters("", TODAY)).toEqual(defaultFilters(TODAY));
  });

  it("collects repeated node parameters", () => {
    expect(readFilters("?node=a&node=b", TODAY).nodeSel).toEqual(new Set(["a", "b"]));
  });

  it("reads no node parameter as every node, not as none", () => {
    expect(readFilters("?from=2026-09-01", TODAY).nodeSel).toBeNull();
  });

  it("ignores a date that is not a plain ISO day", () => {
    const f = readFilters("?from=last-tuesday&to=2026-09-16", TODAY);
    expect(f.from).toBe("2026-09-15");
    expect(f.to).toBe("2026-09-16");
  });

  it("swaps an inverted range rather than showing nothing", () => {
    const f = readFilters("?from=2026-09-16&to=2026-09-10", TODAY);
    expect(f.from).toBe("2026-09-10");
    expect(f.to).toBe("2026-09-16");
  });

  it("reads a time-of-day window", () => {
    const f = readFilters("?tod=06:00-09:30", TODAY);
    expect(f.todFrom).toBe("06:00");
    expect(f.todTo).toBe("09:30");
  });

  it("ignores a malformed time-of-day window", () => {
    const f = readFilters("?tod=6-9", TODAY);
    expect(f.todFrom).toBe("00:00");
    expect(f.todTo).toBe("23:59");
  });

  it("reads a radius filter and floors the radius", () => {
    expect(readFilters("?near=51.5,-0.12,40", TODAY).near).toEqual({
      lat: 51.5,
      lon: -0.12,
      km: 40,
    });
    expect(readFilters("?near=51.5,-0.12,0.5", TODAY).near.km).toBe(2);
  });

  it("ignores a radius filter that is not three numbers", () => {
    expect(readFilters("?near=51.5,-0.12", TODAY).near).toBeNull();
    expect(readFilters("?near=a,b,c", TODAY).near).toBeNull();
  });

  it("ignores a minimum size that is zero or junk", () => {
    expect(readFilters("?minsize=4096", TODAY).minSize).toBe(4096);
    expect(readFilters("?minsize=0", TODAY).minSize).toBe(0);
    expect(readFilters("?minsize=lots", TODAY).minSize).toBe(0);
  });
});

describe("writeFilters", () => {
  const base = (over: Partial<ExplorerFilters> = {}): ExplorerFilters => ({
    ...defaultFilters(TODAY),
    ...over,
  });

  it("always carries the range and nothing else by default", () => {
    expect(writeFilters(base()).toString()).toBe("from=2026-09-15&to=2026-09-17");
  });

  it("writes nodes sorted, so the same selection gives the same link", () => {
    const qs = writeFilters(base({ nodeSel: new Set(["b", "a"]) }));
    expect(qs.getAll("node")).toEqual(["a", "b"]);
  });

  it("omits a default time-of-day window and a zero minimum size", () => {
    const qs = writeFilters(base()).toString();
    expect(qs).not.toContain("tod=");
    expect(qs).not.toContain("minsize=");
  });

  it("quantises the radius centre to four decimals", () => {
    const qs = writeFilters(base({ near: { lat: 51.500049, lon: -0.1276, km: 40 } }));
    expect(qs.get("near")).toBe("51.5000,-0.1276,40");
  });

  it("round-trips a fully populated filter set", () => {
    const f = base({
      nodeSel: new Set(["ret-a"]),
      from: "2026-09-01",
      to: "2026-09-03",
      todFrom: "06:00",
      todTo: "09:30",
      minSize: 4096,
      near: { lat: 51.5, lon: -0.1276, km: 40 },
    });
    expect(readFilters(`?${writeFilters(f)}`, TODAY)).toEqual(f);
  });
});

describe("an empty selection", () => {
  const empty = (): ExplorerFilters => ({ ...defaultFilters(TODAY), nodeSel: new Set<string>() });

  it("round-trips, because it means none and not every node", () => {
    const back = readFilters(writeFilters(empty()).toString(), TODAY);
    expect(back.nodeSel).toBeInstanceOf(Set);
    expect(back.nodeSel!.size).toBe(0);
  });

  it("is spelled differently from selecting everything", () => {
    expect(writeFilters(empty()).toString()).not.toBe(
      writeFilters(defaultFilters(TODAY)).toString(),
    );
  });

  it("leaves a named selection alone", () => {
    const named = { ...defaultFilters(TODAY), nodeSel: new Set(["ret-a", "ret-b"]) };
    const back = readFilters(writeFilters(named).toString(), TODAY);
    expect([...back.nodeSel!].sort()).toEqual(["ret-a", "ret-b"]);
  });
});
