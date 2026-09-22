import { describe, expect, it } from "vitest";

/* A class that no stylesheet defines renders at the browser's defaults, and
   nothing else notices: the page still works, it only looks wrong. This reads
   every component's className and every stylesheet the console loads, and
   asks that each class named has a rule somewhere. It sees literals only: a
   class a helper returns or a lookup table holds, such as a severity map,
   is out of its sight. */

const sources = Object.fromEntries(
  Object.entries(
    import.meta.glob("../**/*.tsx", { query: "?raw", import: "default", eager: true }) as Record<string, string>,
  ).filter(([file]) => !file.startsWith("../test/")),
);

const sheets = {
  ...import.meta.glob("../**/*.css", { query: "?raw", import: "default", eager: true }),
  ...import.meta.glob("../../../packages/shared/css/*.css", { query: "?raw", import: "default", eager: true }),
} as Record<string, string>;

// Named for something other than styling.
const UNSTYLED = new Map([
  ["node-marker-synthetic", "a handle for the E2E suite"],
  ["de-map-centre", "groups the centre marker's SVG for a reader"],
  ["privacy-control", "names the control's wrapper for a reader"],
]);

const CLASS = /^-?[_a-zA-Z][\w-]*$/;

// A class is defined outright by a rule that names it alone on an element
// (`.btn`, `.card .badge`), and only in company by one that compounds it
// (`.login-btn.secondary`): there, the element has to carry the others too.
const defined = new Set<string>();
const compounds: string[][] = [];
for (const css of Object.values(sheets)) {
  const preludes = css.replace(/\/\*[\s\S]*?\*\//g, "").matchAll(/([^{};]+)\{/g);
  for (const [, prelude] of preludes) {
    // Arguments and attribute tests are not the element's own classes.
    const bare = prelude.replace(/\([^)]*\)/g, "").replace(/\[[^\]]*\]/g, "");
    for (const compound of bare.split(/[\s,>+~]+/)) {
      const classes = [...compound.matchAll(/\.(-?[_a-zA-Z][\w-]*)/g)].map((m) => m[1]);
      if (classes.length === 1) defined.add(classes[0]);
      else if (classes.length > 1) compounds.push(classes);
    }
  }
}
const inCompound = new Set(compounds.flat());

/** Whether `cls` has a rule, given every class its element can carry. */
function styled(cls: string, carried: Set<string>): boolean {
  return defined.has(cls) || compounds.some((c) => c.includes(cls) && c.every((x) => carried.has(x)));
}

/** The text of the `{...}` that opens at `start`, braces balanced. */
function braced(src: string, start: number): string {
  let depth = 0;
  for (let i = start; i < src.length; i++) {
    if (src[i] === "{") depth++;
    else if (src[i] === "}" && --depth === 0) return src.slice(start + 1, i);
  }
  return "";
}

