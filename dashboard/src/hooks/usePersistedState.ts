import { useEffect, useRef, useState } from "react";
import { readStored, writeStored } from "../utils/storage";

function load<T>(key: string, initial: T, legacyKey?: string): { value: T; fromLegacy: boolean } {
  let raw = readStored(key);
  let fromLegacy = false;
  if (raw == null && legacyKey) {
    raw = readStored(legacyKey);
    fromLegacy = raw != null;
  }
  if (raw == null) return { value: initial, fromLegacy: false };
  try {
    return { value: JSON.parse(raw) as T, fromLegacy };
  } catch {
    return { value: initial, fromLegacy: false };
  }
}

/**
 * `useState` whose value is mirrored to localStorage as JSON under `key`, read
 * once on mount. `legacyKey` is where an older build kept the same value: it
 * is read only when `key` is absent, and what it holds is copied to `key` at
 * once so the choice outlives the fallback.
 */
export function usePersistedState<T>(
  key: string,
  initial: T,
  legacyKey?: string,
): [T, React.Dispatch<React.SetStateAction<T>>] {
  const [loaded] = useState(() => load(key, initial, legacyKey));
  const [value, setValue] = useState<T>(loaded.value);

  // The first write is skipped unless it migrates: the value was just read
  // from this key, and writing it back would only wake other tabs.
  const first = useRef(true);
  useEffect(() => {
    if (first.current) {
      first.current = false;
      if (!loaded.fromLegacy) return;
    }
    writeStored(key, JSON.stringify(value));
  }, [key, value, loaded]);

  return [value, setValue];
}
