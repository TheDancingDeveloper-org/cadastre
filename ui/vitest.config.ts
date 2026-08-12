import { defineConfig } from "vitest/config";
import viteConfig from "./vite.config.ts";

export default defineConfig({
  ...viteConfig,
  test: { environment: "jsdom", globals: true, exclude: ["**/node_modules/**", "**/tests/e2e/**"] },
});
