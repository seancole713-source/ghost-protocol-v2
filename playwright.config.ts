import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.BASE_URL || "https://ghost-protocol-v2-production.up.railway.app";

export default defineConfig({
  testDir: "./e2e",
  globalTimeout: 180_000,
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
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "mobile-chromium",
      use: { ...devices["Pixel 7"] },
    },
  ],
});
