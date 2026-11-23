import { useEffect, useId, useRef, useState } from "react";
import type { CurrentUser } from "@retina/shared";
import { useAuth } from "../context/AuthContext";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { isPublicRoute } from "../utils/publicRoutes";
import { useMediaQuery } from "../hooks/useMediaQuery";
import { PHONE } from "../styles/breakpoints";
import ThemeSwitch from "./ThemeSwitch";

type HeaderProps = {
  title: string;
  isAdmin?: boolean;
  /** The narrow screen's way to the sidebar. Without it there is no Menu button. */
  onMenu?: () => void;
  menuOpen?: boolean;
  menuButtonRef?: React.Ref<HTMLButtonElement>;
};

/**
 * Where focus goes for a key pressed in a menu of `count` items while item
 * `here` has it, or null for a key the menu leaves alone. `here` is -1 when no
 * item has focus, which the arrows treat as standing just outside either end.
 * The arrows wrap, as the radiogroup's do.
 */
export function menuFocusTarget(key: string, here: number, count: number): number | null {
  if (count < 1) return null;
  const last = count - 1;
  switch (key) {
    case "ArrowDown":
      return here >= last ? 0 : here + 1;
    case "ArrowUp":
      return here <= 0 ? last : here - 1;
    case "Home":
      return 0;
    case "End":
      return last;
    default:
      return null;
  }
}

