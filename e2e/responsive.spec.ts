import { test, expect, devices } from '@playwright/test';

/**
 * Layout gates for the three published pages, at the three widths a judge actually
 * uses. The single most common way one of these breaks is a wide element — a table,
 * a code block, a diagram — escaping its container and giving the whole document a
 * horizontal scrollbar, which on a phone renders the page unreadable rather than
 * merely ugly.
 *
 * Wide content is allowed to scroll INSIDE its own container. The body is not.
 */

const PAGES = ['/', '/judge/', '/pitch-deck.html'];

const VIEWPORTS = [
  { label: 'mobile', width: 390, height: 844 },
  { label: 'tablet', width: 834, height: 1112 },
  { label: 'desktop', width: 1440, height: 900 },
];

for (const { label, width, height } of VIEWPORTS) {
  test.describe(`${label} (${width}x${height})`, () => {
    test.use({ viewport: { width, height } });

    for (const path of PAGES) {
      test(`${path} does not scroll horizontally`, async ({ page }) => {
        await page.goto(path);
        // Reveal-on-scroll content is measured after a settle, so an element that is
        // only laid out once visible is still checked.
        await page.evaluate(async () => {
          for (let y = 0; y < document.body.scrollHeight; y += 600) {
            window.scrollTo(0, y);
            await new Promise((r) => setTimeout(r, 30));
          }
          window.scrollTo(0, 0);
        });

        const overflow = await page.evaluate(() => {
          const de = document.documentElement;
          const offenders: string[] = [];
          document.querySelectorAll('*').forEach((el) => {
            const r = el.getBoundingClientRect();
            if (r.width > 0 && r.right > de.clientWidth + 2) {
              offenders.push(
                el.tagName.toLowerCase() +
                  (el.className ? '.' + String(el.className).split(' ')[0] : ''),
              );
            }
          });
          return {
            scrolls: de.scrollWidth > de.clientWidth + 2,
            offenders: [...new Set(offenders)].slice(0, 6),
          };
        });

        expect(
          overflow.scrolls,
          `horizontal scroll from: ${overflow.offenders.join(', ')}`,
        ).toBe(false);
      });

      test(`${path} loads every image it references`, async ({ page }) => {
        await page.goto(path);
        await page.evaluate(async () => {
          for (let y = 0; y < document.body.scrollHeight; y += 600) {
            window.scrollTo(0, y);
            await new Promise((r) => setTimeout(r, 30));
          }
        });
        await page.waitForLoadState('networkidle');
        const broken = await page.evaluate(() =>
          [...document.images]
            .filter((i) => !i.complete || i.naturalWidth === 0)
            .map((i) => i.getAttribute('src')),
        );
        expect(broken, `broken images: ${broken.join(', ')}`).toHaveLength(0);
      });
    }
  });
}

test.describe('accessibility floors that do not need an audit tool', () => {
  for (const path of PAGES) {
    test(`${path} has a title, a lang, and alt text on every image`, async ({
      page,
    }) => {
      await page.goto(path);
      expect((await page.title()).length).toBeGreaterThan(10);
      expect(await page.locator('html').getAttribute('lang')).toBeTruthy();

      const missingAlt = await page.evaluate(
        () => [...document.images].filter((i) => i.getAttribute('alt') === null).length,
      );
      expect(missingAlt).toBe(0);
    });

    test(`${path} logs no console errors`, async ({ page }) => {
      const errors: string[] = [];
      page.on('console', (m) => m.type() === 'error' && errors.push(m.text()));
      page.on('pageerror', (e) => errors.push(`PAGEERROR: ${e.message}`));
      await page.goto(path);
      await page.waitForLoadState('networkidle');
      expect(errors, errors.join('; ')).toHaveLength(0);
    });
  }
});

test.describe('dark mode', () => {
  test.use({ colorScheme: 'dark' });
  for (const path of PAGES) {
    test(`${path} renders in dark mode without overflow`, async ({ page }) => {
      await page.goto(path);
      const scrolls = await page.evaluate(
        () =>
          document.documentElement.scrollWidth >
          document.documentElement.clientWidth + 2,
      );
      expect(scrolls).toBe(false);
    });
  }
});
