import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { DateRangeControls } from "../../pages/user/dataExplorer/DateRangeControls";
import { defaultFilters } from "../../pages/user/dataExplorer/urlState";

const TODAY = "2026-09-17";

function setup(over = {}) {
  const onChange = vi.fn();
  const filters = { ...defaultFilters(TODAY), ...over };
  render(<DateRangeControls filters={filters} today={TODAY} onChange={onChange} />);
  return { onChange, filters };
}

describe("DateRangeControls", () => {
  it("shows the current range in its inputs", () => {
    setup();
    expect(screen.getByLabelText("From")).toHaveValue("2026-09-15");
    expect(screen.getByLabelText("To")).toHaveValue(TODAY);
  });

  it("a quick range ends today and spans the labelled number of days", () => {
    const { onChange } = setup();
    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ from: "2026-09-11", to: TODAY }),
    );
  });

  it("the shortest quick range is today alone", () => {
    const { onChange } = setup();
    fireEvent.click(screen.getByRole("button", { name: "24h" }));
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({ from: TODAY, to: TODAY }),
    );
  });

  it("marks the quick range that matches the current filters", () => {
    setup({ from: TODAY, to: TODAY });
    expect(screen.getByRole("button", { name: "24h" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "7d" })).toHaveAttribute("aria-pressed", "false");
  });

  // The date and time inputs are controlled and their value prop does not move
  // in these tests, so a per-keystroke `type()` would assert against a partial
  // value. `change` is the one event a date picker emits anyway.
  it("passes a chosen date through", () => {
    const { onChange } = setup();
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-09-01" } });
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ from: "2026-09-01" }));
  });

  it("offers a time-of-day window and reports it", () => {
    const { onChange } = setup();
    fireEvent.change(screen.getByLabelText("From time"), { target: { value: "06:00" } });
    expect(onChange).toHaveBeenLastCalledWith(expect.objectContaining({ todFrom: "06:00" }));
  });

  it("offers a minimum size and reports it in bytes", () => {
    const { onChange } = setup();
    fireEvent.change(screen.getByLabelText("Min size"), { target: { value: "1048576" } });
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ minSize: 1048576 }));
  });

  // The standalone explorer's steps were 100, 300 and 500 KB, so its links
  // carry sizes these steps lack.
  it("shows a size from a link that matches none of its steps", () => {
    setup({ minSize: 102400 });
    expect(screen.getByLabelText("Min size")).toHaveValue("102400");
    expect(screen.getByRole("option", { selected: true })).toHaveTextContent("≥ 100.0 KB");
    const sizes = screen.getAllByRole("option").map((o) => Number((o as HTMLOptionElement).value));
    expect(sizes).toEqual([...sizes].sort((a, b) => a - b));
  });
});
