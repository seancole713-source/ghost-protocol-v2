import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.BASE_URL || "https://ghost-protocol-v2-production.up.railway.app";

export default defineConfig({
  testDir: "./e2e",
  // A serial production audit spans desktop, mobile, and the API surface.
  // Railway cold paths can make the complete 71-test run exceed three minutes.
  globalTimeout: 600_000,
  timeout: 30_000,
  expect: {
    timeout: 10_000,
  },
  fullyParallel: true,
  workers: 1,
  retries: 0,
  use: {
    baseURL,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "desktop-chromium",
      testIgnore: /api-surface\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "mobile-chromium",
      testIgnore: /api-surface\.spec\.ts/,
      use: { ...devices["Pixel 7"] },
    },
    {
      name: "api-json",
      testMatch: /api-surface\.spec\.ts/,
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
