const { chromium } = require('playwright');
const fs = require('fs'), path = require('path');
// usage: node render.js <outdir> <fps> [t1,t2,...]  (list => stills only)
const OUT = process.argv[2], FPS = +process.argv[3] || 60, LIST = process.argv[4];
fs.mkdirSync(OUT, { recursive: true });
(async () => {
  const browser = await chromium.launch({ args: ['--allow-file-access-from-files'] });
  const url = 'file://' + path.resolve(__dirname, 'index.html');
  let jobs;
  if (LIST) jobs = LIST.split(',').map((t, i) => ({ t: +t, name: `s_${String(t).replace('.', '_')}.png` }));
  else { const N = Math.round(FPS * 15); jobs = [...Array(N)].map((_, f) => ({ t: f / FPS, name: `f${String(f).padStart(4, '0')}.jpg` })); }
  const W = LIST ? 1 : 4, chunks = [...Array(W)].map((_, w) => jobs.filter((_, i) => i % W === w));
  const t0 = Date.now();
  page0err = [];
  await Promise.all(chunks.map(async (chunk) => {
    const page = await browser.newPage({ viewport: { width: 1920, height: 1080 }, deviceScaleFactor: 1 });
    page.on('pageerror', e => console.error('PAGEERR', e.message));
    page.on('console', m => { if (m.type() === 'error') console.error('CONSOLE', m.text()); });
    await page.goto(url);
    await page.waitForFunction(() => window.READY === true, null, { timeout: 120000 });
    for (const j of chunk) {
      await page.evaluate(t => window.seek(t), j.t);
      const buf = await page.screenshot(j.name.endsWith('png') ? { type: 'png' } : { type: 'jpeg', quality: 94 });
      fs.writeFileSync(path.join(OUT, j.name), buf);
    }
    await page.close();
  }));
  await browser.close();
  console.log('done', jobs.length, 'frames in', ((Date.now() - t0) / 1000).toFixed(1), 's');
})();
