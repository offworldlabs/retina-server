import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useStoreSession, type StoreSession } from "./useStoreSession";

interface Snap {
  n: number;
}

class FakeStore implements StoreSession<Snap> {
  snapshot: Snap = { n: 0 };
  readonly watchers = new Set<() => void>();
  disposed = 0;

  subscribe = (onChange: () => void) => {
    this.watchers.add(onChange);
    return () => {
      this.watchers.delete(onChange);
    };
  };

  getSnapshot = () => this.snapshot;

  dispose = () => {
    this.disposed++;
  };

  set(n: number) {
    this.snapshot = { n };
    for (const onChange of this.watchers) onChange();
  }
}

const IDLE: Snap = { n: -1 };

function setup(active: boolean) {
  const made: FakeStore[] = [];
  const create = vi.fn(() => {
    const store = new FakeStore();
    made.push(store);
    return store;
  });
  const hook = renderHook(({ on }: { on: boolean }) => useStoreSession(on, create, IDLE), { initialProps: { on: active } });
  return { ...hook, create, made, rerender: (on: boolean) => hook.rerender({ on }) };
}

afterEach(cleanup);

describe("useStoreSession", () => {
  it("makes nothing while inactive, and hands back the idle snapshot", () => {
    const { result, create, rerender } = setup(false);
    rerender(false);
    expect(create).not.toHaveBeenCalled();
    expect(result.current[0]).toBeNull();
    expect(result.current[1]).toBe(IDLE);
  });

  it("makes the session on the first render when it starts active, and keeps it across renders", () => {
    const { result, create, made, rerender } = setup(true);
    expect(create).toHaveBeenCalledTimes(1);
    expect(result.current[0]).toBe(made[0]);
    expect(result.current[1]).toBe(made[0].snapshot);
    rerender(true);
    rerender(true);
    expect(create).toHaveBeenCalledTimes(1);
    expect(result.current[0]).toBe(made[0]);
  });

  it("makes one session in the render that turns it active, and follows its snapshot", () => {
    const { result, create, made, rerender } = setup(false);
    rerender(true);
    expect(create).toHaveBeenCalledTimes(1);
    expect(result.current[0]).toBe(made[0]);
    expect(made[0].watchers.size).toBe(1);
    act(() => made[0].set(3));
    expect(result.current[1]).toEqual({ n: 3 });
    expect(result.current[1]).toBe(made[0].snapshot);
  });

  it("disposes the session when it turns inactive, stops watching it, and makes a new one on the way back", () => {
    const { result, create, made, rerender } = setup(true);
    const first = made[0];
    rerender(false);
    expect(first.disposed).toBe(1);
    expect(first.watchers.size).toBe(0);
    expect(result.current[0]).toBeNull();
    expect(result.current[1]).toBe(IDLE);
    // A late change of the old session reaches nothing.
    act(() => first.set(7));
    expect(result.current[1]).toBe(IDLE);

    rerender(true);
    expect(create).toHaveBeenCalledTimes(2);
    expect(result.current[0]).toBe(made[1]);
    expect(made[1]).not.toBe(first);
    expect(first.disposed).toBe(1);
    expect(made[1].disposed).toBe(0);
  });

  it("disposes the session on unmount", () => {
    const { made, unmount } = setup(true);
    unmount();
    expect(made[0].disposed).toBe(1);
    expect(made[0].watchers.size).toBe(0);
  });
});
