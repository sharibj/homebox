import { defineConfig } from "@playwright/test";
export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.mjs",
  workers: 1,
  timeout: 45000,
  use: {
    baseURL: process.env.BASE_URL || "http://localhost:4173",
    viewport: { width: 390, height: 844 },
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  outputDir: "/results",
  reporter: [["list"]],
});
