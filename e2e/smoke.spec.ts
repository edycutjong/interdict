import { test, expect } from '@playwright/test';

/**
 * The published site must stand up with NO environment at all — no API key, no
 * database, no Firestore credentials. It is three static files; this suite is what
 * fails if that ever stops being true, or if a link on them rots.
 *
 * Deliberately narrow. The product's own behaviour is covered by the 323 Python
 * tests; this is only about the judge-facing surfaces being reachable and honest.
 */

test.describe('loads with no credentials of any kind', () => {
  test('the landing page renders its headline claim', async ({ page }) => {
    await page.goto('/');
    await expect(page.locator('h1')).toBeVisible();
    await expect(page.locator('body')).toContainText('OFAC');
  });

  test('no page requests an external origin', async ({ page }) => {
    // The site embeds no CDN scripts, fonts or trackers. A new external request is
    // a supply-chain surface and a privacy claim change, so it fails here first.
    const external: string[] = [];
    page.on('request', (r) => {
      const u = new URL(r.url());
      if (u.hostname !== '127.0.0.1' && u.hostname !== 'localhost') external.push(r.url());
    });
    for (const p of ['/', '/judge/', '/pitch-deck.html']) {
      await page.goto(p);
      await page.waitForLoadState('networkidle');
    }
    expect(external, `external requests: ${external.join(', ')}`).toHaveLength(0);
  });
});

test.describe('no dead links', () => {
  test('every in-page anchor resolves to a real element', async ({ page }) => {
    for (const path of ['/', '/judge/']) {
      await page.goto(path);
      const broken = await page.evaluate(() => {
        const ids = new Set([...document.querySelectorAll('[id]')].map((e) => e.id));
        return [...document.querySelectorAll('a[href^="#"]')]
          .map((a) => (a as HTMLAnchorElement).getAttribute('href')!)
          .filter((h) => h.length > 1 && !ids.has(h.slice(1)));
      });
      expect(broken, `${path} broken anchors: ${broken.join(', ')}`).toHaveLength(0);
    }
  });

  test('every internal link returns 200', async ({ page, request }) => {
    for (const path of ['/', '/judge/']) {
      await page.goto(path);
      const hrefs = await page.evaluate(() =>
        [...document.querySelectorAll('a[href]')]
          .map((a) => (a as HTMLAnchorElement).getAttribute('href')!)
          .filter((h) => !/^(https?:|mailto:|#)/.test(h)),
      );
      for (const href of [...new Set(hrefs)]) {
        const url = new URL(href, `http://127.0.0.1:4173${path}`);
        expect(
          (await request.get(url.pathname)).status(),
          `${path} -> ${href}`,
        ).toBe(200);
      }
    }
  });

  test('no placeholder hrefs shipped', async ({ page }) => {
    for (const path of ['/', '/judge/', '/pitch-deck.html']) {
      await page.goto(path);
      const placeholders = await page.evaluate(
        () =>
          [...document.querySelectorAll('a[href]')]
            .map((a) => (a as HTMLAnchorElement).getAttribute('href')!)
            .filter((h) => h === '#' || h === '' || h.toLowerCase().includes('todo')),
      );
      expect(placeholders, `${path}: ${placeholders.join(', ')}`).toHaveLength(0);
    }
  });
});

test.describe('social card metadata', () => {
  for (const path of ['/', '/judge/']) {
    test(`${path} carries a complete Open Graph card`, async ({ page }) => {
      await page.goto(path);
      for (const prop of [
        'og:title',
        'og:description',
        'og:image',
        'og:url',
        'og:type',
      ]) {
        const c = await page
          .locator(`meta[property="${prop}"]`)
          .getAttribute('content');
        expect(c, `${path} missing ${prop}`).toBeTruthy();
      }
      // Platforms resample anything that is not exactly 1200x630, with no say from you.
      expect(
        await page.locator('meta[property="og:image:width"]').getAttribute('content'),
      ).toBe('1200');
      expect(
        await page.locator('meta[property="og:image:height"]').getAttribute('content'),
      ).toBe('630');
    });
  }
});

test.describe('the 404 page', () => {
  // GitHub Pages serves site/404.html for any unmatched path on the custom domain.
  // Without it a mistyped /judge lands on GitHub's generic 404, which links nowhere
  // back — and the one reader this site exists for would have no route onward.
  test('is a real page and routes a lost judge onward', async ({ page }) => {
    await page.goto('/404.html');
    await expect(page.locator('h1')).toBeVisible();
    const body = await page.evaluate(() => document.body.innerText.length);
    expect(body).toBeGreaterThan(50);

    // The judge page must be the first and most prominent way out.
    const first = page.locator('ul li a').first();
    await expect(first).toHaveAttribute('href', '/judge/');
    for (const href of ['/judge/', '/', '/pitch-deck.html']) {
      await expect(page.locator(`a[href="${href}"]`)).toHaveCount(1);
    }
  });

  test('is excluded from search indexes', async ({ page }) => {
    await page.goto('/404.html');
    const robots = await page.locator('meta[name="robots"]').getAttribute('content');
    expect(robots).toContain('noindex');
  });

  test('every link on it resolves', async ({ page, request }) => {
    await page.goto('/404.html');
    const hrefs = await page.evaluate(() =>
      [...document.querySelectorAll('a[href]')]
        .map((a) => (a as HTMLAnchorElement).getAttribute('href')!)
        .filter((h) => !/^(https?:|mailto:|#)/.test(h)));
    for (const href of hrefs) {
      expect((await request.get(href)).status(), href).toBe(200);
    }
  });
});
