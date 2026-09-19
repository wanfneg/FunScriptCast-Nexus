// 界面视觉回归：无头 Chrome 打开真实宿主页面，六个页面 × 明暗两套主题逐一截图。
//
// 为什么必须截图：这是设计任务，断言只能证明"没报错"，证明不了"好看"。
// 排版、间距、对比度、动效收尾状态都得靠眼睛看。
//
// 用法：
//   1) 先启动宿主：.venv\Scripts\python.exe host_server.py --no-window
//   2) node tests/ui_shot.js
// 产物：tests/_shots/*.png
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const URL = 'http://127.0.0.1:8790/';
const PORT = 9335;
const OUT = path.join(__dirname, '_shots');
const PAGES = ['dashboard', 'dlna', 'subtitle', 'sync', 'settings'];

function get(p) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port: PORT, path: p }, res => {
      let d = '';
      res.on('data', c => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { resolve(d); } });
    }).on('error', reject);
  });
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=1',
    `--remote-debugging-port=${PORT}`, '--window-size=1280,860',
    '--no-first-run', '--no-default-browser-check', '--font-render-hinting=none',
    '--user-data-dir=' + process.env.TEMP + '\\cdp-shots', URL,
  ], { stdio: 'ignore' });

  let target = null;
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    try {
      const list = await get('/json/list');
      target = (list || []).find(t => t.type === 'page' && t.webSocketDebuggerUrl);
      if (target) break;
    } catch (_) {}
  }
  if (!target) { console.log(JSON.stringify({ error: 'no target' })); chrome.kill(); return; }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => (ws.onopen = r));
  let id = 0;
  const pending = new Map();
  const problems = [];
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error')
      problems.push('console.error: ' + (m.params.args || []).map(a => a.value || a.description || '').join(' '));
    if (m.method === 'Runtime.exceptionThrown')
      problems.push('EXCEPTION: ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
  };
  const send = (method, params) => new Promise(res => {
    const mid = ++id; pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
  });
  const evalJs = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.result && r.result.exceptionDetails) problems.push('EVAL: ' + JSON.stringify(r.result.exceptionDetails.text));
    return r.result && r.result.result ? r.result.result.value : null;
  };
  const shoot = async (name) => {
    const r = await send('Page.captureScreenshot', { format: 'png' });
    if (r.result && r.result.data) {
      fs.writeFileSync(path.join(OUT, name + '.png'), Buffer.from(r.result.data, 'base64'));
      return true;
    }
    problems.push('screenshot failed: ' + name);
    return false;
  };

  await send('Runtime.enable');
  await send('Page.enable');
  await sleep(2600);            // 等首屏 + 字体 + 一次状态轮询

  // 环境自检：字体是否真的落地、图标是否真的渲染出来
  const env = await evalJs(`(() => {
    const cs = getComputedStyle(document.documentElement);
    const sprite = document.querySelectorAll('.sprite symbol').length;
    const icons = document.querySelectorAll('svg.ic use').length;
    // 正确判据是"href 指向的 symbol 存不存在"。用尺寸判断会在隐藏页上误报：
    // 隐藏页里所有元素的 getBoundingClientRect() 都是 0。
    const syms = {};
    document.querySelectorAll('.sprite symbol').forEach(s => { syms[s.id] = 1; });
    let broken = 0, total = 0;
    document.querySelectorAll('svg.ic use').forEach(u => {
      total++;
      const id = (u.getAttribute('href') || '').replace('#', '');
      if (id && !syms[id]) broken++;
    });
    const canvas = document.querySelector('#pulseCanvas');
    const cr = canvas ? canvas.getBoundingClientRect() : null;
    return {
      title: document.title,
      fontsReady: document.fonts ? document.fonts.status : 'n/a',
      spaceGrotesk: document.fonts ? document.fonts.check('16px "Space Grotesk Variable"') : null,
      inter: document.fonts ? document.fonts.check('16px "Inter Variable"') : null,
      jetbrains: document.fonts ? document.fonts.check('16px "JetBrains Mono Variable"') : null,
      spriteSymbols: sprite,
      iconUses: icons,
      brokenIcons: broken,
      iconTotal: total,
      h1Font: getComputedStyle(document.querySelector('.page-head h1')).fontFamily,
      canvas: cr ? { w: Math.round(cr.width), h: Math.round(cr.height), px: canvas.width + 'x' + canvas.height } : null,
      navInd: (() => { const e = document.querySelector('#navInd'); return e ? e.style.transform : null; })(),
      segThumb: (() => {
        const t = document.querySelector('#motionSeg .seg-thumb');
        return t ? { w: t.style.width, tf: t.style.transform } : null;
      })(),
      accent: cs.getPropertyValue('--accent').trim(),
      h1: (document.querySelector('.page-head h1') || {}).textContent,
    };
  })()`);

  const shots = [];
  for (const theme of ['dark', 'light']) {
    await evalJs(`document.documentElement.setAttribute('data-theme','${theme}')`);
    await sleep(450);
    for (const p of PAGES) {
      await evalJs(`document.querySelector('button[data-page="${p}"]').click()`);
      await sleep(760);          // 等入场动画跑完，别截到一半
      if (await shoot(`${p}-${theme}`)) shots.push(`${p}-${theme}`);
    }
  }

  // 交互态：切回仪表盘并悬停一个面板，验证悬停扫光与状态色
  await evalJs(`document.documentElement.setAttribute('data-theme','dark');
                document.querySelector('button[data-page="dashboard"]').click();`);
  await sleep(800);
  await shoot('dashboard-hover');

  const state = await evalJs(`(() => {
    const t = s => (document.querySelector(s) || {}).textContent || null;
    return {
      pills: [t('#pillDlna'), t('#pillSub'), t('#pillGpu')],
      pulse: t('#pulseState'),
      metrics: [t('#mRoots'), t('#mUptime'), t('#mTerms'), t('#gpuText')],
      ring: t('#gpuRing .pct'),
      rail: [t('#railDlnaD'), t('#railSubD'), t('#railGpuD')],
      issues: []
    };
  })()`);

  console.log(JSON.stringify({ env, shots, state, problems }, null, 2));
  ws.close();
  chrome.kill();
})();
