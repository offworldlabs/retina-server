import { describe, it, expect, beforeEach } from "vitest";
import { act, renderHook } from "@testing-library/react";
import { usePersistedState } from "../hooks/usePersistedState";
import { readStored, writeStored } from "../utils/storage";

/** Stubbed as in sidebarCollapse.test.tsx: Node 20 and 26 disagree about whose
 *  window.localStorage is reached. */
function stubStorage(seed: Record<string, string> = {}) {
  const store = new Map(Object.entries(seed));
  Object.defineProperty(window, "localStorage", {
    configurable: true,
    writable: true,
    value: {
      getItem: (k: string) => store.get(k) ?? null,
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
      key: (i: number) => [...store.keys()][i] ?? null,
      get length() { return store.size; },
    },
  });
  return store;
}

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
  let store: Map<string, string>;
  beforeEach(() => {
    store = stubStorage();
  });

  it("starts from the initial value and keeps nothing until it changes", () => {
    const { result } = renderHook(() => usePersistedState("retina.map.x", 1));
    expect(result.current[0]).toBe(1);
    expect(store.has("retina.map.x")).toBe(false);
    act(() => result.current[1](2));
    expect(store.get("retina.map.x")).toBe("2");
  });

  it("reads what it stored", () => {
    store.set("retina.map.x", JSON.stringify({ a: 1 }));
    const { result } = renderHook(() => usePersistedState("retina.map.x", {}));
    expect(result.current[0]).toEqual({ a: 1 });
  });

  it("falls back to the legacy key and copies its value across", () => {
    store.set("tf.x", "false");
    const { result } = renderHook(() => usePersistedState("retina.map.x", true, "tf.x"));
    expect(result.current[0]).toBe(false);
    expect(store.get("retina.map.x")).toBe("false");
  });

  it("prefers the current key over the legacy one", () => {
    store.set("retina.map.x", "true");
    store.set("tf.x", "false");
    const { result } = renderHook(() => usePersistedState("retina.map.x", false, "tf.x"));
    expect(result.current[0]).toBe(true);
  });

  it("uses the initial value for a stored value it cannot parse", () => {
    store.set("retina.map.x", "{not json");
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
    const store = stubStorage();
    writeStored("retina.theme", "dark");
    expect(store.get("retina.theme")).toBe("dark");
    expect(readStored("retina.theme")).toBe("dark");
  });

  it("read nothing and keep nothing when storage refuses, without throwing", () => {
    stubBrokenStorage();
    expect(readStored("retina.theme")).toBeNull();
    expect(() => writeStored("retina.theme", "dark")).not.toThrow();
  });
});
