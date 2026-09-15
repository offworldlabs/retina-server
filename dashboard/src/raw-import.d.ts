/* Vite's raw import, which the theme tests use to read App.css and
   public/theme-boot.js as text rather than through fs — the dashboard carries
   no @types/node.

   Its own file because env.d.ts has a top-level import, which makes every
   `declare module` in it an augmentation of an existing module rather than an
   ambient declaration of a new one. */
declare module "*?raw" {
  const contents: string;
  export default contents;
}

/* Just the one member of vite/client the tests use. Declared rather than
   referencing the whole type package, which redeclares the asset modules
   env.d.ts already owns. */
interface ImportMeta {
  glob(
    pattern: string,
    options?: { query?: string; import?: string; eager?: boolean },
  ): Record<string, unknown>;
}
