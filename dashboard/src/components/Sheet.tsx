import {
  useCallback,
  useId,
  useLayoutEffect,
  useRef,
  type KeyboardEvent,
  type MouseEvent,
  type PointerEvent,
  type ReactNode,
  type RefObject,
} from "react";

import { tabStops, useModalDialog } from "./Dialog";
import "./sheet.css";

/** How many sheets hold the page still. It scrolls again when the last one
 *  lets go, whichever order they close in. */
let holders = 0;

/** What the page's scrollers carried inline before the first sheet held them. */
let held: { style: CSSStyleDeclaration; property: string; value: string; priority: string }[] = [];

/** Stops the page scrolling beneath the sheets: the document, and the
 *  console's own scroller, `.content`. Returns what lets it go again, which
 *  puts back exactly the inline values it found. */
function holdPageScroll() {
  if (holders++ === 0) {
    const scrollers = [document.documentElement, document.body, ...document.querySelectorAll<HTMLElement>(".content")];
    for (const { style } of scrollers) {
      for (const property of ["overflow-x", "overflow-y"]) {
        held.push({
          style,
          property,
          value: style.getPropertyValue(property),
          priority: style.getPropertyPriority(property),
        });
        style.setProperty(property, "hidden");
      }
    }
  }
  let released = false;
  return () => {
    if (released) return;
    released = true;
    if (--holders > 0) return;
    for (const { style, property, value, priority } of held) {
      if (value) style.setProperty(property, value, priority);
      else style.removeProperty(property);
    }
    held = [];
  };
}

export interface SheetProps {
  /** Shown, modally, while true; nothing is rendered while false. */
  open: boolean;
  /** Escape, the close button, a tap on the backdrop, and the browser's own
   *  close request, such as Android's back gesture. */
  onClose: () => void;
  /** The heading, which also names the sheet. */
  title: ReactNode;
  /** A line under the heading that describes the sheet. */
  description?: ReactNode;
  /** "bottom" rises from the foot of the screen, up to 90% of its height and,
   *  on a tablet, no wider than a phone. "full" takes the whole screen, for
   *  content that wants all of it, such as a file preview. */
  variant?: "bottom" | "full";
  /** Buttons along the sheet's foot, below the part that scrolls. */
  actions?: ReactNode;
  /** Focused on open unless it is disabled. Without it, the first control in
   *  the sheet's content, not its header or its actions; with none, the close
   *  button, which changes nothing. */
  initialFocus?: RefObject<HTMLElement | null>;
  /** Added to the `<dialog>`, for styling a sheet's own content. */
  className?: string;
  children?: ReactNode;
}

/** A modal panel for phone and tablet layouts: the map's Layers, the Data
 *  Explorer's preview. Its stylesheet has rules only below 1024px, so a caller
 *  renders it only while `useMediaQuery(TABLET)` matches and keeps its desktop
 *  panel above that.
 *
 *  It is the Dialog's kind of modal, with the same focus handling: a native
 *  `<dialog>` opened with showModal, so the page behind is inert; focus moves
 *  in on open, Tab cycles inside, and focus goes back to whatever opened it.
 *  Escape, the close button and a tap on the backdrop call `onClose`. While
 *  any sheet is open the page does not scroll, and a drag inside one neither
 *  pans a map beneath nor scrolls the page: only its body scrolls, and that
 *  stops at its ends. */
export function Sheet(props: SheetProps) {
  return props.open ? <OpenSheet {...props} /> : null;
}

function OpenSheet({
  onClose,
  title,
  description,
  variant = "bottom",
  actions,
  initialFocus,
  className,
  children,
}: SheetProps) {
  const dialog = useRef<HTMLDialogElement>(null);
  const body = useRef<HTMLDivElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  // Before paint, as the dialog opens, so the page never scrolls under it.
  useLayoutEffect(() => holdPageScroll(), []);

  const firstFocus = useCallback(
    () => (body.current ? tabStops(body.current)[0] : undefined) ?? close.current,
    [],
  );
  const modal = useModalDialog(dialog, { onClose, initialFocus, fallbackFocus: firstFocus });

  function onKeyDown(event: KeyboardEvent<HTMLDialogElement>) {
    modal.onKeyDown(event);
    // The page behind is inert, so its shortcuts, such as the map's single
    // keys, must not act on keys pressed in here either.
    event.stopPropagation();
  }

  // A tap on the backdrop, the dialog's own box around the panel, closes it:
  // a press that both began and ended there. A drag between the panel and
  // the backdrop, either way, must not, though the browser sends its click
  // to the dialog as the box that holds both ends. A click a key makes lands
  // on a control, never on the dialog.
  const backdropPress = useRef<"began" | "ended" | null>(null);
  function onPointerDown(event: PointerEvent<HTMLDialogElement>) {
    backdropPress.current = event.target === event.currentTarget ? "began" : null;
  }
  function onPointerUp(event: PointerEvent<HTMLDialogElement>) {
    const ended = backdropPress.current === "began" && event.target === event.currentTarget;
    backdropPress.current = ended ? "ended" : null;
  }
  function onPointerCancel() {
    backdropPress.current = null;
  }
  function onClick(event: MouseEvent<HTMLDialogElement>) {
    const tap = backdropPress.current === "ended" && event.target === event.currentTarget;
    backdropPress.current = null;
    if (tap) onClose();
  }

  return (
    <dialog
      ref={dialog}
      className={`sheet ${variant === "full" ? "sheet-full" : "sheet-bottom"}${className ? " " + className : ""}`}
      aria-labelledby={titleId}
      aria-describedby={description != null ? descriptionId : undefined}
      onKeyDown={onKeyDown}
      onCancel={modal.onCancel}
      onClose={modal.onClose}
      onPointerDown={onPointerDown}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerCancel}
      onClick={onClick}
    >
      <div className="sheet-panel" tabIndex={-1}>
        <div className="sheet-head">
          <div className="sheet-heading">
            <h2 id={titleId} className="sheet-title">
              {title}
            </h2>
            {description != null && (
              <p id={descriptionId} className="sheet-description">
                {description}
              </p>
            )}
          </div>
          <button ref={close} type="button" className="sheet-close" aria-label="Close" onClick={onClose}>
            <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true" focusable="false">
              <path d="M6 6l12 12M18 6L6 18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
            </svg>
          </button>
        </div>
        <div ref={body} className="sheet-body">{children}</div>
        {actions != null && <div className="btn-row sheet-actions">{actions}</div>}
      </div>
    </dialog>
  );
}
