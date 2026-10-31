/** How much of a name a phone always shows, however narrow: the extension and
 *  the end of the id, which is where two files' names differ. */
export const NAME_TAIL = 16;

/** A name cut for a narrow list: the last NAME_TAIL characters are the tail,
 *  the rest is the head. A name no longer than the tail has an empty head. */
export function cutName(name: string): { head: string; tail: string } {
  return { head: name.slice(0, -NAME_TAIL), tail: name.slice(-NAME_TAIL) };
}
