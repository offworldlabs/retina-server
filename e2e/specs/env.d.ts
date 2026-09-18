// Only tsconfig.e2e.json compiles this directory, and it must stay that way:
// in the browser program this global would let src code use `process`, which
// Vite does not polyfill, so it would typecheck and then throw at runtime.
declare const process: { env: Record<string, string | undefined> };
