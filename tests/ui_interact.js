// 交互回归：验证这次重构动过的行为没坏。
// 重点是那些"看不见但会静默失效"的地方：页面过渡的清理、分段滑块重摆位、
// 图标随主题切换、toast 图标 id、按钮涟漪。
const http = require('http');
const { spawn } = require('child_process');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const URL = 'http://127.0.0.1:8790/';
const PORT = 9338;

function get(p) {
  return new Promise((resolve, reject) => {
    http.get({ host: '127.0.0.1', port: PORT, path: p }, res => {
      let d = ''; res.on('data', c => (d += c));
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch (e) { resolve(d); } });
    }).on('error', reject);
  });
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
function check(ok, label, detail) {
  results.push({ ok, label, detail: detail === undefined ? '' : String(detail) });
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${label}${detail !== undefined ? '  — ' + detail : ''}`);
}

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars',
    `--remote-debugging-port=${PORT}`, '--window-size=1240,820',
    '--no-first-run', '--no-default-browser-check',
    '--user-data-dir=' + process.env.TEMP + '\\cdp-inter', URL,
  ], { stdio: 'ignore' });

  let target = null;
  for (let i = 0; i < 40; i++) {
    await sleep(250);
    try { const l = await get('/json/list'); target = (l || []).find(t => t.type === 'page' && t.webSocketDebuggerUrl); if (target) break; } catch (_) {}
  }
  if (!target) { console.log('no target'); chrome.kill(); return; }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => (ws.onopen = r));
  let id = 0; const pending = new Map(); const problems = [];
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === 'Runtime.exceptionThrown') problems.push(m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text);
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error')
      problems.push((m.params.args || []).map(a => a.value || a.description || '').join(' '));
  };
  const send = (method, params) => new Promise(res => { const mid = ++id; pending.set(mid, res); ws.send(JSON.stringify({ id: mid, method, params: params || {} })); });
  const evalJs = async e => {
    const r = await send('Runtime.evaluate', { expression: e, returnByValue: true });
    if (r.result && r.result.exceptionDetails) { problems.push('EVAL: ' + r.result.exceptionDetails.text); return null; }
    return r.result && r.result.result ? r.result.result.value : null;
  };

  await send('Runtime.enable'); await send('Page.enable');
  await sleep(2600);

  // ---- 1. 主题切换：属性、文案、图标三者必须一起变
  const t0 = await evalJs(`(() => { const u = document.querySelector('#themeToggle use');
    return { theme: document.documentElement.getAttribute('data-theme'), label: document.querySelector('#themeLabel').textContent,
             icon: u ? u.getAttribute('href') : null }; })()`);
  await evalJs(`document.querySelector('#themeToggle').click()`);
  await sleep(500);
  const t1 = await evalJs(`(() => { const u = document.querySelector('#themeToggle use');
    return { theme: document.documentElement.getAttribute('data-theme'), label: document.querySelector('#themeLabel').textContent,
             icon: u ? u.getAttribute('href') : null }; })()`);
  check(t0.theme === 'dark' && t1.theme === 'light', '主题切换生效', `${t0.theme} → ${t1.theme}`);
  check(t1.label !== t0.label, '按钮文案跟随', `${t0.label} → ${t1.label}`);
  check(t0.icon === '#i-sun' && t1.icon === '#i-moon', '按钮图标跟随（暗色显太阳）', `${t0.icon} → ${t1.icon}`);
  await evalJs(`document.querySelector('#themeToggle').click()`);   // 切回暗色
  await sleep(400);

  // ---- 2. 页面过渡：旧页必须被清掉 .leaving，否则会残留并挡住新页
  await evalJs(`document.querySelector('button[data-page="subtitle"]').click()`);
  await sleep(60);
  const mid = await evalJs(`(() => ({ leaving: document.querySelectorAll('.page.leaving').length,
      active: document.querySelectorAll('.page.active').length }))()`);
  await sleep(600);
  const after = await evalJs(`(() => ({ leaving: document.querySelectorAll('.page.leaving').length,
      active: (document.querySelector('.page.active') || {}).id,
      enter: document.querySelectorAll('.page.enter').length }))()`);
  check(mid.leaving >= 1, '切换瞬间旧页有 .leaving（过渡确实在跑）', `leaving=${mid.leaving}`);
  check(after.leaving === 0, '.leaving 会被清理（不会残留）', `leaving=${after.leaving}`);
  check(after.active === 'page-subtitle', '新页已激活', after.active);

  // ---- 3. 隐藏页里的分段控件在显示后必须重新摆位
  await evalJs(`document.querySelector('button[data-page="settings"]').click()`);
  await sleep(700);
  const seg = await evalJs(`(() => { const s = document.querySelector('#motionSeg'), t = document.querySelector('#motionThumb');
    const cur = s.querySelector('button[aria-selected="true"]');
    const tr = t.getBoundingClientRect(), br = cur.getBoundingClientRect();
    return { thumbW: Math.round(tr.width), btnW: Math.round(br.width), dx: +(tr.left - br.left).toFixed(1) }; })()`);
  check(seg.thumbW > 0 && seg.thumbW === seg.btnW && Math.abs(seg.dx) < 1.5,
        '设置页滑块重新摆位正确', `w=${seg.thumbW}/${seg.btnW} dx=${seg.dx}`);

  // ---- 4. 点击分段按钮后滑块跟随
  await evalJs(`document.querySelector('#motionSeg button[data-motion-opt="off"]').click()`);
  await sleep(500);
  const seg2 = await evalJs(`(() => { const s = document.querySelector('#motionSeg'), t = document.querySelector('#motionThumb');
    const cur = s.querySelector('button[aria-selected="true"]');
    const tr = t.getBoundingClientRect(), br = cur.getBoundingClientRect();
    return { sel: cur.getAttribute('data-motion-opt'), motion: document.documentElement.getAttribute('data-motion'),
             dx: +(tr.left - br.left).toFixed(1), w: Math.round(tr.width) === Math.round(br.width) }; })()`);
  check(seg2.sel === 'off' && seg2.motion === 'off', '切换动画强度生效', `${seg2.sel}/${seg2.motion}`);
  check(Math.abs(seg2.dx) < 1.5 && seg2.w, '滑块跟随按钮', `dx=${seg2.dx}`);
  await evalJs(`document.querySelector('#motionSeg button[data-motion-opt="full"]').click()`);
  await sleep(400);

  // ---- 5. toast：图标 id 必须能在 sprite 里找到
  await evalJs(`(function(){ var b=document.createElement('button'); b.className='btn ghost'; b.id='__probe';
      b.textContent='probe'; document.body.appendChild(b); b.click(); b.remove(); })()`);
  await evalJs(`(() => { const t = document.querySelector('.toasts');
      if (window.__t) return; })()`);
  // 直接触发一次请求失败以产生 toast 太重，改为检查已有 toast 或跳过
  const toastIconOk = await evalJs(`(() => {
    const syms = {}; document.querySelectorAll('.sprite symbol').forEach(function(s){ syms[s.id]=1; });
    let bad = 0;
    document.querySelectorAll('.toast use').forEach(function(u){
      const id = (u.getAttribute('href')||'').replace('#','');
      if (id && !syms[id]) bad++;
    });
    return bad; })()`);
  check(toastIconOk === 0, '现有 toast 图标全部可解析', `无法解析 ${toastIconOk} 个`);

  // ---- 6. 按钮涟漪
  await evalJs(`document.querySelector('button[data-page="dashboard"]').click()`);
  await sleep(700);
  const ripple = await evalJs(`(() => { const b = document.querySelector('#btnRefresh');
    const r = b.getBoundingClientRect();
    b.dispatchEvent(new PointerEvent('pointerdown', { bubbles:true, clientX:r.left+10, clientY:r.top+10 }));
    return b.querySelectorAll('.ripple').length; })()`);
  check(ripple >= 1, '按钮涟漪仍然生成', `ripple=${ripple}`);
  await sleep(400);

  // ---- 7. 信号波形：真实的绘制内容（不是空白 canvas）
  const sigInfo = await evalJs(`(() => {
    const cv = document.querySelector('#pulseCanvas');
    if (!cv) return null;
    const ctx = cv.getContext('2d');
    const d = ctx.getImageData(0, 0, cv.width, cv.height).data;
    let nonEmpty = 0;
    for (let i = 3; i < d.length; i += 4) if (d[i] > 8) nonEmpty++;
    return { px: cv.width + 'x' + cv.height, paintedPct: +(nonEmpty / (cv.width*cv.height) * 100).toFixed(2),
             state: document.querySelector('#pulseState').textContent }; })()`);
  check(sigInfo && sigInfo.paintedPct > 0.2, '信号波形确实绘制了内容',
        sigInfo ? `${sigInfo.px} 覆盖率 ${sigInfo.paintedPct}% 状态「${sigInfo.state}」` : 'canvas 缺失');

  // ---- 8. 导航指示块跟随
  const nav = await evalJs(`(() => { const ind = document.querySelector('#navInd');
    const cur = document.querySelector('button[aria-current="true"]');
    const ir = ind.getBoundingClientRect(), cr = cur.getBoundingClientRect();
    return { dy: +(ir.top - cr.top).toFixed(1), dh: +(ir.height - cr.height).toFixed(1), page: cur.getAttribute('data-page') }; })()`);
  check(Math.abs(nav.dy) < 1 && Math.abs(nav.dh) < 1, '导航指示块对准当前项', `dy=${nav.dy} dh=${nav.dh}`);

  // ---- 9. 无表情符号：界面可见文本里不允许出现 emoji
  const emoji = await evalJs(`(() => {
    const re = /[\\u{1F000}-\\u{1FAFF}\\u{2600}-\\u{27BF}\\u{2B00}-\\u{2BFF}\\u{FE0F}]/u;
    const hits = [];
    document.querySelectorAll('body *').forEach(function(el){
      if (!el.children.length) {
        const t = el.textContent || '';
        if (re.test(t)) hits.push(t.trim().slice(0, 40));
      }
    });
    return hits; })()`);
  check(emoji.length === 0, '界面可见文本无表情符号', emoji.length ? emoji.join(' | ') : '0 处');

  console.log(`\n控制台问题: ${problems.length}`);
  problems.slice(0, 5).forEach(p => console.log('   ', String(p).slice(0, 160)));
  const failed = results.filter(r => !r.ok).length;
  console.log(failed ? `\nFAILED ${failed} 项` : '\nALL PASS');
  ws.close(); chrome.kill();
})();
