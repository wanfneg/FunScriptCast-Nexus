// 界面客观审计：不靠"看着还行"，把能算的都算出来。
//
// 检查项：
//   1. 图标完整性——每个 <use href="#i-x"> 都能在 sprite 里找到同名 symbol
//      （指向不存在的 symbol 时元素静默变 0×0，肉眼很难发现）
//   2. 对比度——按 WCAG 公式逐层合成半透明背景后算实际对比度
//   3. 排版层级——h1 / h2 / eyebrow / 正文 / 数字 的字号必须单调递减
//   4. 对齐——同一页里各面板左边缘必须一致
//   5. 溢出——可见元素不得超出视口
//   6. 滑块与指示块——分段控件滑块、导航指示块必须真的落在目标按钮上
//   7. canvas 后备缓冲必须等于 CSS 尺寸 × dpr
//
// 用法：先启动宿主，再 `node tests/ui_audit.js`
const http = require('http');
const { spawn } = require('child_process');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const URL = 'http://127.0.0.1:8790/';
const PORT = 9336;
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

const AUDIT_JS = `(() => {
  /* ---------- 颜色工具：沿祖先链合成半透明背景 ---------- */
  function parse(c){
    var m = String(c||'').match(/rgba?\\(([^)]+)\\)/);
    if(!m) return null;
    var p = m[1].split(',').map(function(x){return parseFloat(x);});
    return { r:p[0], g:p[1], b:p[2], a: p.length>3 ? p[3] : 1 };
  }
  function over(top, bot){          // top 叠在 bot 上
    var a = top.a + bot.a*(1-top.a);
    if(a === 0) return {r:0,g:0,b:0,a:0};
    return {
      r:(top.r*top.a + bot.r*bot.a*(1-top.a))/a,
      g:(top.g*top.a + bot.g*bot.a*(1-top.a))/a,
      b:(top.b*top.a + bot.b*bot.a*(1-top.a))/a,
      a:a
    };
  }
  function effBg(el){
    var stack = [];
    for(var n = el; n; n = n.parentElement){
      var c = parse(getComputedStyle(n).backgroundColor);
      if(c && c.a > 0) stack.push(c);
      if(c && c.a >= 0.999) break;
    }
    var base = parse(getComputedStyle(document.documentElement).backgroundColor)
            || parse(getComputedStyle(document.body).backgroundColor)
            || {r:8,g:9,b:16,a:1};
    var out = base;
    for(var i = stack.length-1; i >= 0; i--) out = over(stack[i], out);
    return out;
  }
  function lum(c){
    function ch(v){ v/=255; return v <= 0.03928 ? v/12.92 : Math.pow((v+0.055)/1.055, 2.4); }
    return 0.2126*ch(c.r) + 0.7152*ch(c.g) + 0.0722*ch(c.b);
  }
  function contrast(fg, bg){
    var L1 = lum(fg), L2 = lum(bg);
    var hi = Math.max(L1,L2), lo = Math.min(L1,L2);
    return (hi + 0.05) / (lo + 0.05);
  }
  function ratioOf(el){
    var fgc = parse(getComputedStyle(el).color);
    if(!fgc) return null;
    var bg = effBg(el);
    var fg = fgc.a >= 0.999 ? fgc : over(fgc, bg);
    return +contrast(fg, bg).toFixed(2);
  }

  /* ---------- 1. 图标完整性 ---------- */
  var syms = {};
  document.querySelectorAll('.sprite symbol').forEach(function(s){ syms[s.id] = 1; });
  var missing = [];
  document.querySelectorAll('use').forEach(function(u){
    var h = u.getAttribute('href') || u.getAttribute('xlink:href') || '';
    var id = h.replace(/^#/, '');
    if(id && !syms[id]) missing.push(id);
  });

  /* ---------- 2. 对比度（只测当前可见页） ---------- */
  var probes = [
    ['.page-head h1','主标题'], ['.page-head p','页面说明'], ['.eyebrow','小标签'],
    ['.hint','提示文字'], ['.metric .num','指标数字'], ['.metric-lab','指标标签'],
    ['.metric .unit','指标单位'], ['.nav-label','导航文字'], ['.nav .nav-ix','导航序号'],
    ['.pill','状态胶囊'], ['.badge','徽标'], ['.btn.ghost','次要按钮'],
    ['.btn.primary','主按钮'], ['.input','输入框'], ['.rail-node .t','轨道标题'],
    ['.rail-node .d','轨道值'], ['.tl-item .t','事件文字'], ['.tl-item .w','事件时间'],
    ['.panel-head h2','面板标题'], ['.row .name','行标题'], ['.row .sub','行说明'],
    ['.theme-toggle','主题按钮'], ['.brand-name','品牌名'], ['.brand-sub','品牌副标'],
    ['.side-foot .ver','版本号'], ['.empty','空状态']
  ];
  var contrastRows = [];
  probes.forEach(function(p){
    var el = document.querySelector(p[0]);
    if(!el) return;
    var r = el.getBoundingClientRect();
    if(r.width < 1) return;                 // 隐藏页里的量不到，跳过
    var cr = ratioOf(el);
    var fs = parseFloat(getComputedStyle(el).fontSize);
    var bold = parseInt(getComputedStyle(el).fontWeight, 10) >= 700;
    var large = fs >= 24 || (fs >= 18.66 && bold);
    var need = large ? 3 : 4.5;
    contrastRows.push({ sel:p[0], name:p[1], ratio:cr, fontPx:+fs.toFixed(1), need:need,
                    pass: cr >= need });
  });

  /* ---------- 3. 排版层级 ---------- */
  function fsOf(s){ var e = document.querySelector(s); return e ? +parseFloat(getComputedStyle(e).fontSize).toFixed(1) : null; }
  var scale = {
    h1: fsOf('.page-head h1'), h2: fsOf('.panel-head h2'),
    num: fsOf('.metric .num'), body: fsOf('.page-head p'),
    eyebrow: fsOf('.eyebrow'), hint: fsOf('.hint'), mono: fsOf('.row .sub')
  };

  /* ---------- 4. 对齐：最左侧面板必须贴住内容区的内边距，不能莫名缩进 ---------- */
  var lefts = [];
  document.querySelectorAll('.page.active .panel').forEach(function(p){
    var r = p.getBoundingClientRect();
    if(r.width > 1) lefts.push(+r.left.toFixed(1));
  });
  // 基准是 .page 自己的内边距（.content 不带内边距，缩进在 .page 上）
  var pageEl = document.querySelector('.page.active');
  var csPage = getComputedStyle(pageEl);
  var expectLeft = +(pageEl.getBoundingClientRect().left + parseFloat(csPage.paddingLeft)).toFixed(1);
  var minLeft = lefts.length ? Math.min.apply(null, lefts) : null;
  // 只有"最左那一列"能用来判断整体缩进；网格里其它列本来就在右边
  var leftSpread = minLeft === null ? 0 : +(minLeft - expectLeft).toFixed(1);

  /* ---------- 5. 溢出 ---------- */
  var vw = document.documentElement.clientWidth;
  var overflow = [];
  document.querySelectorAll('.page.active *').forEach(function(e){
    var r = e.getBoundingClientRect();
    if(r.width < 1) return;
    if(r.right > vw + 1.5) overflow.push((e.className && typeof e.className === 'string' ? e.className.split(' ')[0] : e.tagName) + ' right=' + Math.round(r.right));
  });

  /* ---------- 6. 滑块 / 指示块 ---------- */
  var segCheck = [];
  document.querySelectorAll('.page.active .seg').forEach(function(seg){
    var thumb = seg.querySelector('.seg-thumb');
    var cur = seg.querySelector('button[aria-selected="true"]');
    if(!thumb || !cur) return;
    var tr = thumb.getBoundingClientRect(), br = cur.getBoundingClientRect(), sr = seg.getBoundingClientRect();
    segCheck.push({
      id: seg.id || '(no id)',
      thumbW: Math.round(tr.width), btnW: Math.round(br.width),
      dx: +(tr.left - br.left).toFixed(1),
      inside: tr.left >= sr.left - 0.5 && tr.right <= sr.right + 0.5
    });
  });
  var navInd = (function(){
    var ind = document.querySelector('#navInd'), cur = document.querySelector('button[aria-current="true"]');
    if(!ind || !cur) return null;
    var ir = ind.getBoundingClientRect(), cr = cur.getBoundingClientRect();
    return { dy: +(ir.top - cr.top).toFixed(1), hDiff: +(ir.height - cr.height).toFixed(1) };
  })();

  /* ---------- 7. canvas ---------- */
  var cv = document.querySelector('#pulseCanvas');
  var cvInfo = null;
  if(cv){
    var r = cv.getBoundingClientRect();
    var dpr = Math.min(2, window.devicePixelRatio || 1);
    cvInfo = { css: Math.round(r.width)+'x'+Math.round(r.height), buf: cv.width+'x'+cv.height,
               expected: Math.round(r.width*dpr)+'x'+Math.round(r.height*dpr) };
  }

  return {
    missingIcons: missing, spriteCount: Object.keys(syms).length,
    contrast: contrastRows, contrastFails: contrastRows.filter(function(c){return !c.pass;}).length,
    scale: scale, leftIndent: leftSpread, overflow: overflow.slice(0,8), segCheck: segCheck,
    navInd: navInd, canvas: cvInfo
  };
})()`;

