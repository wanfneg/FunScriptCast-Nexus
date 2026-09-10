// 版式构成度量：把"感觉对不对"尽量变成数字。
// 关心的是：留白够不够、行宽是否可读、区块比例是否失衡、有没有大块空洞。
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const URL = 'http://127.0.0.1:8790/';
const PORT = 9337;
const PAGES = ['dashboard', 'dlna', 'subtitle', 'sync', 'glossary', 'settings'];

function get(p) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port: PORT, path: p }, res => {
      let d = ''; res.on('data', c => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { resolve(d); } });
    }).on('error', reject);
  });
}
const sleep = ms => new Promise(r => setTimeout(r, ms));

const GEO = `(() => {
  function box(el){ var r = el.getBoundingClientRect();
    return { x:Math.round(r.left), y:Math.round(r.top), w:Math.round(r.width), h:Math.round(r.height) }; }
  var pageEl = document.querySelector('.page.active');
  var page = box(pageEl);
  var vw = document.documentElement.clientWidth, vh = document.documentElement.clientHeight;
  var blocks = [];
  Array.prototype.forEach.call(pageEl.children, function(el, i){
    var b = box(el);
    if (b.w < 2 || b.h < 2) return;
    blocks.push({ i:i, cls:(el.className||'').split(' ').slice(0,3).join('.'), ...b,
                  fill: +(b.w*b.h/(page.w*page.h)*100).toFixed(1) });
  });
  // 垂直留白：相邻块之间的间隙
  var gaps = [];
  for (var k = 1; k < blocks.length; k++) gaps.push(blocks[k].y - (blocks[k-1].y + blocks[k-1].h));
  // 整页高度与视口对比：溢出多少
  var scrollH = document.querySelector('.content').scrollHeight;
  var head = document.querySelector('.page-head');
  var h1 = document.querySelector('.page-head h1');
  var pr = document.querySelector('.page-head p');
  // 行长（按中文字宽估算）
  var pStyle = pr ? getComputedStyle(pr) : null;
  var pChars = pr && pStyle ? Math.round(pr.getBoundingClientRect().width / parseFloat(pStyle.fontSize)) : null;
  // 最长的说明文字块
  var longest = 0;
  pageEl.querySelectorAll('.hint, .row .sub, .page-head p').forEach(function(e){
    var r = e.getBoundingClientRect();
    var fs = parseFloat(getComputedStyle(e).fontSize);
    if (r.width > 1) longest = Math.max(longest, Math.round(r.width / fs));
  });
  return {
    page: page, vw: vw, vh: vh, viewportFill: +(page.h / vh).toFixed(2),
    scrollH: scrollH, scrollRatio: +(scrollH / vh).toFixed(2),
    headerH: head ? Math.round(head.getBoundingClientRect().height) : 0,
    titlePx: h1 ? Math.round(parseFloat(getComputedStyle(h1).fontSize)) : 0,
    paraChars: pChars, longestRunChars: longest,
    blocks: blocks, gaps: gaps,
    contentLeft: document.querySelector('.content').getBoundingClientRect().left
  };
})()`;

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=1',
    `--remote-debugging-port=${PORT}`, '--window-size=1240,820',
    '--no-first-run', '--no-default-browser-check', '--font-render-hinting=none',
    '--user-data-dir=' + process.env.TEMP + '\\cdp-geo', URL,
  ], { stdio: 'ignore' });

  let target = null;
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    try { const l = await get('/json/list'); target = (l||[]).find(t => t.type==='page' && t.webSocketDebuggerUrl); if (target) break; } catch (_) {}
  }
  if (!target) { console.log('no target'); chrome.kill(); return; }
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => (ws.onopen = r));
  let id = 0; const pending = new Map();
  ws.onmessage = ev => { const m = JSON.parse(ev.data); if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); } };
  const send = (method, params) => new Promise(res => { const mid = ++id; pending.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params: params || {} })); });
  const evalJs = async e => { const r = await send('Runtime.evaluate', { expression: e, returnByValue: true }); return r.result && r.result.result ? r.result.result.value : null; };

  await send('Runtime.enable'); await send('Page.enable');
  await sleep(2600);
  const out = {};
  for (const p of PAGES) {
    await evalJs(`document.querySelector('button[data-page="${p}"]').click()`);
    await sleep(700);
    out[p] = await evalJs(GEO);
  }
  console.log(JSON.stringify(out, null, 1));
  ws.close(); chrome.kill();
})();
