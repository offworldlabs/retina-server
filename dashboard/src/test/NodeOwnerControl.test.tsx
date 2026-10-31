import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import NodeOwnerControl from "../pages/map/NodeOwnerControl";
import { CLAIM_PATH } from "../components/ClaimPath";

const owner = { name: "Ada Owner", email: "ada@example.com" };

function setup(props: Partial<React.ComponentProps<typeof NodeOwnerControl>> = {}) {
  const onToggle = vi.fn();
  const view = render(
    <NodeOwnerControl user={owner} ownedCount={2} ownerOnly={false} onToggle={onToggle} loading={false} {...props} />,
  );
  return { ...view, onToggle };
}

describe("NodeOwnerControl", () => {
  it("renders nothing while auth is loading", () => {
    const { container } = setup({ loading: true });
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing for a signed-out visitor", () => {
    const { container } = setup({ user: null });
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the user's name", () => {
    setup();
    expect(screen.getByText("Ada Owner")).toBeInTheDocument();
  });

  it("falls back to the email when the user has no name", () => {
    setup({ user: { name: "", email: "ada@example.com" } });
    expect(screen.getByText("ada@example.com")).toBeInTheDocument();
  });

  it("reflects ownerOnly in the checkbox", () => {
    const off = setup({ ownerOnly: false });
    expect(screen.getByRole("checkbox")).not.toBeChecked();
    off.unmount();
    setup({ ownerOnly: true });
    expect(screen.getByRole("checkbox")).toBeChecked();
  });

  it("calls onToggle(true) when the box is ticked", () => {
    const { onToggle } = setup({ ownerOnly: false });
    fireEvent.click(screen.getByRole("checkbox"));
    expect(onToggle).toHaveBeenCalledWith(true);
  });

  it("calls onToggle(false) when the box is cleared", () => {
    const { onToggle } = setup({ ownerOnly: true });
    fireEvent.click(screen.getByRole("checkbox"));
    expect(onToggle).toHaveBeenCalledWith(false);
  });

  it("names the claim path when the owner view is empty", () => {
    const { container } = setup({ ownerOnly: true, ownedCount: 0 });
    const warning = container.querySelector(".owner-warning");
    expect(warning).toHaveTextContent("No nodes on your account yet.");
    expect(warning).toHaveTextContent(CLAIM_PATH);
    expect(container).not.toHaveTextContent("Claim a node from My Nodes");
  });

  it("shows no warning when the account owns nodes", () => {
    const { container } = setup({ ownerOnly: true, ownedCount: 3 });
    expect(container.querySelector(".owner-warning")).toBeNull();
  });

  it("shows no warning while the owner view is off", () => {
    const { container } = setup({ ownerOnly: false, ownedCount: 0 });
    expect(container.querySelector(".owner-warning")).toBeNull();
  });
});
