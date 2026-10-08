// Run with NODE_PATH pointing to an installation of playwright.
// CHROME_PATH can select an installed Chrome instead of Playwright Chromium.
const { chromium } = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

(async () => {
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROME_PATH ? { executablePath: process.env.CHROME_PATH } : {}),
  });
  try {
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    const source = fs.readFileSync(path.join(__dirname, '../dashboard/service_board/index.html'), 'utf8');
    await page.setContent('<iframe title="board" style="width:100%;border:0" height="0"></iframe>');
    await page.evaluate(source => {
      window.heights = [];
      window.addEventListener('message', event => {
        if (event.data.type === 'streamlit:setFrameHeight') {
          window.heights.push(event.data.height);
          document.querySelector('iframe').height = event.data.height;
        }
      });
      document.querySelector('iframe').srcdoc = source;
    }, source);
    const frame = await page.locator('iframe').elementHandle().then(handle => handle.contentFrame());
    await frame.waitForSelector('#board', { state: 'attached' });
    const args = {
      services: [{ id: 'a', name: 'Alpha' }, { id: 'b', name: 'Beta' }],
      layout: { version: 1, items: [{ type: 'group', id: 'g', name: 'Lab', service_ids: ['a', 'b'] }] },
    };
    const send = () => page.evaluate(args => document.querySelector('iframe').contentWindow.postMessage({ type: 'streamlit:render', args }, '*'), args);
    await send();
    await frame.locator('.folder').waitFor();
    await page.waitForFunction(() => document.querySelector('iframe').clientHeight > 350);
    await page.waitForTimeout(300);
    const firstHeight = await page.locator('iframe').evaluate(el => el.clientHeight);
    await page.waitForTimeout(500);
    assert.equal(await page.locator('iframe').evaluate(el => el.clientHeight), firstHeight, 'Frame must not grow in a resize feedback loop');
    assert.ok(firstHeight >= 355 && firstHeight < 600);
    const messagesBefore = await page.evaluate(() => heights.length);
    await send();
    await page.waitForFunction(count => heights.length > count, messagesBefore);
    assert.equal(await page.locator('iframe').evaluate(el => el.clientHeight), firstHeight);
    await page.locator('iframe').evaluate(el => { el.height = 0; });
    await send();
    await page.waitForFunction(() => document.querySelector('iframe').clientHeight > 350);
    await frame.locator('.folder').click();
    await frame.locator('.overlay').waitFor();
    assert.equal(await frame.locator('.group-service').count(), 2);
    await frame.getByRole('button', { name: 'Close', exact: true }).click();
    await page.waitForTimeout(200);
    assert.equal(await page.locator('iframe').evaluate(el => el.clientHeight), firstHeight);
    await page.setViewportSize({ width: 390, height: 844 });
    await frame.locator('.folder').click();
    await page.waitForTimeout(200);
    assert.ok(await page.locator('iframe').evaluate(el => el.clientHeight) > 700, 'Mobile group must fit both cards');
    await frame.getByRole('button', { name: 'Close', exact: true }).click();
    await page.waitForTimeout(200);
    assert.ok(await page.locator('iframe').evaluate(el => el.clientHeight) < 600, 'Frame must shrink after closing the mobile group');
    assert.deepEqual(errors, []);
    console.log('PASS: stable frame height, repeated render, collapsed-frame recovery, desktop and mobile groups');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
