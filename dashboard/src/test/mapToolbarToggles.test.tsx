import type { ComponentProps } from "react";
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import Toolbar from "../pages/map/Toolbar";

// Only the props these tests read; the handlers are never called.
type ToolbarProps = ComponentProps<typeof Toolbar>;

/* The toolbar's switches are ui.css's pressed .btn: their state is the ARIA
   attribute, which is also what draws them pressed. */
describe("the map toolbar's toggles", () => {
  const renderToolbar = (props: Partial<ToolbarProps> = {}) =>
    render(
      <Toolbar
        {...({
          connected: true,
          aircraftCount: 0,
          anomalyCount: 0,
          filters: { minFl: "", maxFl: "", minGs: "", type: "all" },
          showArcs: true,
          showTrails: false,
          showAnomaliesOnly: true,
          ...props,
        } as ToolbarProps)}
      />,
    );

  it("report their state as aria-pressed", () => {
    renderToolbar();
    expect(screen.getByRole("button", { name: "Arcs" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Trails" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: /Anomalies/ })).toHaveAttribute("aria-pressed", "true");
  });

  it("are the shared outline button, with no pressed class of their own", () => {
    const { container } = renderToolbar();
    const toggles = [...container.querySelectorAll(".toggle-btn")];
    expect(toggles.length).toBeGreaterThan(10);
    for (const el of toggles) {
      expect(el).toHaveClass("btn", "btn-secondary");
      expect(el).not.toHaveClass("active");
    }
  });

  it("leave the two that open something to aria-expanded", () => {
    renderToolbar({ showFilters: false });
    const filters = screen.getByRole("button", { name: /^Filters/ });
    expect(filters).toHaveAttribute("aria-expanded", "false");
    expect(filters).not.toHaveAttribute("aria-pressed");
  });

  it("open Filters as a card around its header and body", () => {
    const { container } = renderToolbar({ showFilters: true });
    const popover = container.querySelector(".filters-popover");
    expect(popover).toHaveClass("card");
    expect(popover?.querySelector(":scope > .card-header")).not.toBeNull();
    expect(popover?.querySelector(":scope > .card-body")).not.toBeNull();
  });
});
