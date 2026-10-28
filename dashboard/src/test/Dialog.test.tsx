import { createRef, useState, type RefObject } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Dialog } from "../components/Dialog";

interface HarnessProps {
  busy?: boolean;
  field?: boolean;
  /** Save turned off, as a caller does once the answer rules it out. */
  saveDisabled?: boolean;
  onSubmit?: () => void;
  onClose?: () => void;
  initialFocus?: RefObject<HTMLButtonElement>;
}

/** An opener and the dialog it opens, the way a page holds one. */
function Harness({ busy = false, field = true, saveDisabled = false, onSubmit, onClose, initialFocus }: HarnessProps) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        Open
      </button>
      <Dialog
        open={open}
        title="Rename node"
        description="The name shows on your pages only."
        busy={busy}
        onSubmit={onSubmit}
        onClose={() => {
          onClose?.();
          setOpen(false);
        }}
        initialFocus={initialFocus}
        actions={
          <button ref={initialFocus} type="submit" className="btn btn-primary" disabled={saveDisabled}>
            Save
          </button>
        }
      >
        {field && <input aria-label="Name" className="input" />}
      </Dialog>
    </>
  );
}

/** Opens the harness from its button, focused first as a click would leave it. */
function open() {
  const opener = screen.getByRole("button", { name: "Open" });
  opener.focus();
  fireEvent.click(opener);
  return { opener, dialog: screen.getByRole("dialog", { name: "Rename node" }) };
}

afterEach(() => {
  delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).showModal;
  delete (HTMLDialogElement.prototype as Partial<HTMLDialogElement>).close;
});

