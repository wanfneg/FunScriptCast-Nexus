// 更新弹窗回归（评审 F07）：下载/检查失败必须能收尾，任何状态都要留一条关闭路径。
//
// 为什么不用 jsdom：项目的 UI 测试一律走「无头 Chrome + CDP」（见 ui_check.js），
// 这里沿用同一套路，但**不需要启动宿主** —— 用一个静态服务器托管 ui/，并在页面脚本
// 运行前注入 fetch 桩，把 /api/state 变成可控的输入，从而走**真实的轮询与弹窗代码路径**。
//
// 覆盖：
//   1. 下载中弹窗可关（"后台运行"）且关掉之后不再每秒重弹；
//   2. 弹窗打开时下载失败 → 切换成"更新失败"并留"关闭"按钮（此前是死锁）；
//   3. 任何打开中的弹窗至少有一个可见按钮（这条是死锁的通用判据）。
//
// 用法：node tests/ui_update_modal.js
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const UI_DIR = path.join(__dirname, '..', 'ui');
const HTTP_PORT = 8799;
const CDP_PORT = 9341;

const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
function check(ok, label, detail) {
  results.push({ ok, label });
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${label}${detail !== undefined ? '  — ' + detail : ''}`);
}

// ---- 静态服务器（只伺候 ui/，不需要宿主）----------------------------------
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
               '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png' };
const server = http.createServer((req, res) => {
  const rel = decodeURIComponent(req.url.split('?')[0]).replace(/^\/+/, '') || 'index.html';
  const file = path.join(UI_DIR, rel);
  if (!file.startsWith(UI_DIR) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) {
    res.writeHead(404); res.end('nope'); return;
  }
  res.writeHead(200, { 'Content-Type': MIME[path.extname(file)] || 'application/octet-stream' });
  fs.createReadStream(file).pipe(res);
});

// ---- CDP 小工具 -----------------------------------------------------------
function httpGet(p) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port: CDP_PORT, path: p }, res => {
      let d = ''; res.on('data', c => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { resolve(d); } });
    }).on('error', reject);
  });
}

(async () => {
  await new Promise(r => server.listen(HTTP_PORT, '127.0.0.1', r));

  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars',
    `--remote-debugging-port=${CDP_PORT}`, '--window-size=1240,820',
    '--no-first-run', '--no-default-browser-check',
    '--user-data-dir=' + path.join(process.env.TEMP || '.', 'cdp-updmodal'),
    'about:blank',
  ], { stdio: 'ignore' });

  let target = null;
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    try {
      const l = await httpGet('/json/list');
      target = (l || []).find(t => t.type === 'page' && t.webSocketDebuggerUrl);
      if (target) break;
    } catch (_) {}
  }
  if (!target) { console.log('  [FAIL] 起不来 headless Chrome'); chrome.kill(); server.close(); process.exit(1); }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => (ws.onopen = r));
  let id = 0; const pending = new Map();
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
  };
  const send = (method, params) => new Promise(res => {
    const mid = ++id; pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
  });
  const evaluate = async expr => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.result && r.result.exceptionDetails) throw new Error(JSON.stringify(r.result.exceptionDetails));
    return r.result && r.result.result ? r.result.result.value : undefined;
  };

  await send('Page.enable');
  await send('Runtime.enable');

  // 页面脚本运行**之前**注入：把 /api/state 换成可控输入，其余请求给个无害响应。
  await send('Page.addScriptToEvaluateOnNewDocument', { source: `
    window.__UPD = { state: null, version: "1.0.20" };
    window.__FETCH_LOG = [];
    const realFetch = window.fetch;
    window.fetch = function (url, opts) {
      const u = String(url);
      window.__FETCH_LOG.push(u);
      if (u.indexOf('/api/state') === 0) {
        return Promise.resolve(new Response(JSON.stringify(Object.assign(
          { ok: true, version: window.__UPD.version, update: window.__UPD.state },
          window.__UPD.extra || {})), { status: 200, headers: { 'Content-Type': 'application/json' } }));
      }
      return Promise.resolve(new Response(JSON.stringify({ ok: true }), { status: 200,
        headers: { 'Content-Type': 'application/json' } }));
    };
  ` });

  await send('Page.navigate', { url: `http://127.0.0.1:${HTTP_PORT}/index.html` });
  await sleep(1800);   // 让首轮轮询跑完

  const modalOpen = () => evaluate('!document.getElementById("updModal").hidden');
  const btnState = () => evaluate(`(function(){
    const g = document.getElementById("updModalGo"), l = document.getElementById("updModalLater");
    return { go: !g.hidden, later: !l.hidden, laterText: l.textContent.trim(),
             title: document.getElementById("updModalTitle").textContent.trim(),
             text: document.getElementById("updModalText").textContent.trim() };
  })()`);
  const setState = s => evaluate(`window.__UPD.state = ${JSON.stringify(s)}; true`);

  // --- 1. 下载中：弹窗打开，且**有**关闭路径（"后台运行"）------------------
  await setState({ state: 'downloading', latest: '1.0.21', pct: 42, current: '1.0.20' });
  await sleep(1500);
  let open = await modalOpen(), b = await btnState();
  check(open === true, '下载中弹窗会打开', JSON.stringify(b));
  check(b.later === true && b.laterText === '后台运行',
        '下载中提供"后台运行"关闭路径（此前两个按钮都隐藏 ⇒ 死锁）', b.laterText);
  check(b.go === false, '下载中不提供"下载更新"主按钮（避免重复触发）');

  // --- 2. 点"后台运行"：弹窗关掉，且**不再每秒重弹** ----------------------
  await evaluate('document.getElementById("updModalLater").click(); true');
  await sleep(1500);   // 期间至少又轮询了一次，状态仍是 downloading
  check((await modalOpen()) === false, '点"后台运行"后弹窗保持关闭（不会被轮询重弹）');

  // --- 3. 弹窗打开时失败 → 必须收尾成"更新失败"并留"关闭" ----------------
  await setState({ state: 'available', latest: '1.0.21', has_update: true, size: 116000000, current: '1.0.20' });
  await sleep(1500);
  check((await modalOpen()) === true, '发现新版本时会弹窗');
  await setState({ state: 'error', latest: '1.0.21', error: '连接被重置' });
  await sleep(1500);
  open = await modalOpen(); b = await btnState();
  check(open === true, '失败时弹窗仍在（改为错误态而不是消失/卡住）');
  check(b.later === true && b.laterText === '关闭', '失败态提供"关闭"按钮', b.laterText);
  check(b.go === false, '失败态不显示"下载更新"主按钮');
  check(/更新失败|失败/.test(b.title) && /连接被重置/.test(b.text),
        '失败原因回显给用户', b.title + ' / ' + b.text);

  // --- 4. 失败态关掉后不再重弹；通用判据：打开中的弹窗必有可见按钮 --------
  await evaluate('document.getElementById("updModalLater").click(); true');
  await sleep(1500);
  check((await modalOpen()) === false, '失败态关掉后不再重弹');
  let allGood = true, seen = [];
  for (const st of [
    { state: 'downloading', latest: '1.0.21', pct: 80, current: '1.0.20' },
    { state: 'available', latest: '1.0.21', has_update: true, size: 1, current: '1.0.20' },
    { state: 'ready', latest: '1.0.21', current: '1.0.20' },
    { state: 'error', error: 'x' },
  ]) {
    await setState(st);
    await sleep(1300);
    if (!(await modalOpen())) continue;
    const bb = await btnState();
    seen.push(st.state + ':' + (bb.go ? 'go' : '') + (bb.later ? 'later' : ''));
    if (!bb.go && !bb.later) allGood = false;
  }
  check(allGood, '任何打开中的弹窗都至少有一个可见按钮（死锁通用判据）', seen.join(' '));

  const errs = await evaluate('window.__UPD_ERRS || 0');
  console.log(`\n  ${results.filter(r => r.ok).length}/${results.length} 项通过` + (errs ? `（页面错误 ${errs}）` : ''));
  ws.close(); chrome.kill(); server.close();
  process.exit(results.every(r => r.ok) ? 0 : 1);
})().catch(e => { console.log('  [FAIL] 脚本异常：' + e.message); server.close(); process.exit(1); });
