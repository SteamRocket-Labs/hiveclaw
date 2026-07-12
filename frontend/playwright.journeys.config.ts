import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig, devices } from '@playwright/test';


const frontendRoot = path.dirname(fileURLToPath(import.meta.url));
const backendRoot = path.resolve(frontendRoot, '../backend');
const python = process.env.HIVE_JOURNEY_PYTHON
  || (fs.existsSync(path.join(backendRoot, '.venv/bin/python')) ? path.join(backendRoot, '.venv/bin/python') : 'python');
const schemaDatabaseUrl = process.env.HIVE_JOURNEY_SCHEMA_DATABASE_URL
  || process.env.DATABASE_URL
  || 'postgresql+asyncpg://hive:hive@127.0.0.1:5432/hive';
const databaseUrl = process.env.HIVE_JOURNEY_DATABASE_URL
  || schemaDatabaseUrl.replace(/\/\/[^:]+:[^@]+@/, '//app_rls:atomic-harness-app-rls@');
const redisUrl = process.env.REDIS_URL || 'redis://127.0.0.1:6379/15';
const backendPort = Number(process.env.HIVE_JOURNEY_BACKEND_PORT || '8008');
const fakePort = Number(process.env.HIVE_JOURNEY_FAKE_PORT || '8010');
const frontendPort = Number(process.env.HIVE_JOURNEY_FRONTEND_PORT || '3008');
const backendUrl = `http://127.0.0.1:${backendPort}`;
const fakeUrl = `http://127.0.0.1:${fakePort}`;
const frontendUrl = `http://127.0.0.1:${frontendPort}`;
process.env.HIVE_JOURNEY_BACKEND_URL = backendUrl;
process.env.HIVE_JOURNEY_FAKE_URL = fakeUrl;


export default defineConfig({
  testDir: './e2e',
  testMatch: 'atomic-user-journeys.spec.ts',
  timeout: 120_000,
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI
    ? [['github'], ['html', { open: 'never', outputFolder: 'playwright-journey-report' }]]
    : 'list',
  expect: { timeout: 20_000 },
  use: {
    ...devices['Desktop Chrome'],
    baseURL: frontendUrl,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    locale: 'en-US',
    timezoneId: 'UTC',
    reducedMotion: 'reduce',
  },
  webServer: [
    {
      command: `${python} -m tests.journeys.fake_external_provider`,
      cwd: backendRoot,
      url: `${fakeUrl}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
      env: { ...process.env, HIVE_JOURNEY_FAKE_PORT: String(fakePort) },
    },
    {
      command: `${python} -m tests.journeys.run_backend`,
      cwd: backendRoot,
      url: `${backendUrl}/api/health`,
      reuseExistingServer: false,
      timeout: 180_000,
      env: {
        ...process.env,
        DATABASE_URL: databaseUrl,
        SCHEMA_DATABASE_URL: schemaDatabaseUrl,
        REDIS_URL: redisUrl,
        SECRET_KEY: 'atomic-harness-secret',
        JWT_SECRET_KEY: 'atomic-harness-jwt-secret',
        SECRETS_MASTER_KEY: 'atomic-harness-master-secret-32bytes',
        HIVE_CODE_EXEC_PROVIDER: 'local',
        RLS_APP_PASSWORD: 'atomic-harness-app-rls',
        NO_PROXY: '127.0.0.1,localhost',
        no_proxy: '127.0.0.1,localhost',
        SLACK_API_BASE_URL: `${fakeUrl}/slack/api`,
        HIVE_JOURNEY_BACKEND_PORT: String(backendPort),
      },
    },
    {
      command: `npm run dev -- --host 127.0.0.1 --port ${frontendPort}`,
      cwd: frontendRoot,
      url: frontendUrl,
      reuseExistingServer: false,
      timeout: 120_000,
      env: { ...process.env, HIVE_DEV_BACKEND_URL: backendUrl },
    },
  ],
});
