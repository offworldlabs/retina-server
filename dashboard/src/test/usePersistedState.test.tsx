import { describe, it, expect } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { usePersistedState } from "../hooks/usePersistedState";
import { readStored, writeStored } from "../utils/storage";

function stubBrokenStorage() {
  const refuse = () => {
    throw new Error("SecurityError");
  };
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    writable: true,
    value: { getItem: refuse, setItem: refuse },
  });
}

describe("usePersistedState", () => {
  it("starts from the initial value and keeps nothing until it changes", () => {
    const { result } = renderHook(() => usePersistedState("retina.map.x", 1));
    expect(result.current[0]).toBe(1);
    expect(window.localStorage.getItem("retina.map.x")).toBeNull();
    act(() => result.current[1](2));
    expect(window.localStorage.getItem("retina.map.x")).toBe("2");
  });

  it("reads what it stored", () => {
    window.localStorage.setItem("retina.map.x", JSON.stringify({ a: 1 }));
    const { result } = renderHook(() => usePersistedState("retina.map.x", {}));
    expect(result.current[0]).toEqual({ a: 1 });
  });

  it("falls back to the legacy key and copies its value across", () => {
    window.localStorage.setItem("tf.x", "false");
    const { result } = renderHook(() => usePersistedState("retina.map.x", true, "tf.x"));
    expect(result.current[0]).toBe(false);
    expect(window.localStorage.getItem("retina.map.x")).toBe("false");
  });

  it("prefers the current key over the legacy one", () => {
    window.localStorage.setItem("retina.map.x", "true");
    window.localStorage.setItem("tf.x", "false");
    const { result } = renderHook(() => usePersistedState("retina.map.x", false, "tf.x"));
    expect(result.current[0]).toBe(true);
  });

  it("uses the initial value for a stored value it cannot parse", () => {
    window.localStorage.setItem("retina.map.x", "{not json");
    const { result } = renderHook(() => usePersistedState("retina.map.x", 7));
    expect(result.current[0]).toBe(7);
  });

  it("still works as state when storage refuses", () => {
    stubBrokenStorage();
    const { result } = renderHook(() => usePersistedState("retina.map.x", 1, "tf.x"));
    expect(result.current[0]).toBe(1);
    act(() => result.current[1](2));
    expect(result.current[0]).toBe(2);
  });
});

describe("readStored and writeStored", () => {
  it("round-trip a raw string", () => {
    writeStored("retina.theme", "dark");
    expect(window.localStorage.getItem("retina.theme")).toBe("dark");
    expect(readStored("retina.theme")).toBe("dark");
  });

  it("read nothing and keep nothing when storage refuses, without throwing", () => {
    stubBrokenStorage();
    expect(readStored("retina.theme")).toBeNull();
    expect(() => writeStored("retina.theme", "dark")).not.toThrow();
  });
});