(async () => {
  const chrome = spawn(CHROME, [
    '--headless=new', '--disable-gpu', '--hide-scrollbars', '--force-device-scale-factor=1',
    `--remote-debugging-port=${PORT}`, '--window-size=1280,860',
    '--no-first-run', '--no-default-browser-check', '--font-render-hinting=none',
    '--user-data-dir=' + process.env.TEMP + '\\cdp-audit', URL,
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
  if (!target) { console.log('no target'); chrome.kill(); return; }

  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise(r => (ws.onopen = r));
  let id = 0;
  const pending = new Map();
  const problems = [];
  ws.onmessage = ev => {
    const m = JSON.parse(ev.data);
    if (m.id && pending.has(m.id)) { pending.get(m.id)(m); pending.delete(m.id); }
    if (m.method === 'Runtime.exceptionThrown')
      problems.push('EXCEPTION: ' + (m.params.exceptionDetails.exception?.description || m.params.exceptionDetails.text));
    if (m.method === 'Runtime.consoleAPICalled' && m.params.type === 'error')
      problems.push('console.error: ' + (m.params.args || []).map(a => a.value || a.description || '').join(' '));
  };
  const send = (method, params) => new Promise(res => {
    const mid = ++id; pending.set(mid, res);
    ws.send(JSON.stringify({ id: mid, method, params: params || {} }));
  });
  const evalJs = async (expr) => {
    const r = await send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    const res = r.result || {};
    if (res.exceptionDetails) {
      problems.push('EVAL THROW: ' + (res.exceptionDetails.exception?.description
                   || res.exceptionDetails.text || JSON.stringify(res.exceptionDetails)));
      return null;
    }
    return res.result ? res.result.value : null;
  };

  await send('Runtime.enable');
  await send('Page.enable');
  await sleep(2600);

  const out = {};
  for (const theme of ['dark', 'light']) {
    await evalJs(`document.documentElement.setAttribute('data-theme','${theme}')`);
    await sleep(400);
    out[theme] = {};
    for (const p of PAGES) {
      await evalJs(`document.querySelector('button[data-page="${p}"]').click()`);
      await sleep(700);
      out[theme][p] = await evalJs(AUDIT_JS);
    }
  }
  console.log(JSON.stringify({ audit: out, problems }, null, 1));
  ws.close();
  chrome.kill();
})();
