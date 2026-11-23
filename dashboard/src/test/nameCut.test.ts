import { describe, expect, it } from "vitest";
import { NAME_TAIL, cutName } from "../utils/nameCut";

describe("cutName", () => {
  it("keeps the last 16 characters as the tail and the rest as the head", () => {
    expect(NAME_TAIL).toBe(16);
    expect(cutName("part-133334-29b7c2de562f4b548b33c8a2af9ba145.parquet")).toEqual({
      head: "part-133334-29b7c2de562f4b548b33c8a2",
      tail: "af9ba145.parquet",
    });
  });

  it("gives a 16-character name an empty head", () => {
    expect(cutName("checkpoint012345")).toEqual({ head: "", tail: "checkpoint012345" });
  });

  it("gives a shorter name an empty head", () => {
    expect(cutName("a.json")).toEqual({ head: "", tail: "a.json" });
  });

  it("cuts a name with an early dot by length, not at the dot", () => {
    expect(cutName("a.very-long-descriptive-suffix.json")).toEqual({
      head: "a.very-long-descrip",
      tail: "tive-suffix.json",
    });
  });
});