/** The menu's items, in the order the arrows walk them. */
function itemsOf(menu: HTMLElement | null): HTMLElement[] {
  return Array.from(menu?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
}

function focusEnd(menu: HTMLElement | null, end: "first" | "last") {
  const items = itemsOf(menu);
  items[end === "first" ? 0 : items.length - 1]?.focus();
}

/** Must match the route in routes.tsx. */
const ACCOUNT_PATH = "/account";

type AccountMenuProps = {
  user: CurrentUser;
  signingOut: boolean;
  onSignOut: () => void;
  /** Offers the account page, which only the app console has. */
  accountPage: boolean;
};

/**
 * The account's menu button, after the APG pattern: one tab stop in the bar,
 * a popup whose items are reached by the arrows rather than by Tab, and focus
 * that goes back to the button when the popup is dismissed.
 *
 * Its own component so that its state goes with the session: a sign-out
 * unmounts it, and the next sign-in starts with the menu shut.
 */
function AccountMenu({ user, signingOut, onSignOut, accountPage }: AccountMenuProps) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  // Which end of the menu takes focus once it has opened. Set by the key that
  // opened it, and read after the render that shows the items, since a hidden
  // item cannot take focus.
  const focusOnOpen = useRef<"first" | "last">("first");
  const id = useId();
  const triggerId = `${id}account`;
  const menuId = `${id}account-menu`;
  const accountLineId = `${id}account-line`;

  // A new page shuts the menu. Adjusted during render rather than in an effect,
  // so the page never draws once with the menu still open over it.
  const { pathname } = useLocation();
  const [shownOn, setShownOn] = useState(pathname);
  if (shownOn !== pathname) {
    setShownOn(pathname);
    setOpen(false);
  }

  useEffect(() => {
    if (open) focusEnd(menu.current, focusOnOpen.current);
  }, [open]);

  // Registered only while open, and in the capture phase: the map's controls
  // stop their pointer events from bubbling, and a press on the map must shut
  // the menu as surely as one on the page. pointerdown as well as mousedown,
  // because a tap on a part of the page with no click handler of its own
  // fires no mouse events at all on iOS.
  useEffect(() => {
    if (!open) return;
    const onPress = (e: Event) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", onPress, true);
    document.addEventListener("mousedown", onPress, true);
    return () => {
      document.removeEventListener("pointerdown", onPress, true);
      document.removeEventListener("mousedown", onPress, true);
    };
  }, [open]);

  const show = (end: "first" | "last") => {
    if (open) {
      focusEnd(menu.current, end);
      return;
    }
    focusOnOpen.current = end;
    setOpen(true);
  };

  const dismiss = () => {
    setOpen(false);
    trigger.current?.focus();
  };

  // Enter and Space are taken on keydown and their default refused, so the
  // button's own click does not follow and toggle the menu straight back shut.
  const onTriggerKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    switch (e.key) {
      case "Enter":
      case " ":
      case "ArrowDown":
        show("first");
        break;
      case "ArrowUp":
        show("last");
        break;
      case "Escape":
        if (!open) return;
        e.stopPropagation();
        setOpen(false);
        break;
      default:
        return;
    }
    e.preventDefault();
  };

  const onMenuKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      e.preventDefault();
      // The narrow screen's drawer also shuts on an Escape that reaches the
      // document, and this one was meant for the menu alone.
      e.stopPropagation();
      dismiss();
      return;
    }
    // Shut, and the browser moves focus on as it would from anywhere else.
    if (e.key === "Tab") {
      setOpen(false);
      return;
    }
    const items = itemsOf(menu.current);
    const next = menuFocusTarget(e.key, items.indexOf(document.activeElement as HTMLElement), items.length);
    if (next === null) return;
    // Otherwise the arrows scroll the page and Home/End jump it.
    e.preventDefault();
    items[next].focus();
  };

  // The menu stays open while the request is in flight, so the pending state
  // is there to see; it closes when the identity goes, which unmounts it, or
  // when the page leaves for the edge's sign-out.
  const signOut = () => {
    if (!signingOut) onSignOut();
  };

  const label = user.name || user.email;

  return (
    <div className="account-menu" ref={root}>
      <button
        ref={trigger}
        id={triggerId}
        type="button"
        className="header-user"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={menuId}
        // The visible name alone would not say what the button opens, and
        // below the breakpoint the initial is all there is to see.
        aria-label={`Account: ${label}`}
        onClick={() => (open ? setOpen(false) : show("first"))}
        onKeyDown={onTriggerKeyDown}
      >
        <span className="user-initial" aria-hidden="true">
          {(label || "U")[0].toUpperCase()}
        </span>
        <span className="user-name">{label}</span>
      </button>
      {/* Kept in the document while shut, so aria-controls always names an
          element that exists. */}
      <div className="user-dropdown" hidden={!open}>
        {/* Text, not a disabled control, which a screen reader would announce
            as something that could not be used. The menu takes it as its
            description instead. */}
        <p id={accountLineId} className="user-dropdown-account">
          <span className="user-dropdown-label">Signed in as</span>{" "}
          <span className="user-dropdown-email">{user.email}</span>
        </p>
        <div
          ref={menu}
          id={menuId}
          role="menu"
          aria-labelledby={triggerId}
          aria-describedby={accountLineId}
          onKeyDown={onMenuKeyDown}
        >
          {accountPage && (
            // A link, so it can also be opened in a new tab. Enter and Space
            // are taken on keydown and turned into its click, so both open it
            // the same way a pointer does; a held Enter that opened the menu
            // repeats onto this item first, and must not follow it.
            <Link
              to={ACCOUNT_PATH}
              role="menuitem"
              tabIndex={-1}
              className="user-dropdown-item"
              // Shut and back to the button, as Escape leaves it: the page
              // may be the one already showing, which would not shut it.
              onClick={dismiss}
              onKeyDown={(e) => {
                if (e.key !== "Enter" && e.key !== " ") return;
                e.preventDefault();
                if (!e.repeat) e.currentTarget.click();
              }}
            >
              Account
            </Link>
          )}
          {/* Not a <button>: its native activation would also fire on the
              keyup of a Space whose keydown opened the menu from the trigger. */}
          <div
            role="menuitem"
            tabIndex={-1}
            className="user-dropdown-item"
            aria-disabled={signingOut || undefined}
            onClick={signOut}
            onKeyDown={(e) => {
              if (e.key !== "Enter" && e.key !== " ") return;
              e.preventDefault();
              // A held Enter repeats onto the item it just focused, which
              // would sign out whoever only meant to open the menu.
              if (!e.repeat) signOut();
            }}
          >
            {signingOut ? "Signing out…" : "Sign out"}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function Header({ title, isAdmin = false, onMenu, menuOpen = false, menuButtonRef }: HeaderProps) {
  const { user, logout, signingOut } = useAuth();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  // A phone's bar holds Menu, the title and the account, and no more: the
  // appearance switch moves to the foot of the drawer that Menu opens. With no
  // Menu there is no drawer to reach it in, so it stays here at every width.
  // Sidebar's footer keys off the same pair.
  const phone = useMediaQuery(PHONE);
  const switchInBar = !(phone && onMenu);

  // Read when the session ends, not at the click: the caller may have moved
  // on while the sign-out request was in flight.
  const currentPath = useRef(pathname);
  useEffect(() => {
    currentPath.current = pathname;
  }, [pathname]);

  // An open page stays put and redraws signed out. Any other would bounce to
  // the sign-in card, so it gives way to the map, replaced in history because
  // it is no longer there to go back to. The admin console has no open pages
  // and no map, so RequireAuth's bounce stands there. The same holds when the
  // server did not confirm the sign-out: the identity is dropped either way,
  // and the provider's notice says what is left.
  const handleLogout = () => {
    logout(() => {
      if (!isAdmin && !isPublicRoute(currentPath.current, isAdmin)) navigate("/map", { replace: true });
    });
  };

  return (
    <header className="header">
      <div className="header-lead">
        {onMenu && (
          // Shown only below the breakpoint, where the sidebar leaves the row.
          <button
            ref={menuButtonRef}
            className="header-menu"
            onClick={onMenu}
            aria-label="Menu"
            aria-expanded={menuOpen}
            aria-controls="console-sidebar"
            title="Menu"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
              aria-hidden="true"
            >
              <line x1="3" y1="6" x2="21" y2="6" />
              <line x1="3" y1="12" x2="21" y2="12" />
              <line x1="3" y1="18" x2="21" y2="18" />
            </svg>
          </button>
        )}
        <div className="header-title">{title}</div>
      </div>
      <div className="header-actions">
        {/* In the bar rather than the account menu, so a caller with no session
            has it too, and finds it in the same place once they have one. */}
        {switchInBar && <ThemeSwitch />}
        {/* A caller with no session is on one of the open routes, and the
            way on from there is in, not out, and back to this page. */}
        {!user ? (
          <Link className="header-signin" to="/login" state={{ fromOpenPage: true, next: pathname }}>
            Sign in
          </Link>
        ) : (
          <AccountMenu user={user} signingOut={signingOut} onSignOut={handleLogout} accountPage={!isAdmin} />
        )}
      </div>
    </header>
  );
}
