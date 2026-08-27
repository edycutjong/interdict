import { test, expect } from '@playwright/test';

/**
 * `/judge` is the surface built for exactly one reader, and it is the link that goes
 * in the submission entry. A judge page that breaks on submission day is worse than
 * no judge page at all, so its availability is a CI-enforced invariant rather than
 * something anyone remembers to re-check.
 *
 * Every request below carries NO credentials, NO cookies and NO storage state.
 * That is the property under test: this project has no auth today, and this suite is
 * what fails loudly if one is ever introduced in front of the page.
 */

const CLAIM =
  'When Treasury updates the OFAC list, it re-screens the whole payment book';

test.use({ storageState: { cookies: [], origins: [] } });

test.describe('/judge — reachable with no credentials', () => {
  test('returns 200 with no auth, no cookies and no redirect', async ({ request }) => {
    const res = await request.get('/judge/', { maxRedirects: 0 });
    expect(res.status()).toBe(200);
    expect(res.headers()['content-type']).toContain('text/html');
  });

  test('carries the claim sentence verbatim', async ({ page }) => {
    await page.goto('/judge/');
    // The same sentence as the README, the landing page and the Devpost entry. If it
    // drifts in one place it has drifted everywhere, and this is where that surfaces.
    await expect(page.locator('.claim')).toContainText(CLAIM);
  });

  test('sends no cookies and stores nothing', async ({ page, context }) => {
    await page.goto('/judge/');
    expect(await context.cookies()).toHaveLength(0);
    const stored = await page.evaluate(
      () => localStorage.length + sessionStorage.length,
    );
    expect(stored).toBe(0);
  });

  test('the 30-second path is present and every step links somewhere real', async ({
    page,
  }) => {
    await page.goto('/judge/');
    const steps = page.locator('.path li');
    await expect(steps).toHaveCount(4);

    for (const href of await page.locator('.path li a.h').evaluateAll((as) =>
      as.map((a) => (a as HTMLAnchorElement).getAttribute('href') ?? ''),
    )) {
      expect(href).not.toBe('');
      expect(href).not.toBe('#');
    }
  });

  test('the receipts a judge is asked to check are actually on the page', async ({
    page,
  }) => {
    await page.goto('/judge/');
    const body = page.locator('body');
    // Each of these is printed by a command in DEMO.md. If a number is edited in one
    // surface and not the others, the claim stops being reproducible — fail here.
    for (const receipt of ['0.995', '0.840', '19,199', '353', '1,181,434.51']) {
      await expect(body).toContainText(receipt);
    }
  });

  test('states the limitations rather than only the wins', async ({ page }) => {
    await page.goto('/judge/');
    await expect(page.locator('#limits')).toBeVisible();
    await expect(page.locator('#limits')).toContainText('never issued a CLEAR');
  });

  test('no section is left blank by the reveal animation', async ({ page }) => {
    // Content must never be trapped invisible by the decoration that reveals it.
    // A judge arriving by deep link lands mid-document before the observer has seen
    // anything, so the page reveals everything on a hash arrival.
    await page.goto('/judge/#receipts');
    await page.waitForTimeout(1200);
    const hidden = await page.evaluate(
      () =>
        [...document.querySelectorAll('.reveal')].filter(
          (el) => parseFloat(getComputedStyle(el).opacity) < 0.5,
        ).length,
    );
    expect(hidden).toBe(0);
  });
});

test.describe('the surfaces /judge sends a judge to', () => {
  for (const path of ['/', '/judge/', '/pitch-deck.html']) {
    test(`${path} returns 200 unauthenticated`, async ({ request }) => {
      expect((await request.get(path)).status()).toBe(200);
    });
  }

  test('the landing page offers a visible route to /judge on every viewport', async ({
    page,
  }) => {
    await page.goto('/');
    const links = page.locator('a[href="/judge/"]');
    expect(await links.count()).toBeGreaterThan(0);
    // At least one must actually be rendered — a link hidden by a mobile breakpoint
    // is not a route.
    const visible = await links.evaluateAll(
      (as) => as.filter((a) => a.getBoundingClientRect().width > 0).length,
    );
    expect(visible).toBeGreaterThan(0);
  });
});