describe("Dialog", () => {
  it("renders nothing while closed", () => {
    render(<Harness />);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("is named by its title, described by its description, and focuses its first field", () => {
    render(<Harness />);
    const { dialog } = open();

    expect(dialog).toHaveAccessibleDescription("The name shows on your pages only.");
    expect(screen.getByRole("textbox", { name: "Name" })).toHaveFocus();
  });

  it("focuses Cancel, the button that changes nothing, when it has no field", () => {
    render(<Harness field={false} />);
    open();
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
  });

  it("focuses what the caller names instead", () => {
    const save = createRef<HTMLButtonElement>();
    render(<Harness initialFocus={save} />);
    open();
    expect(screen.getByRole("button", { name: "Save" })).toHaveFocus();
  });

  it("closes on Escape and gives focus back to what opened it", () => {
    const onClose = vi.fn();
    render(<Harness onClose={onClose} />);
    const { opener, dialog } = open();

    fireEvent.keyDown(dialog, { key: "Escape" });

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });

  it("closes on Cancel and gives focus back to what opened it", () => {
    render(<Harness />);
    const { opener } = open();

    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(opener).toHaveFocus();
  });

  it("stays open while busy: Escape does nothing and Cancel is disabled", () => {
    const onClose = vi.fn();
    render(<Harness busy onClose={onClose} />);
    const { dialog } = open();

    const escape = fireEvent.keyDown(dialog, { key: "Escape" });

    expect(escape).toBe(false);
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("keeps Tab inside, wrapping from the last stop to the first and back", () => {
    render(<Harness />);
    const { dialog } = open();
    const name = screen.getByRole("textbox", { name: "Name" });
    const cancel = screen.getByRole("button", { name: "Cancel" });

    cancel.focus();
    expect(fireEvent.keyDown(dialog, { key: "Tab" })).toBe(false);
    expect(name).toHaveFocus();

    expect(fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true })).toBe(false);
    expect(cancel).toHaveFocus();

    // Between the ends, Tab is the browser's own.
    name.focus();
    expect(fireEvent.keyDown(dialog, { key: "Tab" })).toBe(true);
  });

  it("brings Tab back in when focus has escaped to the page", () => {
    render(<Harness />);
    const { opener, dialog } = open();

    opener.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });

    expect(screen.getByRole("textbox", { name: "Name" })).toHaveFocus();
  });

  it("skips a disabled Cancel when wrapping", () => {
    render(<Harness busy />);
    const { dialog } = open();

    screen.getByRole("button", { name: "Save" }).focus();
    fireEvent.keyDown(dialog, { key: "Tab" });

    expect(screen.getByRole("textbox", { name: "Name" })).toHaveFocus();
  });

  it("submits on Enter in a field, but not while busy", () => {
    const onSubmit = vi.fn();
    const { rerender } = render(<Harness onSubmit={onSubmit} />);
    open();
    const form = screen.getByRole("textbox", { name: "Name" }).closest("form") as HTMLFormElement;

    expect(fireEvent.submit(form)).toBe(false);
    expect(onSubmit).toHaveBeenCalledTimes(1);

    rerender(<Harness onSubmit={onSubmit} busy />);
    fireEvent.submit(form);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  describe("when a request ends with the dialog still open", () => {
    it("brings focus that the browser dropped back to the first field", () => {
      const { rerender } = render(<Harness />);
      open();
      const save = screen.getByRole("button", { name: "Save" });
      save.focus();

      rerender(<Harness busy />);
      // A browser drops focus from a control disabled under it to the page.
      act(() => save.blur());
      expect(document.body).toHaveFocus();

      rerender(<Harness />);

      expect(screen.getByRole("textbox", { name: "Name" })).toHaveFocus();
    });

    it("brings it back to what the caller names instead", () => {
      const save = createRef<HTMLButtonElement>();
      const { rerender } = render(<Harness initialFocus={save} />);
      open();

      rerender(<Harness initialFocus={save} busy />);
      act(() => save.current?.blur());
      rerender(<Harness initialFocus={save} />);

      expect(screen.getByRole("button", { name: "Save" })).toHaveFocus();
    });

    it("moves it off a control the answer left disabled", () => {
      const { rerender } = render(<Harness />);
      open();
      const save = screen.getByRole("button", { name: "Save" });
      save.focus();

      // jsdom, like some browsers, keeps focus on a control disabled under it.
      rerender(<Harness busy saveDisabled />);
      expect(save).toHaveFocus();

      rerender(<Harness saveDisabled />);

      expect(screen.getByRole("textbox", { name: "Name" })).toHaveFocus();
    });

    it("falls back to Cancel when it has no field", () => {
      const { rerender } = render(<Harness field={false} />);
      open();
      const cancel = screen.getByRole("button", { name: "Cancel" });

      // Dropped as it is disabled: jsdom will not blur a disabled control.
      act(() => cancel.blur());
      rerender(<Harness field={false} busy />);
      expect(document.body).toHaveFocus();
      rerender(<Harness field={false} />);

      expect(cancel).toHaveFocus();
    });

    it("leaves focus where it is while that is still usable", () => {
      const { rerender } = render(<Harness />);
      open();
      const save = screen.getByRole("button", { name: "Save" });
      save.focus();

      rerender(<Harness busy />);
      rerender(<Harness />);

      expect(save).toHaveFocus();
    });
  });

  describe("in a browser with showModal", () => {
    function stubModal() {
      const showModal = vi.fn(function (this: HTMLDialogElement) {
        this.setAttribute("open", "");
      });
      const close = vi.fn(function (this: HTMLDialogElement) {
        this.removeAttribute("open");
        this.dispatchEvent(new Event("close"));
      });
      Object.assign(HTMLDialogElement.prototype, { showModal, close });
      return { showModal, close };
    }

    it("opens modally, so the page behind is inert, and closes the same way", () => {
      const { showModal, close } = stubModal();
      const onClose = vi.fn();
      render(<Harness onClose={onClose} />);
      const { dialog, opener } = open();

      expect(showModal).toHaveBeenCalledTimes(1);
      expect(showModal.mock.contexts[0]).toBe(dialog);

      fireEvent.click(screen.getByRole("button", { name: "Cancel" }));

      expect(close).toHaveBeenCalledTimes(1);
      // Its own close is not taken for the browser's.
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(opener).toHaveFocus();
    });

    it("answers the browser's cancel request, and refuses it while busy", () => {
      stubModal();
      const onClose = vi.fn();
      const { rerender } = render(<Harness busy onClose={onClose} />);
      const { dialog } = open();

      const refused = new Event("cancel", { cancelable: true });
      act(() => {
        dialog.dispatchEvent(refused);
      });
      expect(refused.defaultPrevented).toBe(true);
      expect(onClose).not.toHaveBeenCalled();

      rerender(<Harness onClose={onClose} />);
      const accepted = new Event("cancel", { cancelable: true });
      act(() => {
        dialog.dispatchEvent(accepted);
      });
      expect(accepted.defaultPrevented).toBe(true);
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    it("follows the browser when it closes the dialog regardless", () => {
      stubModal();
      const onClose = vi.fn();
      render(<Harness busy onClose={onClose} />);
      const { dialog } = open();

      act(() => {
        dialog.removeAttribute("open");
        dialog.dispatchEvent(new Event("close"));
      });

      expect(onClose).toHaveBeenCalledTimes(1);
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });
});
