import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useHashWriter } from "./useUrlHashState";

beforeEach(() => vi.useFakeTimers());
afterEach(() => {
  vi.useRealTimers();
  window.history.replaceState(null, "", "/");
});

it("keeps the router's history state on the entry it rewrites", () => {
  const routerState = { usr: null, key: "abc123", idx: 3 };
  window.history.replaceState(routerState, "", "/map");
  const { result } = renderHook(() => useHashWriter());
  result.current({ hex: "abc123" });
  vi.advanceTimersByTime(250);
  expect(window.location.hash).toBe("#hex=abc123");
  expect(window.history.state).toEqual(routerState);
});
