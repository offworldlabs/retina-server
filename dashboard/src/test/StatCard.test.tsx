import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatCard } from "../components/StatCard";

describe("StatCard", () => {
  it("renders the label over the value in the stylesheet's markup", () => {
    const { container } = render(<StatCard label="Nodes Online" value="3 / 4" />);
    const card = container.firstElementChild!;
    expect(card).toHaveClass("stat-card");
    expect(card.querySelector(".stat-label")).toHaveTextContent("Nodes Online");
    expect(card.querySelector(".stat-value")).toHaveTextContent("3 / 4");
    expect(card.querySelector(".stat-sub")).toBeNull();
  });

  it.each(["accent", "success", "warning", "error"] as const)("colours the value with the %s tone", (tone) => {
    const { container } = render(<StatCard label="Errors" value={2} tone={tone} />);
    expect(container.firstElementChild).toHaveClass("stat-card", tone);
  });

  it("takes no tone class when the tone is undefined", () => {
    const { container } = render(<StatCard label="Frames" value={0} tone={undefined} />);
    expect(container.firstElementChild!.className).toBe("stat-card");
  });

  it("puts the sub line under the value", () => {
    const { container } = render(<StatCard label="Active" value={5} sub="currently flagged" />);
    expect(container.querySelector(".stat-sub")).toHaveTextContent("currently flagged");
  });

  it("sets the unit beside the value, smaller", () => {
    render(<StatCard label="Mean" value="1.23" unit="km" />);
    const value = screen.getByText("1.23", { exact: false }).closest(".stat-value")!;
    expect(value.querySelector(".stat-unit")).toHaveTextContent("km");
  });

  it("accepts markup for the value", () => {
    render(
      <StatCard
        label="Active Nodes"
        value={
          <>
            7 <span data-testid="peak">/ peak 9</span>
          </>
        }
      />,
    );
    expect(screen.getByTestId("peak")).toBeInTheDocument();
  });

  it("can shrink the value for a long reading", () => {
    const { container } = render(<StatCard label="Most Common Type" value="instant direction change" size="small" />);
    expect(container.querySelector(".stat-value")).toHaveClass("small");
  });
});
