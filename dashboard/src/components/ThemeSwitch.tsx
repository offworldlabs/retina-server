import { useRef } from "react";
import { useTheme, type ThemePreference } from "../context/ThemeContext";

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

/**
 * The appearance setting: three glyphs, one radiogroup. The header carries it,
 * and on a phone the drawer's foot does instead, so both places draw this one
 * control and a choice made in either reads the same in the other.
 */
export default function ThemeSwitch() {
  const { preference, setPreference } = useTheme();
  const radios = useRef<(HTMLButtonElement | null)[]>([]);

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
    // Otherwise the arrows scroll the page and Home/End jump it.
    e.preventDefault();
    setPreference(APPEARANCE[next].value);
    radios.current[next]?.focus();
  };

  return (
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
  );
}
