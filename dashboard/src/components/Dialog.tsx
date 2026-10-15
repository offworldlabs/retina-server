import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  type FormEvent,
  type KeyboardEvent,
  type ReactNode,
  type RefObject,
  type SyntheticEvent,
} from "react";

import "./dialog.css";

/** What Tab can land on inside the panel. */
const TABBABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/** Where focus goes on open when the caller names nothing: the first field. */
const FIELD = "input:not([disabled]), select:not([disabled]), textarea:not([disabled])";

/** Where focus starts in `panel`: what the caller names, unless it is
 *  disabled, else the first field, else `fallback`. */
function startOf(panel: HTMLElement, named: HTMLElement | null | undefined, fallback: HTMLElement | null) {
  if (named && !named.matches(":disabled")) return named;
  return panel.querySelector<HTMLElement>(FIELD) ?? fallback;
}

export interface DialogProps {
  /** Shown, modally, while true; nothing is rendered while false. */
  open: boolean;
  /** The heading, which also names the dialog. */
  title: ReactNode;
  /** A line under the heading that describes the dialog: what confirming does. */
  description?: ReactNode;
  /** Cancel and Escape. Never called while `busy`. */
  onClose: () => void;
  /** A request is in flight: Escape does nothing and Cancel is disabled, so
   *  the answer decides what happens next. When it ends with the dialog open,
   *  focus that fell out of it comes back. */
  busy?: boolean;
  /** Enter in a field, or an action of `type="submit"`. */
  onSubmit?: () => void;
  /** The buttons set before Cancel, the one that confirms first. */
  actions?: ReactNode;
  cancelLabel?: string;
  /** Focused on open, and again when a request ends with focus lost. Without
   *  it, or while it is disabled, the first field, or else Cancel, the one
   *  button that changes nothing. */
  initialFocus?: RefObject<HTMLElement>;
  children?: ReactNode;
}

/** The console's modal: a native `<dialog>` opened with showModal, so the page
 *  behind is inert and the panel sits in the top layer above the header and
 *  the navigation drawer. Focus moves in on open, Tab cycles inside, Escape
 *  and Cancel close it, and focus goes back to whatever opened it. */
export function Dialog(props: DialogProps) {
  return props.open ? <OpenDialog {...props} /> : null;
}

function OpenDialog({
  title,
  description,
  onClose,
  busy = false,
  onSubmit,
  actions,
  cancelLabel = "Cancel",
  initialFocus,
  children,
}: DialogProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  // Set while this component is closing the dialog itself, so the close event
  // that follows is not taken for the browser closing it.
  const closing = useRef(false);
  const titleId = useId();
  const descriptionId = useId();

  // Before paint, so the panel is never drawn in the page's flow first.
  useLayoutEffect(() => {
    const node = dialog.current;
    if (!node) return;
    const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closing.current = false;
    // jsdom has no showModal; the attribute shows it there, without the inert
    // page, and everything below still holds.
    if (typeof node.showModal === "function") {
      if (!node.open) node.showModal();
    } else {
      node.setAttribute("open", "");
    }
    startOf(node, initialFocus?.current, cancel.current)?.focus();
    return () => {
      closing.current = true;
      if (typeof node.close === "function") node.close();
      else node.removeAttribute("open");
      if (opener?.isConnected) opener.focus();
    };
    // Once per opening: what has focus at that moment is what gets it back.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A request that ends with the dialog still open, refused or failed, hands
  // focus back inside it. The button pressed was disabled while it ran, and a
  // browser drops focus from a disabled control to the page, where Tab and a
  // screen reader would lose the dialog.
  const wasBusy = useRef(busy);
  useEffect(() => {
    const ended = wasBusy.current && !busy;
    wasBusy.current = busy;
    const node = dialog.current;
    if (!ended || !node) return;
    const active = document.activeElement;
    const usable =
      active instanceof HTMLElement && active !== node && node.contains(active) && !active.matches(":disabled");
    if (!usable) startOf(node, initialFocus?.current, cancel.current)?.focus();
  }, [busy, initialFocus]);

  function onKeyDown(event: KeyboardEvent<HTMLDialogElement>) {
    const node = dialog.current;
    if (!node) return;
    if (event.key === "Escape") {
      // Cancelled here, the browser sends no cancel event of its own, so a
      // busy dialog stays open however often Escape is pressed.
      event.preventDefault();
      event.stopPropagation();
      if (!busy) onClose();
      return;
    }
    if (event.key !== "Tab") return;
    const stops = Array.from(node.querySelectorAll<HTMLElement>(TABBABLE));
    if (stops.length === 0) {
      event.preventDefault();
      return;
    }
    const first = stops[0];
    const last = stops[stops.length - 1];
    const active = document.activeElement;
    const outside = !active || !node.contains(active) || active === node;
    if (event.shiftKey && (outside || active === first)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (outside || active === last)) {
      event.preventDefault();
      first.focus();
    }
  }

  // A close request that is not a key press, such as Android's back gesture.
  function onCancel(event: SyntheticEvent<HTMLDialogElement>) {
    event.preventDefault();
    if (!busy) onClose();
  }

  // The browser may close it regardless, after repeated close requests; the
  // caller's state follows rather than leaving an open dialog nobody can see.
  function onNativeClose() {
    if (!closing.current && dialog.current && !dialog.current.open) onClose();
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!busy) onSubmit?.();
  }

  return (
    <dialog
      ref={dialog}
      className="dialog"
      aria-labelledby={titleId}
      aria-describedby={description != null ? descriptionId : undefined}
      onKeyDown={onKeyDown}
      onCancel={onCancel}
      onClose={onNativeClose}
    >
      <form className="dialog-panel" onSubmit={submit} noValidate>
        <h2 id={titleId} className="dialog-title">
          {title}
        </h2>
        {description != null && (
          <p id={descriptionId} className="dialog-description">
            {description}
          </p>
        )}
        {children != null && <div className="dialog-body">{children}</div>}
        <div className="btn-row dialog-actions">
          {actions}
          <button ref={cancel} type="button" className="btn btn-secondary" onClick={onClose} disabled={busy}>
            {cancelLabel}
          </button>
        </div>
      </form>
    </dialog>
  );
}
