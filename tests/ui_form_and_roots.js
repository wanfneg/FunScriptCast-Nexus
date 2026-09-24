// 前端回归（评审 F08 / F09）：表单回填护栏 与 媒体根目录增删的丢更新。
//
// 沿用 ui_update_modal.js 的套路：静态托管 ui/ + 在页面脚本运行前注入 fetch 桩，
// 走**真实的轮询与事件路径**，不需要启动宿主。
//
// F08：模型下载完成时只许刷新模型下拉，**绝不能整表重填**——否则用户在"准备填云端 key"
//      途中被打断：输入被服务端旧值覆盖，他再点保存就把旧值写回去（以为配好了云端、
//      实际还在 local）。旧代码在这条路径上还会连带清掉 dirty 之外的其它输入。
// F09：媒体根目录增删基于本地缓存（S.settings，靠 1s 轮询刷新）全量回写 ⇒ 背靠背操作
//      丢更新：删 A → 轮询带回仍含 A 的旧列表 → 添加 B 时把 A 一起 POST 回去（A 复活，
//      而 toast 已经说过"已移除"）。修法是提交前先取服务端当前值。
//
// 用法：node tests/ui_form_and_roots.js
const http = require('http');
const fs = require('fs');
const path = require('path');
const { spawn } = require('child_process');

const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe';
const UI_DIR = path.join(__dirname, '..', 'ui');
const HTTP_PORT = 8801;
const CDP_PORT = 9343;

