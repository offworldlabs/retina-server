import type { ComponentProps } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import Toolbar from "./Toolbar";
import { AT_LIVE_EDGE_TITLE } from "./replay/useArchiveReplay";

type ToolbarProps = ComponentProps<typeof Toolbar>;

function setup(over: Partial<ToolbarProps> = {}) {
  const view = render(
    <Toolbar
      {...({
        connected: true,
        paused: false,
        aircraftCount: 0,
        anomalyCount: 0,
        filters: { minFl: "", maxFl: "", minGs: "", type: "all" },
        onTogglePause: vi.fn(),
        onShare: vi.fn(),
        ...over,
      } as ToolbarProps)}
    />,
  );
  const pill = view.container.querySelector(".connection-badge") as HTMLElement;
  return { view, pill };
}

const openMenu = () => fireEvent.click(screen.getByTitle("More actions"));
const menuItems = () => screen.getAllByRole("menuitem").map((el) => el.textContent);

describe("the toolbar's feed pill", () => {
  it.each([
    [{ connected: true, paused: false }, "LIVE", "badge connection-badge online"],
    [{ connected: true, paused: true }, "PAUSED", "badge connection-badge online"],
    [{ connected: false, paused: false }, "POLL", "badge connection-badge warning"],
    [{ connected: false, paused: true }, "POLL", "badge connection-badge warning"],
  ])("reads as before outside the archive: %o", (state, text, className) => {
    const { pill } = setup(state);
    expect(pill).toHaveTextContent(text);
    expect(pill.className).toBe(className);
  });

  it.each([
    { connected: true, paused: false },
    { connected: true, paused: true },
    { connected: false, paused: false },
  ])("reads ARCHIVE in the archive, whatever the socket is doing: %o", (state) => {
    const { pill } = setup({ ...state, archive: true });
    expect(pill).toHaveTextContent(/^ARCHIVE$/);
    expect(pill.className).toBe("badge connection-badge archive");
  });
});

describe("the toolbar's pause button", () => {
  it("keeps its live labels outside the archive", () => {
    const { view } = setup({ paused: false });
    const btn = screen.getByRole("button", { name: "⏸ Pause" });
    expect(btn).not.toHaveAttribute("aria-label");
    expect(btn).toHaveAttribute("aria-pressed", "false");
    view.rerender(
      <Toolbar {...({ connected: true, paused: true, aircraftCount: 0, filters: {} } as ToolbarProps)} />,
    );
    expect(screen.getByRole("button", { name: "▶ Resume" })).toHaveAttribute("aria-pressed", "true");
  });

  it("plays and pauses the replay in the archive", () => {
    const onTogglePause = vi.fn();
    const { view } = setup({ archive: true, paused: true, onTogglePause });
    const play = screen.getByRole("button", { name: "Play replay" });
    expect(play).toHaveTextContent("▶ Play");
    fireEvent.click(play);
    expect(onTogglePause).toHaveBeenCalledTimes(1);
    view.rerender(
      <Toolbar
        {...({ archive: true, connected: true, paused: false, aircraftCount: 0, filters: {} } as ToolbarProps)}
      />,
    );
    expect(screen.getByRole("button", { name: "Pause replay" })).toHaveTextContent("⏸ Pause");
  });
});

describe("the toolbar's Play at the live edge (L1)", () => {
  it("is disabled, with a title that points to Jump to now", () => {
    const onTogglePause = vi.fn();
    setup({ archive: true, paused: true, atLiveEdge: true, onTogglePause });
    const play = screen.getByRole("button", { name: "Play replay" });
    expect(play).toHaveTextContent("▶ Play");
    expect(play).toBeDisabled();
    expect(play).toHaveAttribute("title", AT_LIVE_EDGE_TITLE);
    fireEvent.click(play);
    expect(onTogglePause).not.toHaveBeenCalled();
  });

  it("keeps Pause while playing into the edge, and Play off it", () => {
    const { view } = setup({ archive: true, paused: false, atLiveEdge: true });
    const pause = screen.getByRole("button", { name: "Pause replay" });
    expect(pause).toBeEnabled();
    expect(pause).not.toHaveAttribute("title");
    view.rerender(
      <Toolbar
        {...({ archive: true, atLiveEdge: false, connected: true, paused: true, aircraftCount: 0, filters: {} } as ToolbarProps)}
      />,
    );
    const play = screen.getByRole("button", { name: "Play replay" });
    expect(play).toBeEnabled();
    expect(play).not.toHaveAttribute("title");
  });

  it("leaves the live pause alone", () => {
    setup({ paused: true, atLiveEdge: true });
    const resume = screen.getByRole("button", { name: "▶ Resume" });
    expect(resume).toBeEnabled();
    expect(resume).not.toHaveAttribute("title");
  });
});

describe("the toolbar's Open archive entry", () => {
  it("sits right after Copy link when the map can open the archive", () => {
    setup({ onOpenArchive: vi.fn() });
    openMenu();
    const items = menuItems();
    const share = items.indexOf("Copy link to this view");
    expect(items[share + 1]).toBe("Open archive 1 h ago");
    expect(screen.getByRole("menuitem", { name: /Open archive/ }).querySelector(".menu-hint")).toHaveTextContent(
      "1 h ago",
    );
  });

  it("opens the archive and closes the menu", () => {
    const onOpenArchive = vi.fn();
    setup({ onOpenArchive });
    openMenu();
    fireEvent.click(screen.getByRole("menuitem", { name: /Open archive/ }));
    expect(onOpenArchive).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("menu")).toBeNull();
  });

  it("is absent when nothing can open the archive", () => {
    setup();
    openMenu();
    expect(screen.queryByRole("menuitem", { name: /Open archive/ })).toBeNull();
    expect(menuItems()).toHaveLength(6);
  });

  it("is absent while already in the archive", () => {
    setup({ archive: true, onOpenArchive: vi.fn() });
    openMenu();
    expect(screen.queryByRole("menuitem", { name: /Open archive/ })).toBeNull();
  });
});

describe("the toolbar's ground-truth button", () => {
  it("reads ADS-B traffic in the archive", () => {
    setup({ archive: true, truthAvailable: true });
    const g = screen.getByRole("button", { name: "ADS-B traffic" });
    expect(g).toHaveAttribute("title", "Overlay the archived ADS-B traffic");
    expect(screen.queryByRole("button", { name: "Debug Truth" })).toBeNull();
  });

  it("keeps Debug Truth live", () => {
    setup({ truthAvailable: true });
    const g = screen.getByRole("button", { name: "Debug Truth" });
    expect(g).toHaveAttribute("title", "Overlay the ADS-B positions the solver is being measured against");
    expect(screen.queryByRole("button", { name: "ADS-B traffic" })).toBeNull();
  });
});
