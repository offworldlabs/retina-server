import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { UsageBar, usageTone } from "../components/UsageBar";

describe("usageTone", () => {
  it.each([
    [0, "success"],
    [70, "success"],
    [70.1, "warning"],
    [90, "warning"],
    [90.1, "error"],
    [100, "error"],
  ] as const)("reads %s%% as %s", (pct, tone) => {
    expect(usageTone(pct)).toBe(tone);
  });
});

describe("UsageBar", () => {
  it("sets the label beside its reading, over a bar filled to the percentage", () => {
    const { container } = render(<UsageBar label="CPU" value="42.0%" pct={42} />);
    const bar = screen.getByRole("progressbar", { name: "CPU" });
    expect(bar).toHaveAttribute("aria-valuenow", "42");
    expect(container.querySelector(".usage-bar-value")).toHaveTextContent("42.0%");
    const fill = container.querySelector(".usage-bar-fill") as HTMLElement;
    expect(fill).toHaveClass("success");
    expect(fill.style.width).toBe("42%");
  });

  it("colours the fill by the shared thresholds", () => {
    const { container } = render(<UsageBar label="Disk" value="95%" pct={95} />);
    expect(container.querySelector(".usage-bar-fill")).toHaveClass("error");
  });

  it("caps an overfull reading at the end of the track", () => {
    const { container } = render(<UsageBar label="Queue" value="250 / 200" pct={125} />);
    expect((container.querySelector(".usage-bar-fill") as HTMLElement).style.width).toBe("100%");
    expect(screen.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "100");
  });

  it("leaves the track empty for a missing reading rather than drawing zero", () => {
    const { container } = render(<UsageBar label="Memory" value="—" pct={null} />);
    expect(container.querySelector(".usage-bar-fill")).toBeNull();
    expect(screen.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
  });

  it("puts the note under the bar", () => {
    const { container } = render(<UsageBar label="Used" value="Free" pct={10} note="10.0% used" />);
    expect(container.querySelector(".usage-bar-note")).toHaveTextContent("10.0% used");
  });
});
