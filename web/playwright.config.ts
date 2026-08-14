import { defineConfig, devices } from "@playwright/test";
import path from "node:path";

const WEB_ROOT = __dirname;
const LOCAL_WEB_PORT = process.env.PW_WEB_PORT || "3000";
const LOCAL_BASE_URL = `http://127.0.0.1:${LOCAL_WEB_PORT}`;
const BASE_URL = process.env.WEB_BASE_URL || LOCAL_BASE_URL;
const SERIAL_MODE = process.env.PW_SERIAL === "1";
const START_LOCAL_WEB_SERVER = !process.env.WEB_BASE_URL;

export default defineConfig({
  testDir: path.join(WEB_ROOT, "tests"),
  outputDir: path.join(WEB_ROOT, "test-results"),
  snapshotPathTemplate: path.join(
    WEB_ROOT,
    "tests",
    "{testFilePath}-snapshots",
    "{arg}{-platform}{ext}",
  ),
  fullyParallel: !SERIAL_MODE,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: SERIAL_MODE ? 1 : undefined,
  reporter: [
    [
      "html",
      {
        open: "never",
        outputFolder: path.join(WEB_ROOT, "playwright-report"),
      },
    ],
    ["list"],
  ],
  use: {
    baseURL: BASE_URL,
    trace: "on-first-retry",
  },
  globalSetup: START_LOCAL_WEB_SERVER
    ? path.join(WEB_ROOT, "tests", "e2e", "support", "managed-web-server.ts")
    : undefined,
  projects: [
    {
      name: "teaching-flow",
      testMatch: [
        "**/e2e/teacher-classroom-flow.spec.ts",
        "**/e2e/content-operations-flow.spec.ts",
        "**/e2e/student-classroom-flow.spec.ts",
        "**/e2e/classroom-learning-loop.spec.ts",
      ],
      workers: 1,
      use: {
        ...devices["Desktop Chrome"],
        channel: "chromium",
      },
    },
    {
      name: "ui-audit",
      testMatch: "**/*.audit.ts",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "tailwind-migration-baseline",
      testMatch: [
        "**/tailwind-migration-baseline.spec.ts",
        "**/classroom-*.visual.spec.ts",
      ],
      use: {
        ...devices["Desktop Chrome"],
        channel: "chromium",
        launchOptions: {
          args: ["--disable-gpu"],
        },
      },
    },
  ],
});
