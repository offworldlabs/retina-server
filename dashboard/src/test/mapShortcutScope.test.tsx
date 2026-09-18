import { describe, it, expect, vi, afterEach } from "vitest";
import { renderHook } from "@testing-library/react";
import { useKeyboardShortcuts } from "../pages/map/useKeyboardShortcuts";

function press(key: string) {
  window.dispatchEvent(new KeyboardEvent("keydown", { key, bubbles: true }));
}

function focusIn(className: string) {
  const host = document.createElement("div");
  host.className = className;
  const button = document.createElement("button");
  host.appendChild(button);
  document.body.appendChild(host);
  button.focus();
  return () => host.remove();
}

describe("map shortcuts", () => {
  afterEach(() => document.body.replaceChildren());

  it("fire when nothing is focused", () => {
    const handler = vi.fn();
    renderHook(() => useKeyboardShortcuts({ t: handler }));
    press("t");
    expect(handler).toHaveBeenCalledOnce();
  });

  it("fire when focus is inside the map surface", () => {
    const handler = vi.fn();
    focusIn("app map-surface");
    renderHook(() => useKeyboardShortcuts({ t: handler }));
    press("t");
    expect(handler).toHaveBeenCalledOnce();
  });

  it("stay quiet when focus is on console chrome", () => {
    const handler = vi.fn();
    focusIn("header");
    renderHook(() => useKeyboardShortcuts({ t: handler }));
    press("t");
    expect(handler).not.toHaveBeenCalled();
  });
});
