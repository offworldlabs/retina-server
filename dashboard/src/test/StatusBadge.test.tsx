import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { StatusBadge } from "../components/StatusBadge";

describe("StatusBadge", () => {
  it("marks a connected node online", () => {
    render(<StatusBadge status="active" />);
    expect(screen.getByText("Online")).toHaveClass("badge", "online");
  });

  it.each(["disconnected", null, undefined])("marks a node with status %s offline", (status) => {
    render(<StatusBadge status={status} />);
    expect(screen.getByText("Offline")).toHaveClass("badge", "offline");
  });

  it("says a node was never connected rather than that it dropped", () => {
    render(<StatusBadge status="never_connected" />);
    expect(screen.getByText("Never connected")).toHaveClass("badge", "offline");
  });

  it.each([
    [true, "Online", "online"],
    [false, "Offline", "offline"],
  ])("takes the server's verdict when given one (%s)", (online, label, tone) => {
    render(<StatusBadge online={online} />);
    expect(screen.getByText(label)).toHaveClass("badge", tone);
  });
});
