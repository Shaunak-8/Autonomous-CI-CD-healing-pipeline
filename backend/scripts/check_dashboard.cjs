const path = require('node:path');
const fs = require('node:fs');
const { chromium } = require(path.resolve(process.argv[2]));

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1276, height: 900 } });
    const errors = [];
    page.on('pageerror', (error) => errors.push(error.message));
    const user = { id: 'layout-fixture', email: 'layout@example.invalid', aud: 'authenticated', app_metadata: {}, user_metadata: {} };
    const token = [Buffer.from('{"alg":"HS256","typ":"JWT"}').toString('base64url'),
      Buffer.from(JSON.stringify({ sub: user.id, exp: Math.floor(Date.now() / 1000) + 3600 })).toString('base64url'), 'fixture-signature'].join('.');
    await page.addInitScript(({ user, token }) => {
      localStorage.setItem('sb-smudwksuuxhbvohoryoj-auth-token', JSON.stringify({
        access_token: token, refresh_token: 'example-refresh', expires_at: Math.floor(Date.now() / 1000) + 3600,
        expires_in: 3600, token_type: 'bearer', user,
      }));
    }, { user, token });
    await page.route('**/api/**', (route) => {
      const project = { id: 'layout-fixture', name: 'Layout fixture', repo_url: 'https://github.com/example/repository' };
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(
        route.request().url().endsWith('/projects') ? { projects: [project] } : project) });
    });
    await page.route('https://*.supabase.co/**', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(user) }));
    await page.goto('http://127.0.0.1:5173/app/project/layout-fixture');
    await page.waitForSelector('main.run-dashboard');
    await page.evaluate(async () => {
      const { useAgentStore } = await import('/src/store/agentStore.js');
      useAgentStore.setState({
        whitebox: { coverage: null, generated_tests: [], reason: 'Static analysis; coverage was not measured.',
          uncovered_paths: Array.from({ length: 6 }, (_, index) => ({
            file: 'backend/app/adaptive/simulation/very_long_controller_filename.py', function: 'evaluate_and_adapt',
            line: 100 + index, type: 'STATIC_BRANCH', condition: 'self.steps_since_switch >= self.cooldown_steps', coverage_status: 'UNMEASURED',
          })) }, score: { base: 0, final: 0, total: 0, penalty: 0 },
      });
    });
    await page.waitForSelector('[aria-label="White-box findings"]');
    await page.locator('[aria-label="White-box findings"]').scrollIntoViewIfNeeded();
    const layout = await page.locator('[aria-label="White-box findings"]').evaluate((element) => ({
      width: element.clientWidth, contentWidth: element.scrollWidth,
      viewport: window.innerWidth, right: element.getBoundingClientRect().right,
    }));
    if (layout.right > layout.viewport || layout.width > layout.viewport || errors.length) {
      throw new Error(JSON.stringify({ layout, errors }));
    }
    const output = path.resolve(__dirname, '../results/ui-validation/whitebox-layout.png');
    fs.mkdirSync(path.dirname(output), { recursive: true });
    await page.screenshot({ path: output });
    console.log(JSON.stringify({ layout, browserErrors: errors, screenshot: output }));
  } finally {
    await browser.close();
  }
})().catch((error) => { console.error(error.message); process.exitCode = 1; });
