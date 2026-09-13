import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RetnodeLink, retnodeUrl } from "../components/RetnodeLink";

describe("retnodeUrl", () => {
  it("builds the node's own subdomain", () => {
    expect(retnodeUrl("ret0a1b2c3d")).toBe("https://ret0a1b2c3d.retnode.com");
  });

  it.each(["", "not a label", "node.with.dots", "-leading", "trailing-", "a/b"])(
    "returns null for %j, which is not a DNS label",
    (id) => {
      expect(retnodeUrl(id)).toBeNull();
    },
  );
});

describe("RetnodeLink", () => {
  it("makes the node id itself the link, opened in a new tab", () => {
    render(<RetnodeLink nodeId="ret0a1b2c3d" />);
    const link = screen.getByRole("link", { name: /ret0a1b2c3d/ });
    expect(link).toHaveAttribute("href", "https://ret0a1b2c3d.retnode.com");
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("links a node's name to the site its id names", () => {
    render(<RetnodeLink nodeId="ret0a1b2c3d">Rooftop north</RetnodeLink>);
    expect(screen.getByRole("link", { name: /rooftop north/i })).toHaveAttribute(
      "href",
      "https://ret0a1b2c3d.retnode.com",
    );
  });

  it("does not trigger the enclosing card's navigation", () => {
    const onCardClick = vi.fn();
    render(
      <div onClick={onCardClick}>
        <RetnodeLink nodeId="ret0a1b2c3d" />
      </div>,
    );
    fireEvent.click(screen.getByRole("link"));
    expect(onCardClick).not.toHaveBeenCalled();
  });

  it("links when the payload does not say whether the node is synthetic", () => {
    render(<RetnodeLink nodeId="ret0a1b2c3d" />);
    expect(screen.getByRole("link")).toBeInTheDocument();
  });

  it.each([
    ["a synthetic node, which has no box behind it", { nodeId: "synth-0001", synthetic: true }],
    ["an id that cannot be a hostname", { nodeId: "bad id" }],
  ])("still shows the id as plain text for %s", (_case, props) => {
    render(<RetnodeLink {...props} />);
    expect(screen.getByText(props.nodeId)).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });
});
