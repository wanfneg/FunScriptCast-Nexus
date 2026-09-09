// 设计稿结构化自查：无头 Chrome + CDP
// 检查：控制台错误 / 水平溢出 / 关键元素尺寸 / 动画与 token 是否生效
const http = require('http');
const { spawn } = require('child_process');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const URL = 'file:///E:/Development/FunScriptCast-Nexus/design/prototype.html';
const PORT = 9333;

function get(path) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port: PORT, path }, res => {
      let d = '';
      res.on('data', c => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { resolve(d); } });
    }).on('error', reject);
  });
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars',
    `--remote-debugging-port=${PORT}`, '--window-size=1440,900',
    '--no-first-run', '--no-default-browser-check', '--user-data-dir=' + process.env.TEMP + '\\cdp-profile',
    URL,
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

  // 极简 CDP 客户端
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => (ws.onopen = r));
  let id = 0;
  const pending = new Map();
  const logs = [];
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error')
      logs.push((m.params.args || []).map(a => a.value || a.description || '').join(' '));
    if (m.method === 'Runtime.exceptionThrown')
      logs.push('EXCEPTION: ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
  };
  const send = (method, params) => new Promise(res => {
    const mid = ++id;
    pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
  });

  await send('Runtime.enable');
  await send('Page.enable');
  await sleep(1200);   // 等首屏动画跑完

  const expr = `(() => {
    const q = s => document.querySelector(s);
    const rect = s => { const e = q(s); if (!e) return null; const r = e.getBoundingClientRect();
      return { w: Math.round(r.width), h: Math.round(r.height), x: Math.round(r.x), y: Math.round(r.y) }; };
    const cs = s => { const e = q(s); return e ? getComputedStyle(e) : null; };
    const navBtn = q('button[data-page="dashboard"]');
    const ind = q('#navInd');
    const rootCs = cs(':root') || cs('html');
    return {
      title: document.title,
      pages: document.querySelectorAll('.page').length,
      activePage: q('.page.active') && q('.page.active').id,
      cards: document.querySelectorAll('.card').length,
      buttons: document.querySelectorAll('.btn').length,
      switches: document.querySelectorAll('.switch').length,
      domNodes: document.getElementsByTagName('*').length,
      docOverflowX: document.documentElement.scrollWidth - window.innerWidth,
      docOverflowY: document.documentElement.scrollHeight - window.innerHeight,
      sidebar: rect('.sidebar'),
      content: rect('.content'),
      titlebar: rect('.titlebar'),
      firstCard: rect('.card.hero'),
      railNodes: document.querySelectorAll('.rail-node').length,
      railOn: document.querySelectorAll('.rail-node.on').length,
      metrics: [...document.querySelectorAll('[data-count]')].map(e => e.textContent),
      ringOffset: q('.ring .fg') && q('.ring .fg').style.strokeDashoffset,
      ringPct: q('.ring .pct') && q('.ring .pct').textContent,
      navIndicator: ind ? { h: Math.round(ind.getBoundingClientRect().height), opacity: getComputedStyle(ind).opacity,
                            transform: getComputedStyle(ind).transform } : null,
      navBtnRect: navBtn ? rect('button[data-page="dashboard"]') : null,
      accent: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(),
      bodyBg: getComputedStyle(document.body).backgroundColor,
      motion: document.documentElement.getAttribute('data-motion') || '(unset)',
      theme: document.documentElement.getAttribute('data-theme') || '(unset)',
    };
  })()`;

  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
  const out = r.result?.result?.value;
  console.log(JSON.stringify({ logs, dom: out }, null, 2));
  ws.close();
  chrome.kill();
})();
