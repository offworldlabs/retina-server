import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import tableCss from "../components/dataTable.css?raw";
import { clampPage, Pager } from "../components/Pager";
import { stubViewport } from "./matchMedia";
import { declarations } from "./paletteTokens";

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

  describe("by width", () => {
    afterEach(() => vi.unstubAllGlobals());

    it("draws the desktop pager as before", () => {
      stubViewport(1440);
      const { container } = render(<Pager page={1} totalPages={4} onPage={() => {}} note="37 nodes" />);
      expect(container.innerHTML).toBe(
        '<div class="pager"><button class="btn btn-secondary btn-sm">← Prev</button>' +
          '<span class="pager-note">Page 2 of 4 (37 nodes)</span>' +
          '<button class="btn btn-secondary btn-sm">Next →</button></div>',
      );
    });

    it("keeps a phone's pager to one row, Prev and Next at its edges, the note free to wrap under the page", () => {
      stubViewport(390);
      render(<Pager page={1} totalPages={4} onPage={() => {}} note="37 nodes" />);
      expect(screen.getByText("Page 2 of 4")).toHaveClass("pager-page");
      expect(screen.getByText("(37 nodes)")).toHaveClass("pager-count");

      const css = tableCss.replace(/\/\*[\s\S]*?\*\//g, "");
      const phone = css.slice(css.indexOf("@media (max-width: 640px) {"));
      expect(declarations(phone, "  .pager {")).toEqual({ "justify-content": "space-between", gap: "12px" });
      expect(declarations(phone, ".pager > .btn {")).toEqual({ flex: "none" });
      expect(declarations(phone, ".pager-note {")).toMatchObject({ flex: "1 1 0", "text-align": "center" });
      expect(declarations(phone, ".pager-page {")).toEqual({ "white-space": "nowrap" });
      expect(declarations(phone, ".pager-count {")).toEqual({ display: "inline-block" });
    });
  });
});
