import { useState, useRef, useEffect } from "react";
import { useAuth } from "../context/AuthContext";
import { useTheme, type ThemePreference } from "../context/ThemeContext";
import { Link, useNavigate } from "react-router-dom";

/* Feather's sun, monitor and moon, inlined in the same house style as the
   sidebar's set: 24-unit box, no fill, 2-unit round-capped stroke in
   currentColor, so each one inherits whichever ink its button is drawn with.

   Ordered light → system → dark, which reads as a run from one extreme to the
   other with the neutral in the middle, rather than putting the default first
   and the two opposites side by side. */
const APPEARANCE: readonly { value: ThemePreference; label: string; icon: React.ReactNode }[] = [
  {
    value: "light",
    label: "Light",
    icon: (
      <>
        <circle cx="12" cy="12" r="5" />
        <line x1="12" y1="1" x2="12" y2="3" />
        <line x1="12" y1="21" x2="12" y2="23" />
        <line x1="4.22" y1="4.22" x2="5.64" y2="5.64" />
        <line x1="18.36" y1="18.36" x2="19.78" y2="19.78" />
        <line x1="1" y1="12" x2="3" y2="12" />
        <line x1="21" y1="12" x2="23" y2="12" />
        <line x1="4.22" y1="19.78" x2="5.64" y2="18.36" />
        <line x1="18.36" y1="5.64" x2="19.78" y2="4.22" />
      </>
    ),
  },
  {
    value: "system",
    label: "System",
    icon: (
      <>
        <rect x="2" y="3" width="20" height="14" rx="2" ry="2" />
        <line x1="8" y1="21" x2="16" y2="21" />
        <line x1="12" y1="17" x2="12" y2="21" />
      </>
    ),
  },
  {
    value: "dark",
    label: "Dark",
    icon: <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />,
  },
];

export default function Header({ title }) {
  const { user, logout } = useAuth();
  const { preference, setPreference } = useTheme();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const radios = useRef<(HTMLButtonElement | null)[]>([]);
  const navigate = useNavigate();

  useEffect(() => {
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  const handleLogout = async () => {
    const { redirected } = await logout();
    if (!redirected) navigate("/login");
  };

  /**
   * The keyboard half of the radiogroup. A `role="radio"` set is one tab stop,
   * not three, and the arrows move between the options — so the group carries
   * a roving tabIndex (only the checked button is reachable by Tab) and the
   * arrows both select and focus, which is how a radio group is expected to
   * behave and how a screen reader announces it.
   */
  const onRadioKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const last = APPEARANCE.length - 1;
    const here = APPEARANCE.findIndex((a) => a.value === preference);
    let next: number;
    switch (e.key) {
      case "ArrowLeft":
      case "ArrowUp":
        next = here <= 0 ? last : here - 1;
        break;
      case "ArrowRight":
      case "ArrowDown":
        next = here >= last ? 0 : here + 1;
        break;
      case "Home":
        next = 0;
        break;
      case "End":
        next = last;
        break;
      default:
        return;
    }
    // Arrows scroll the dropdown otherwise, and Home/End jump the page.
    e.preventDefault();
    setPreference(APPEARANCE[next].value);
    radios.current[next]?.focus();
  };

  return (
    <header className="header">
      <div className="header-title">{title}</div>
      <div className="header-actions">
        {/* A caller with no session is on one of the open routes, and the
            way on from there is in, not out. */}
        {!user ? (
          <Link className="header-signin" to="/login">
            Sign in
          </Link>
        ) : (
        <div className="header-user" ref={ref} onClick={() => setOpen(!open)}>
          {user?.avatar ? (
            <img src={user.avatar} alt="" referrerPolicy="no-referrer" />
          ) : (
            <div
              style={{
                width: 28,
                height: 28,
                borderRadius: "50%",
                background: "var(--accent)",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                fontSize: 13,
                fontWeight: 600,
                color: "var(--accent-ink)",
              }}
            >
              {(user?.name || "U")[0].toUpperCase()}
            </div>
          )}
          <span className="user-name">{user?.name}</span>
          {open && (
            <div className="user-dropdown">
              <button disabled style={{ color: "var(--text-muted)", fontSize: 11 }}>
                {user?.email}
              </button>
              {/* Clicks are stopped here because the whole .header-user toggles
                  the menu: without it the menu shuts on the first press, and
                  comparing the three settings means reopening it each time. */}
              <div className="dropdown-group" onClick={(e) => e.stopPropagation()}>
                <span className="dropdown-label">Appearance</span>
                <div
                  className="theme-switch"
                  role="radiogroup"
                  aria-label="Appearance"
                  onKeyDown={onRadioKeyDown}
                >
                  {APPEARANCE.map(({ value, label, icon }, i) => (
                    <button
                      key={value}
                      ref={(el) => {
                        radios.current[i] = el;
                      }}
                      role="radio"
                      aria-checked={preference === value}
                      // One tab stop for the group, not three: Tab reaches the
                      // checked option and the arrows move from there.
                      tabIndex={preference === value ? 0 : -1}
                      // The glyph carries no text, so the name has to be said
                      // outright; `title` gives the same word as a tooltip, for
                      // anyone who cannot tell the monitor from the moon.
                      aria-label={label}
                      title={label}
                      className={preference === value ? "active" : ""}
                      onClick={() => setPreference(value)}
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
                        {icon}
                      </svg>
                    </button>
                  ))}
                </div>
              </div>
              <button onClick={handleLogout}>Sign out</button>
            </div>
          )}
        </div>
        )}
      </div>
    </header>
  );
}
