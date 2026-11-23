import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import ShortcutHelp from "./ShortcutHelp";

describe("ShortcutHelp", () => {
  it("says Space also drives the replay in archive mode", () => {
    render(<ShortcutHelp visible onClose={() => {}} />);
    const row = screen.getByText("Space").closest("tr") as HTMLElement;
    expect(row).toHaveTextContent("Pause / resume (the replay in archive mode)");
  });
});
