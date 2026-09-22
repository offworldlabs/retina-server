/**
 * localStorage for preferences. Storage throws in private mode and on a full
 * quota, and a console that will not render is worse than one that forgets a
 * choice, so every failure here is a value not read or not kept.
 */

export function readStored(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function writeStored(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    /* the choice still holds for this tab */
  }
}
