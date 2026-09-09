// 前端集成自查：无头 Chrome 打开真实宿主页面，验证数据绑定与控制台错误
const http = require('http');
const { spawn } = require('child_process');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const URL = 'http://127.0.0.1:8790/';
const PORT = 9334;

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
    `--remote-debugging-port=${PORT}`, '--window-size=1280,860',
    '--no-first-run', '--no-default-browser-check',
    '--user-data-dir=' + process.env.TEMP + '\\cdp-profile2', URL,
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
    const mid = ++id; pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
  });

  await send('Runtime.enable');
  await send('Page.enable');
  await sleep(2500);   // 等首屏 + 一次轮询

  const expr = `(() => {
    const t = s => (document.querySelector(s) || {}).textContent || null;
    const cls = s => { const e = document.querySelector(s); return e ? e.className : null; };
    return {
      title: document.title,
      pills: { dlna: t('#pillDlna'), sub: t('#pillSub'), gpu: t('#pillGpu') },
      rail: { dlna: t('#railDlnaD'), dlnaCls: cls('#railDlna'), sub: t('#railSubD'), gpu: t('#railGpuD'), gpuU: t('#railGpuU') },
      metrics: { roots: t('#mRoots'), uptime: t('#mUptime'), terms: t('#mTerms'), gpuText: t('#gpuText') },
      ringPct: t('#gpuRing .pct'),
      ringOffset: (document.querySelector('#gpuRing .fg') || {}).style ? document.querySelector('#gpuRing .fg').style.strokeDashoffset : null,
      dlnaPage: { badge: t('#dlnaBadge'), url: (document.querySelector('#dlnaUrl')||{}).value, roots: document.querySelectorAll('#rootList .row').length },
      subPage: { badge: t('#subBadge'), model: t('#subModel'), device: t('#subDevice') },
      timeline: document.querySelectorAll('#timeline .tl-item').length,
      ver: t('#verLine'), aboutIp: t('#aboutIp'),
      navIndicator: (() => { const e = document.querySelector('#navInd'); return e ? { h: Math.round(e.getBoundingClientRect().height), op: getComputedStyle(e).opacity } : null; })(),
      overflow: { x: document.documentElement.scrollWidth - innerWidth, y: document.documentElement.scrollHeight - innerHeight },
      domNodes: document.getElementsByTagName('*').length,
      theme: document.documentElement.getAttribute('data-theme'),
    };
  })()`;

  const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true });
  console.log(JSON.stringify({ logs, dom: r.result?.result?.value }, null, 2));
  ws.close();
  chrome.kill();
})();
