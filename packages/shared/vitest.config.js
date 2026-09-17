import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    // So `tokens.css?raw` reaches the token test as its text. Vitest stubs every
    // CSS import with an empty string by default, the raw query included — and
    // a stub does not fail the import, it fails the assertions further down.
    css: true,
  },
});
