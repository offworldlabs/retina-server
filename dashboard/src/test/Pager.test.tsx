import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { clampPage, Pager } from "../components/Pager";

describe("clampPage", () => {
  it("leaves a page that still exists alone", () => {
    expect(clampPage(0, 3)).toBe(0);
    expect(clampPage(2, 3)).toBe(2);
  });

  it("snaps a page past the end back to the last one", () => {
    expect(clampPage(5, 3)).toBe(2);
    expect(clampPage(1, 1)).toBe(0);
  });

  it("stays on the first page when there are none", () => {
    expect(clampPage(4, 0)).toBe(0);
  });
});

describe("Pager", () => {
  it("renders nothing when everything fits on one page", () => {
    const { container } = render(<Pager page={0} totalPages={1} onPage={() => {}} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("names the page and moves one page either way", () => {
    const onPage = vi.fn();
    render(<Pager page={1} totalPages={4} onPage={onPage} />);
    expect(screen.getByText("Page 2 of 4")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /prev/i }));
    expect(onPage).toHaveBeenLastCalledWith(0);
    fireEvent.click(screen.getByRole("button", { name: /next/i }));
    expect(onPage).toHaveBeenLastCalledWith(2);
  });

  it("cannot go before the first page or past the last", () => {
    const { rerender } = render(<Pager page={0} totalPages={3} onPage={() => {}} />);
    expect(screen.getByRole("button", { name: /prev/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /next/i })).toBeEnabled();
    rerender(<Pager page={2} totalPages={3} onPage={() => {}} />);
    expect(screen.getByRole("button", { name: /prev/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /next/i })).toBeDisabled();
  });

  it("appends a note in brackets", () => {
    render(<Pager page={0} totalPages={2} onPage={() => {}} note="37 nodes" />);
    expect(screen.getByText("Page 1 of 2 (37 nodes)")).toBeInTheDocument();
  });
});
