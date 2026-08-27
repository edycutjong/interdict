import { defineConfig, devices } from '@playwright/test';

/**
 * E2E for the published surfaces in `site/` — the landing page, the pitch deck, and
 * `/judge/`, the one page written for a single reader.
 *
 * The server is Python's stdlib rather than a Node static server: this is a Python
 * project, `python3` is already a hard requirement, and one fewer dependency in the
 * lockfile is one fewer thing to audit before a public flip. It also resolves
 * `/judge/` to `/judge/index.html` the same way GitHub Pages does, so a link that
 * passes here is a link that works in production — testing against `file://` would
 * pass root-absolute hrefs that are dead once deployed.
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: process.env.CI ? [['github'], ['html', { open: 'never' }]] : 'html',

  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  projects: [
    { name: 'chromium', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile',   use: { ...devices['Pixel 7'] } },
  ],

  webServer: {
    command: 'python3 -m http.server 4173 --bind 127.0.0.1 --directory site',
    url: 'http://127.0.0.1:4173/',
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
  },
});
