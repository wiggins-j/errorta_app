import { defineConfig } from "vitest/config";

// Local config so this Worker project's tests aren't swept up by the root
// Errorta vitest config (which globs src/** under happy-dom). These are plain
// node tests over the Worker's pure logic + handlers.
export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    environment: "node",
  },
});
