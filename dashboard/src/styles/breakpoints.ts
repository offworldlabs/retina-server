/* The widths where the console changes shape, for code that needs to know.
   CSS cannot read these, so each stylesheet repeats the literal widths in its
   own @media rules, and test/breakpoints.test.ts keeps both to the same set. */

/** Phones: the single-column layouts switch on here. */
export const PHONE = "(max-width: 640px)";

/** The sidebar is an off-canvas drawer up to here, and a rail or full column above. */
export const DRAWER = "(max-width: 768px)";

/** Tablets and below. The .98 leaves no fractional width between this and `min-width: 1024px`. */
export const TABLET = "(max-width: 1023.98px)";

/** Touch tablets and below, for controls that need a finger-sized target. */
export const COARSE_TABLET = "(max-width: 1023.98px) and (pointer: coarse)";
