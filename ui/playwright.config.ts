import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "tests/e2e",
  outputDir: "../.ci-artifacts/e2e/test-results",
  webServer: {
    command: "npm run build && npx vite --host 127.0.0.1",
    port: 5173,
    reuseExistingServer: false,
  },
  use: {
    baseURL: "http://127.0.0.1:5173",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
});
