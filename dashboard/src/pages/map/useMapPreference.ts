import { usePersistedState } from "../../hooks/usePersistedState";

/**
 * A map display preference, stored under `retina.map.<name>`. The standalone
 * map kept the same names under `tf.`, which is read when the new key is
 * absent so a choice made there survives.
 */
export function useMapPreference<T>(name: string, initial: T) {
  return usePersistedState(`retina.map.${name}`, initial, `tf.${name}`);
}