const sleep = ms => new Promise(r => setTimeout(r, ms));
const results = [];
function check(ok, label, detail) {
  results.push({ ok, label });
  console.log(`  [${ok ? 'PASS' : 'FAIL'}] ${label}${detail !== undefined ? '  — ' + detail : ''}`);
}

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
    `--remote-debugging-port=${CDP_PORT}`, '--window-size=1240,900',
    '--no-first-run', '--no-default-browser-check',
    '--user-data-dir=' + path.join(process.env.TEMP || '.', 'cdp-formroots'),
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

  // 页面脚本运行前注入：桩住所有 /api/*，并把关键状态放在 window.__ST 里供测试驱动。
  await send('Page.addScriptToEvaluateOnNewDocument', { source: `
    window.__ST = {
      serverRoots: ["C:/A"],            // 服务端的**权威**根目录（POST 会改它）
      staleRoots: ["C:/A"],             // /api/state 里那份（故意保持陈旧，模拟 1s 轮询滞后）
      postedRoots: [],                  // 记录每次 POST 的 roots
      serverCfg: { asr: { backend: "audiocpp", audiocpp: { model: "models/Qwen3-ASR-0.6B" } },
                   translate: { backend: "local", local: { model: "models/Sakura-7B.gguf" },
                                openai: { base_url: "https://cfg.example/v1", model: "cfg-model" } },
                   server: { idle_release_min: 5 } },
      // ⚠ 必须**先 downloading、输入之后再翻成 done**：若一开始就是 done，
      // "下载完成"那条分支会在页面启动那一次就触发（早于用户输入），之后
      // S.modelDoneSeen 拦住不再触发 ⇒ 断言恒真、测不出 F08（本轮先假通过过一次）。
      models: [ { id: "sakura-7b", role: "translate", state: "downloading", pct: 50,
                  label: "Sakura-7B", path: "models/Sakura-7B.gguf", name: "Sakura-7B",
                  files: [], size_gb: 4 } ],
    };
    function J(o) {
      return Promise.resolve(new Response(JSON.stringify(o), { status: 200,
        headers: { 'Content-Type': 'application/json' } }));
    }
    window.fetch = function (url, opts) {
      const u = String(url); const method = (opts && opts.method) || 'GET';
      if (u.indexOf('/api/state') === 0) {
        return J({ ok: true, version: "1.0.20",
                   settings: { dlna_roots: window.__ST.staleRoots },
                   /* ⚠ renderRoots 读的是 dlna.roots（宿主 /api/state 的 dlna 段），
                      不是 settings.dlna_roots —— 桩放错位置会让"根目录已渲染"前置失败 */
                   dlna: { roots: window.__ST.staleRoots },
                   subtitle: {}, subtitleCache: {}, translate: {}, sync: {}, gpu: {} });
      }
      if (u.indexOf('/api/settings') === 0) {
        if (method === 'POST') {
          let body = {};
          try { body = JSON.parse(opts.body || '{}'); } catch (e) {}
          if (body.dlna_roots) {
            window.__ST.postedRoots.push(body.dlna_roots.slice());
            window.__ST.serverRoots = body.dlna_roots.slice();   // 服务端接受了
          }
          return J({ ok: true, settings: window.__ST.serverRoots });
        }
        return J({ ok: true, settings: { dlna_roots: window.__ST.serverRoots } });
      }
      if (u.indexOf('/api/dlna/roots') === 0) {
        // ⚠ 添加根目录走的是这条（不是 /api/settings）：桩必须同样记录，
        // 否则测试会误判成"没提交"
        let body = {};
        try { body = JSON.parse(opts.body || '{}'); } catch (e) {}
        if (body.roots) {
          window.__ST.postedRoots.push(body.roots.slice());
          window.__ST.serverRoots = body.roots.slice();
        }
        return J({ ok: true, roots: window.__ST.serverRoots, missing: [], need_restart: false });
      }
      if (u.indexOf('/api/subtitle/config') === 0) {
        if (method === 'POST') return J({ ok: true, config: window.__ST.serverCfg });
        return J({ ok: true, config: window.__ST.serverCfg });
      }
      if (u.indexOf('/api/models/catalog') === 0) {
        // ⚠ loadModels 读的是 /api/models/catalog 的 r.items（不是 /api/models）：
        // 桩错路径 ⇒ "下载完成"那条分支永不触发 ⇒ F08 断言会**假通过**（旧代码也过）。
        // 本轮就是这么先假通过了一次，靠"还原旧代码再跑"才发现。
        return J({ ok: true, items: window.__ST.models });
      }
      if (u.indexOf('/api/subtitle/models') === 0) return J({ ok: true, models: window.__ST.models });
      if (u.indexOf('/api/subtitle/asr-models') === 0) {
        return J({ ok: true, models: [ { value: "models/Qwen3-ASR-0.6B", name: "Qwen3-ASR-0.6B" } ] });
      }
      if (u.indexOf('/api/models') === 0) return J({ ok: true, models: window.__ST.models });
      return J({ ok: true });
    };
  ` });

  await send('Page.navigate', { url: `http://127.0.0.1:${HTTP_PORT}/index.html` });
  await sleep(2000);

  // ================= F08 =================
  // 用户正在改「云端 base」：写值 + 派发 input（这会置 subCfgDirty）
  await evaluate(`(function(){
    var el = document.getElementById("mtCloudBase");
    el.value = "https://user-typing.example/v1";
    el.dispatchEvent(new Event("input", { bubbles: true }));
    return true;
  })()`);
  const beforeVal = await evaluate('document.getElementById("mtCloudBase").value');
  check(beforeVal === 'https://user-typing.example/v1', '测试前置：用户输入已写入', beforeVal);

  // 现在让"下载"完成 —— loadModels 每 1.5s 轮询一次，此时用户输入就在表单里
  await evaluate('window.__ST.models[0].state = "done"; window.__ST.models[0].pct = 100; true');
  await sleep(3500);
  const afterVal = await evaluate('document.getElementById("mtCloudBase").value');
  check(afterVal === 'https://user-typing.example/v1',
        'F08 模型下载完成后，用户未保存的输入不被覆盖', afterVal);
  // 下拉确实刷新过（否则就是"干脆不刷新"而不是"只刷新下拉"）
  const opts = await evaluate(`Array.prototype.map.call(
      document.getElementById("mtLocalModel").options, function(o){return o.value;})`);
  check(Array.isArray(opts), 'F08 本地模型下拉仍有选项（刷新未把列表清空）', JSON.stringify(opts));

  // ================= F09 =================
  // 先让界面渲染出根目录（/api/state 的陈旧列表）
  await sleep(1200);
  const rendered = await evaluate('document.querySelectorAll("[data-del-root]").length');
  check(rendered >= 1, '测试前置：根目录已渲染', rendered);

  // 删 A（走真实点击路径）
  await evaluate('document.querySelector("[data-del-root]").click(); true');
  await sleep(600);
  // 背靠背：立刻添加 B（此时本地缓存仍是陈旧的 [C:/A]）
  await evaluate(`(function(){
    document.getElementById("newRoot").value = "D:/B";
    document.getElementById("addRoot").click();
    return true;
  })()`);
  await sleep(900);

  const posts = await evaluate('window.__ST.postedRoots');
  const last = posts && posts.length ? posts[posts.length - 1] : null;
  check(last && last.indexOf('D:/B') >= 0, 'F09 添加 B 已提交', JSON.stringify(last));
  check(last && last.indexOf('C:/A') < 0,
        'F09 已删除的 A 不得被"背靠背添加 B"重新写回（丢更新）', JSON.stringify(posts));

  console.log(`\n  ${results.filter(r => r.ok).length}/${results.length} 项通过`);
  ws.close(); chrome.kill(); server.close();
  process.exit(results.every(r => r.ok) ? 0 : 1);
})().catch(e => { console.log('  [FAIL] 脚本异常：' + e.message); server.close(); process.exit(1); });
