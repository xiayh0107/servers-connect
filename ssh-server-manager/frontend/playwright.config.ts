import {defineConfig, devices} from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: 0,
  workers: process.env.CI ? 1 : 2,
  timeout: 90_000,
  expect: {timeout: 10_000},
  reporter: [['list'], ['html', {open: 'never'}]],
  use: {
    baseURL: 'http://127.0.0.1:4173',
    viewport: {width: 1440, height: 900},
    trace: 'retain-on-failure', screenshot: 'only-on-failure',
  },
  projects: [
    {name: 'smoke', testMatch: 'smoke.spec.ts', use: {...devices['Desktop Chrome'], viewport: {width: 1440, height: 900}}},
    {name: 'ssh', testMatch: 'ssh.spec.ts', use: {...devices['Desktop Chrome'], viewport: {width: 1440, height: 900}, trace: 'off', screenshot: 'off', video: 'off'}},
  ],
  webServer: {
    command: 'npm exec vite -- --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173/assets/',
    reuseExistingServer: false,
    timeout: 30_000,
  },
});
