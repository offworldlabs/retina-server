import { createRef, useState, type ReactNode, type RefObject } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Sheet } from "../components/Sheet";
import sheetCss from "../components/sheet.css?raw";

interface HarnessProps {
  title?: string;
  onClose?: () => void;
  initialFocus?: RefObject<HTMLButtonElement>;
  children?: ReactNode;
}

/** An opener and the sheet it opens, the way a page holds one. */
function Harness({ title = "Map layers", onClose, initialFocus, children }: HarnessProps) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open {title}
      </button>
      <Sheet
        open={open}
        title={title}
        description="What the map draws."
        onClose={() => {
          onClose?.();
          setOpen(false);
        }}
        initialFocus={initialFocus}
        actions={
          <button ref={initialFocus} type="button" className="btn btn-primary">
            Done
          </button>
        }
      >
        <label>
          <input type="checkbox" /> Trails
        </label>
        {children}
      </Sheet>
    </>
  );
}

/** Opens the harness from its button, focused first as a tap would leave it. */
function open(title = "Map layers") {
  const opener = screen.getByRole("button", { name: `Open ${title}` });
  opener.focus();
  fireEvent.click(opener);
  return { opener, sheet: screen.getByRole("dialog", { name: title }) };
}

/** The console's own scroller, as DashboardLayout renders it. */
function pageScroller() {
  const content = document.createElement("div");
  content.className = "content";
  document.body.append(content);
  return content;
}

afterEach(() => {
  delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).showModal;
  delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).close;
  document.querySelectorAll(".content").forEach((content) => content.remove());
});

describe("Sheet", () => {
  it("opens modally, named by its title and described, with focus on its first control", () => {
    const showModal = vi.fn(function (this: HTMLDialogElement) {
      this.setAttribute("open", "");
    });
    const close = vi.fn(function (this: HTMLDialogElement) {
      this.removeAttribute("open");
      this.dispatchEvent(new Event("close"));
    });
    Object.assign(HTMLDialogElement.prototype, { showModal, close });
    render(<Harness />);
    const { sheet } = open();

    expect(showModal).toHaveBeenCalledTimes(1);
    expect(showModal.mock.contexts[0]).toBe(sheet);
    expect(sheet).toHaveAccessibleDescription("What the map draws.");
    expect(screen.getByRole("checkbox", { name: "Trails" })).toHaveFocus();
  });

  it("focuses what the caller names instead", () => {
    const done = createRef<HTMLButtonElement>();
    render(<Harness initialFocus={done} />);
    open();
    expect(screen.getByRole("button", { name: "Done" })).toHaveFocus();
  });

  it("focuses the close button, never an action, when its content holds no control", () => {
    render(
      <Sheet
        open
        title="Remove tower"
        onClose={() => {}}
        actions={<button type="button">Delete</button>}
      >
        <p>This cannot be undone.</p>
      </Sheet>,
    );

    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
    expect(screen.getByRole("button", { name: "Delete" })).not.toHaveFocus();
  });

  it("keeps Tab inside, wrapping from the last control to the first and back", () => {
    render(<Harness />);
    const { sheet } = open();
    const close = screen.getByRole("button", { name: "Close" });
    const done = screen.getByRole("button", { name: "Done" });

    done.focus();
    expect(fireEvent.keyDown(sheet, { key: "Tab" })).toBe(false);
    expect(close).toHaveFocus();

    expect(fireEvent.keyDown(sheet, { key: "Tab", shiftKey: true })).toBe(false);
    expect(done).toHaveFocus();
  });

  it.each([
    ["Escape", (sheet: HTMLElement) => fireEvent.keyDown(sheet, { key: "Escape" })],
    ["its close button", () => fireEvent.click(screen.getByRole("button", { name: "Close" }))],
    [
      "a tap on the backdrop",
      (sheet: HTMLElement) => {
        fireEvent.pointerDown(sheet);
        fireEvent.pointerUp(sheet);
        fireEvent.click(sheet);
      },
    ],
  ])("closes on %s and gives focus back to what opened it", (_, dismiss) => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    const { opener, sheet } = open();

    dismiss(sheet);

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });

  it("stays open when a press that began in the panel ends on the backdrop", () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    const { sheet } = open();

    fireEvent.pointerDown(screen.getByRole("checkbox", { name: "Trails" }));
    fireEvent.pointerUp(sheet);
    fireEvent.click(sheet);

    expect(onClose).not.toHaveBeenCalled();
  });

  // The browser sends that press's click to the dialog, the box that holds
  // both ends, as it does a tap's.
  it("stays open when a press that began on the backdrop ends on a control in the panel", () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    const { sheet } = open();

    fireEvent.pointerDown(sheet);
    fireEvent.pointerUp(screen.getByRole("button", { name: "Done" }));
    fireEvent.click(sheet);

    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "Map layers" })).toBeInTheDocument();
  });

  it("stays open on a click a key makes on a control, after a press on the backdrop made none", () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    const { sheet } = open();

    fireEvent.pointerDown(sheet);
    fireEvent.pointerUp(sheet);
    fireEvent.click(screen.getByRole("button", { name: "Done" }));

    expect(onClose).not.toHaveBeenCalled();
  });

  it("holds the page still while open, and puts back the inline overflow it found", () => {
    const content = pageScroller();
    content.style.setProperty("overflow-y", "scroll", "important");
    render(<Harness />);
    const { sheet } = open();

    for (const scroller of [document.documentElement, document.body, content]) {
      expect([scroller.style.overflowX, scroller.style.overflowY]).toEqual(["hidden", "hidden"]);
    }

    fireEvent.keyDown(sheet, { key: "Escape" });

    expect([content.style.overflowX, content.style.overflowY]).toEqual(["", "scroll"]);
    expect(content.style.getPropertyPriority("overflow-y")).toBe("important");
    for (const scroller of [document.documentElement, document.body]) {
      expect([scroller.style.overflowX, scroller.style.overflowY]).toEqual(["", ""]);
    }
  });

  it("keeps the page held while a second sheet is open, and closes only the one asked", () => {
    const content = pageScroller();
    render(
      <Harness>
        <Harness title="Filters" />
      </Harness>,
    );
    const { sheet: layers } = open();
    const { opener, sheet: filters } = open("Filters");

    // Android's back gesture asks the top one to close.
    act(() => {
      filters.dispatchEvent(new Event("cancel", { cancelable: true }));
    });

    expect(screen.queryByRole("dialog", { name: "Filters" })).toBeNull();
    expect(screen.getByRole("dialog", { name: "Map layers" })).toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(content.style.overflowY).toBe("hidden");

    fireEvent.keyDown(layers, { key: "Escape" });

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(content.style.overflowY).toBe("");
  });

  // Callers render a sheet only below desktop width, and a desktop keeps its
  // own panels: a rule above that width would be drawn by nothing.
  it("styles nothing above tablet width", () => {
    const bare = sheetCss.replace(/\/\*[\s\S]*?\*\//g, "");
    const preludes: string[] = [];
    let at = 0;
    for (let open = bare.indexOf("{"); open >= 0; open = bare.indexOf("{", at)) {
      let close = open + 1;
      for (let depth = 1; depth > 0; close++) depth += bare[close] === "{" ? 1 : bare[close] === "}" ? -1 : 0;
      preludes.push(bare.slice(at, open).trim());
      at = close;
    }
    expect(preludes.length).toBeGreaterThan(0);
    expect(preludes.filter((prelude) => !/^@media \(max-width: (1023\.98|640)px\)/.test(prelude))).toEqual([]);
  });
});