// A string is a class when it is what the expression evaluates to: a branch
// of a ternary or a logical operator, or the whole expression. One compared
// against or passed to a call is not.
const BRANCH = /(?:^|[?:]|&&|\|\|)\s*(["'])([^"']*)\1/g;
// What a fragment glued to an interpolation can be.
const FRAGMENT = /^[\w-]+$/;

/** Every class a className expression can produce: whole names, and the
 *  fragments glued to an interpolation's either side, or both. */
function classesIn(expr: string) {
  const names: string[] = [];
  const prefixes: string[] = [];
  const suffixes: string[] = [];
  const infixes: string[] = [];
  const outside = expr.replace(/`([^`]*)`/g, (_, template: string) => {
    const parts = template.split(/\$\{([^}]*)\}/);
    parts.forEach((part, i) => {
      if (i % 2 === 1) {
        for (const m of part.matchAll(BRANCH)) names.push(...m[2].split(/\s+/));
        return;
      }
      const tokens = part.split(/\s+/);
      // A token glued to an interpolation is only part of a name, unless every
      // branch the interpolation can yield opens or closes on a space.
      const branches = (at: number) => [...(parts[at] ?? "").matchAll(BRANCH)].map((m) => m[2]).filter(Boolean);
      const touches = (at: number, edge: RegExp) => branches(at).some((b) => !edge.test(b)) || !branches(at).length;
      const gluedBefore = i < parts.length - 1 && !/\s$/.test(part) && touches(i + 1, /^\s/);
      const gluedAfter = i > 0 && !/^\s/.test(part) && touches(i - 1, /\s$/);
      if (gluedBefore && gluedAfter && tokens.length === 1) {
        infixes.push(tokens[0]);
        return;
      }
      const before = gluedBefore ? tokens.pop() : undefined;
      const after = gluedAfter ? tokens.shift() : undefined;
      names.push(...tokens);
      if (before) prefixes.push(before);
      if (after) suffixes.push(after);
    });
    return "";
  });
  for (const m of outside.matchAll(BRANCH)) names.push(...m[2].split(/\s+/));
  const fragments = (list: string[]) => list.filter((f) => FRAGMENT.test(f));
  return {
    names: names.filter((n) => CLASS.test(n)),
    prefixes: fragments(prefixes),
    suffixes: fragments(suffixes),
    infixes: fragments(infixes),
  };
}

type Use = { cls: string; glued?: "prefix" | "suffix" | "infix"; carried: Set<string>; where: string };
const used: Use[] = [];
for (const [file, src] of Object.entries(sources)) {
  for (const m of src.matchAll(/className=(?:"([^"]*)"|\{)/g)) {
    const line = src.slice(0, m.index).split("\n").length;
    const where = `${file.replace("../", "")}:${line}`;
    const { names, prefixes, suffixes, infixes } =
      m[1] !== undefined
        ? { names: m[1].split(/\s+/).filter((n) => CLASS.test(n)), prefixes: [], suffixes: [], infixes: [] }
        : classesIn(braced(src, m.index + m[0].length - 1));
    // Every class the element can carry, whichever branch is taken: a compound
    // counts as met when its members all appear somewhere in the expression.
    const carried = new Set(names);
    for (const cls of names) used.push({ cls, carried, where });
    for (const cls of prefixes) used.push({ cls, glued: "prefix", carried, where });
    for (const cls of suffixes) used.push({ cls, glued: "suffix", carried, where });
    for (const cls of infixes) used.push({ cls, glued: "infix", carried, where });
  }
}

const everyClass = [...defined, ...inCompound];
function hasRule({ cls, glued, carried }: Use): boolean {
  if (glued === "prefix") return everyClass.some((d) => d.startsWith(cls));
  if (glued === "suffix") return everyClass.some((d) => d.endsWith(cls));
  if (glued === "infix") return everyClass.some((d) => d.includes(cls));
  return styled(cls, carried) || UNSTYLED.has(cls);
}

describe("the console's class names", () => {
  it("finds components and stylesheets to compare, so an empty glob cannot pass", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(50);
    expect(Object.keys(sheets).some((f) => f.endsWith("packages/shared/css/ui.css"))).toBe(true);
    expect(used.length).toBeGreaterThan(500);
  });

  it("each have a rule in some stylesheet", () => {
    const missing = used.filter((use) => !hasRule(use)).map(({ cls, where }) => `${cls} (${where})`);
    expect(missing).toEqual([]);
  });

  it("keep no exemption for a class that has since been styled or removed", () => {
    const named = new Set(used.map(({ cls }) => cls));
    expect([...UNSTYLED.keys()].filter((cls) => defined.has(cls) || inCompound.has(cls) || !named.has(cls))).toEqual(
      [],
    );
  });

  // The two shapes a looser reading lets through.
  it("counts a compounded class only on an element carrying its partner", () => {
    expect(defined.has("secondary")).toBe(false);
    expect(styled("secondary", new Set(["login-btn", "secondary"]))).toBe(true);
    expect(styled("secondary", new Set(["secondary"]))).toBe(false);
  });

  it("checks the fragment glued after an interpolation, or between two", () => {
    expect(classesIn("`icon-${kind}-off`").suffixes).toEqual(["-off"]);
    expect(classesIn("`${a}-mid-${b}`").infixes).toEqual(["-mid-"]);
  });

  it("reads a branch in either quote", () => {
    expect(classesIn("on ? 'active' : \"idle\"").names).toEqual(["active", "idle"]);
  });
});
