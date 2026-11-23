import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ShortcutHelp from "./ShortcutHelp";

describe("ShortcutHelp", () => {
  it("lists G as the archived ADS-B traffic in archive mode, and as ground truth live", () => {
    const { rerender } = render(<ShortcutHelp visible onClose={() => {}} truthAvailable archive />);
    expect(screen.getByText("G").closest("tr")).toHaveTextContent("Toggle archived ADS-B traffic");
    rerender(<ShortcutHelp visible onClose={() => {}} truthAvailable />);
    expect(screen.getByText("G").closest("tr")).toHaveTextContent("Toggle ground-truth overlay");
    rerender(<ShortcutHelp visible onClose={() => {}} />);
    expect(screen.queryByText("G")).toBeNull();
  });

  it("says Space also drives the replay in archive mode", () => {
    render(<ShortcutHelp visible onClose={() => {}} />);
    const row = screen.getByText("Space").closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("Pause / resume (the replay in archive mode)");
  });
});
