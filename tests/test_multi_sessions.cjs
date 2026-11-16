// Real local server, real independent preview workers. Only discovery uses fake devices.
const {chromium} = require('playwright');
const {spawn} = require('node:child_process');
const path = require('node:path'), fs = require('node:fs'), assert = require('node:assert/strict');

(async () => {
  const root = path.resolve(__dirname, '..');
  const launchServer = (port = 0) => spawn(process.env.PYTHON || 'python', ['-u', '-m', 'route_studio', '--no-browser', '--port', String(port)], {
    cwd: root, windowsHide: true, env: {...process.env, PYTHONUTF8: '1', LOCALAPPDATA: path.join(root, 'build/multi-session-data')},
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let browser, serverErrors = '', server = launchServer();
  const ready = () => {
    server.stderr.on('data', chunk => {serverErrors = (serverErrors + chunk).slice(-4000);});
    return new Promise((resolve, reject) => {
      let output = '';
      const timer = setTimeout(() => reject(new Error('Server did not start: ' + serverErrors)), 10000);
      server.stdout.on('data', chunk => {
        output += chunk;
        const match = output.match(/http:\/\/127\.0\.0\.1:\d+\/\?token=[A-Za-z0-9_-]+/);
        if (match) {clearTimeout(timer); resolve(match[0]);}
      });
      server.once('error', reject);
    });
  };
  try {
    const url = await ready();
    browser = await chromium.launch({headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome'});
    const page = await browser.newPage({viewport: {width: 1440, height: 1080}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('https://*.is.autonavi.com/**', route => route.abort());
    await page.route('**/api/devices', route => route.fulfill({json: {
      android: [{serial: 'test-phone-a', state: 'device'}, {serial: 'test-phone-b', state: 'device'}],
      ios: [], dependencies: {adb: true, androidDirect: true, ios: true}, errors: [],
    }}));
    await page.goto(url);
    await page.waitForFunction(() => !document.querySelector('#session-add').disabled);
    const a = 'default';
    await page.fill('#session-name', '设备 A'); await page.click('#session-rename');
    await page.waitForFunction(() => document.querySelector('.session-card strong').textContent === '设备 A');
    await page.selectOption('#mode', 'android');
    await page.waitForFunction(() => document.querySelector('#android-device').options.length === 3);
    await page.selectOption('#android-device', 'test-phone-a');
    await page.fill('#speed', '2.8'); await page.selectOption('#loop-mode', 'infinite');
    await page.click('#save-button');
    await page.click('#session-add');
    await page.waitForFunction(() => document.querySelectorAll('.session-card').length === 2 && !document.querySelector('#session-add').disabled);
    const b = await page.locator('.session-card[aria-pressed="true"]').getAttribute('data-session-id');
    assert.equal(await page.inputValue('#mode'), 'preview');
    assert.equal(await page.inputValue('#speed'), '2.5');
    await page.click('#load-button');
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('本会话尚未保存'));
    await page.fill('#session-name', '设备 B'); await page.click('#session-rename');
    await page.selectOption('#mode', 'android');
    await page.waitForFunction(() => !document.querySelector('#android-device').disabled);
    await page.selectOption('#android-device', 'test-phone-b');
    await page.fill('#speed', '4.1'); await page.selectOption('#loop-mode', 'infinite');
    await page.click('#clear-button');
    for (const [lat, lon] of [[31, 121], [31.001, 121]]) {
      await page.fill('#latitude', String(lat)); await page.fill('#longitude', String(lon));
      await page.click('.add-button');
    }
    const switchTo = async id => {
      await page.locator(`.session-card[data-session-id="${id}"]`).click();
      await page.waitForFunction(id => document.querySelector('.session-card[aria-pressed="true"]')?.dataset.sessionId === id && !document.querySelector('#session-add').disabled, id);
    };
    const state = id => page.evaluate(async id => (await (await fetch('/api/status?sessionId=' + id)).json()).run, id);
    await switchTo(a);
    assert.equal(await page.inputValue('#android-device'), 'test-phone-a');
    assert.equal(await page.inputValue('#speed'), '2.8');
    assert.equal(await page.locator('#waypoints .waypoint').count(), 14);
    await page.selectOption('#mode', 'preview'); await page.click('#start-button');
    await page.waitForFunction(() => document.querySelector('#run-state').dataset.state === 'running');
    await switchTo(b);
    assert.equal(await page.inputValue('#android-device'), 'test-phone-b');
    assert.equal(await page.inputValue('#speed'), '4.1');
    assert.equal(await page.locator('#waypoints .waypoint').count(), 2);
    await page.selectOption('#mode', 'preview'); await page.click('#start-button');
    await page.waitForFunction(() => document.querySelector('#run-state').dataset.state === 'running');
    assert.equal((await state(a)).state, 'running'); assert.equal((await state(b)).state, 'running');
    assert.notEqual((await state(a)).runId, (await state(b)).runId);
    await switchTo(a); await page.click('#pause-button');
    await page.waitForFunction(() => document.querySelector('#run-state').dataset.state === 'paused');
    assert.equal((await state(b)).state, 'running');
    await page.click('#stop-button');
    await page.waitForFunction(() => document.querySelector('#run-state').dataset.state === 'idle');
    const date = new Date(Date.now() + 120000);
    const local = new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
    await page.fill('#schedule-time', local); await page.click('#schedule-button');
    await page.waitForFunction(() => document.querySelector('#run-state').dataset.state === 'scheduled');
    assert.equal(await page.isDisabled('#speed'), true);
    assert.equal((await state(b)).state, 'running');
    await page.screenshot({path: path.join(root, 'build/multi-device-desktop.png'), fullPage: true});
    await page.reload();
    await page.waitForFunction(() => document.querySelector('#run-state').dataset.state === 'scheduled' && !document.querySelector('#session-add').disabled);
    await page.click('#stop-button');
    await page.waitForFunction(() => document.querySelector('#run-state').dataset.state === 'idle');
    assert.equal((await state(b)).state, 'running');
    await switchTo(b);
    await page.waitForFunction(() => Number(document.querySelector('#trace-status').dataset.points) > 0);
    assert.match(await page.textContent('#current-position'), /^31\./);
    assert.equal(await page.isDisabled('#session-remove'), true);
    await page.setViewportSize({width: 390, height: 980});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: path.join(root, 'build/multi-device-mobile.png')});
    await page.click('#stop-button');
    await page.waitForFunction(() => document.querySelector('#run-state').dataset.state === 'idle');
    await page.click('#session-remove');
    await page.waitForFunction(() => document.querySelectorAll('.session-card').length === 1);
    assert.equal(await page.inputValue('#speed'), '2.8');
    await page.click('#session-add');
    await page.waitForFunction(() => document.querySelectorAll('.session-card').length === 2 && !document.querySelector('#session-add').disabled);
    await page.fill('#session-name', '设备 C'); await page.click('#session-rename');
    await page.fill('#speed', '5.2'); await page.click('#save-button');
    await page.fill('#schedule-time', local); await page.click('#schedule-button');
    await page.waitForFunction(() => document.querySelector('#run-state').dataset.state === 'scheduled');
    await switchTo(a);
    assert.deepEqual(errors, []);
    const exited = new Promise(resolve => server.once('exit', resolve));
    await page.evaluate(async () => { await fetch('/api/quit', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}); });
    await exited;
    server = launchServer(new URL(url).port);
    const restarted = await ready();
    await page.goto(restarted);
    await page.waitForFunction(() => document.querySelectorAll('.session-card').length === 2 && !document.querySelector('#session-add').disabled);
    assert.equal(await page.inputValue('#session-name'), '设备 A');
    assert.match(await page.textContent('#session-summary'), /0 个预约/);
    const c = await page.locator('.session-card').filter({hasText: '设备 C'}).getAttribute('data-session-id');
    await switchTo(c);
    assert.equal(await page.inputValue('#speed'), '5.2');
    assert.equal(await page.getAttribute('#run-state', 'data-state'), 'idle');
    await page.fill('#speed', '6'); await page.click('#load-button');
    assert.equal(await page.inputValue('#speed'), '5.2');
    assert.deepEqual(errors, []);
    await page.evaluate(async () => { await fetch('/api/quit', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: '{}'}); });
    console.log('PASS: isolated device drafts/saved routes, concurrent real workers, pause/stop, schedule/cancel, reload, scoped traces, mobile layout, process restart restores plans without restarting tasks. No device location changed.');
  } finally {
    if (browser) await browser.close();
    if (server.exitCode === null) server.kill();
  }
})().catch(error => {console.error(error); process.exit(1);});
