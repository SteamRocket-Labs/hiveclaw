/**
 * Playwright E2E facility (§9 P12) — the dev-server fixture the roadmap
 * requires before any Playwright acceptance is promised.
 *
 * Scope: browser-level UI flows against the REAL Vite app with the /api
 * surface mocked per-test via page.route() — deterministic and backend-free.
 * Full-stack E2E (real backend + PG) is a deployment-environment concern and
 * lands with P15's rollout validation.
 */

import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  fullyParallel: true,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: 'http://localhost:3008',
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:3008',
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
