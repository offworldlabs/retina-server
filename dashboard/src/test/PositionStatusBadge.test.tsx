import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PositionStatusBadge } from "../components/PositionStatusBadge";

describe("PositionStatusBadge", () => {
  it("renders nothing for a positioned node", () => {
    const { container } = render(<PositionStatusBadge status="positioned" />);
    expect(container).toBeEmptyDOMElement();
  });

  it.each([
    ["missing_both", "No position"],
    ["missing_rx", "No receiver position"],
    ["missing_tx", "No illuminator position"],
  ])("labels %s", (status, label) => {
    render(<PositionStatusBadge status={status as never} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });
});
