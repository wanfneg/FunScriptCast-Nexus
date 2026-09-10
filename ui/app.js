/* ============================================================
   FunScriptCast-Nexus —— 前端逻辑
   与 host_server.py 的 /api/* 通信；状态 1s 轮询（页面隐藏时暂停）
   ============================================================ */
(function () {
  "use strict";

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var S = { state: null, settings: {}, glossary: { ja: {}, en: {} }, pollTimer: null, counts: {}, glossLang: "ja" };

  function motionOff() { return document.documentElement.getAttribute("data-motion") === "off"; }

  /* pywebview JS 桥是否就绪（浏览器里直接开页面时没有） */
  function bridgeReady() {
    if (window.pywebview && window.pywebview.api) return true;
    toast("仅桌面应用中可用", "浏览器里没有系统文件对话框", "warn");
    return false;
  }

  /* ---------------------------------------------------------- API */
  function api(path, method, body) {
    return fetch(path, {
      method: method || "GET",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined
    }).then(function (r) { return r.json(); }).catch(function (e) {
      toast("请求失败", String(e), "err");
      return { ok: false };
    });
  }

  /* ---------------------------------------------------------- Toast */
  var toastBox = $("#toasts");
  function toast(title, desc, kind) {
    var el = document.createElement("div");
    el.className = "toast " + (kind || "ok");
    el.innerHTML = '<svg class="ic"><use href="#' + (kind === "err" ? "i-warn" : "i-check") + '"/></svg>' +
      '<div><div class="tt">' + esc(title) + "</div>" + (desc ? '<div class="td">' + esc(desc) + "</div>" : "") + "</div>";
    toastBox.appendChild(el);
    setTimeout(function () { el.classList.add("out"); setTimeout(function () { el.remove(); }, 220); }, 3000);
  }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c];
    });
  }

  /* ---------------------------------------------------------- 导航 */
  var nav = $("#nav"), ind = $("#navInd"), content = $("#content");
  function moveIndicator(btn) {
    if (!btn) return;
    var r = btn.getBoundingClientRect(), pr = nav.getBoundingClientRect();
    ind.style.transform = "translateY(" + (r.top - pr.top) + "px)";
    ind.style.height = r.height + "px";
  }
  nav.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-page]");
    if (!btn) return;
    $$("button[data-page]", nav).forEach(function (b) { b.removeAttribute("aria-current"); });
    btn.setAttribute("aria-current", "true");
    moveIndicator(btn);
    $$(".page", content).forEach(function (p) { p.classList.remove("active"); });
    var page = document.getElementById("page-" + btn.getAttribute("data-page"));
    if (page) { page.classList.add("active"); content.scrollTop = 0; }
  });
  window.addEventListener("resize", function () { moveIndicator($('button[aria-current="true"]', nav)); });

  /* ---------------------------------------------------------- 分段控件 */
  function initSeg(segId, thumbId, onPick) {
    var seg = document.getElementById(segId), thumb = document.getElementById(thumbId);
    if (!seg || !thumb) return;
    function place(btn) {
      if (!btn) return;
      var r = btn.getBoundingClientRect(), pr = seg.getBoundingClientRect();
      thumb.style.width = r.width + "px";
      thumb.style.transform = "translateX(" + (r.left - pr.left - 3) + "px)";
    }
    seg.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      $$("button", seg).forEach(function (x) { x.setAttribute("aria-selected", x === b ? "true" : "false"); });
      place(b);
      if (onPick) onPick(b);
    });
    var cur = $('button[aria-selected="true"]', seg) || $("button", seg);
    if (cur) { cur.setAttribute("aria-selected", "true"); place(cur); }
    window.addEventListener("resize", function () { place($('button[aria-selected="true"]', seg)); });
  }

  /* ---------------------------------------------------------- 主题 / 动效 */
  function placeSegThumb(segId, thumbId, sel) {
    var seg = document.getElementById(segId), thumb = document.getElementById(thumbId);
    var b = $(sel, seg);
    if (!seg || !thumb || !b) return;
    var r = b.getBoundingClientRect(), pr = seg.getBoundingClientRect();
    thumb.style.width = r.width + "px";
    thumb.style.transform = "translateX(" + (r.left - pr.left - 3) + "px)";
  }
  function setTheme(t, persist) {
    document.documentElement.setAttribute("data-theme", t);
    var label = t === "dark" ? "亮色" : "暗色";
    var tip = t === "dark" ? "切换到亮色" : "切换到暗色";
    ["themeLabel", "themeLabel2"].forEach(function (id) {
      var el = document.getElementById(id); if (el) el.textContent = label;
    });
    ["themeToggle", "themeToggle2"].forEach(function (id) {
      var el = document.getElementById(id); if (el) el.title = tip;
    });
    if (persist) api("/api/settings", "POST", { theme: t });
  }
  function setMotion(m, persist) {
    document.documentElement.setAttribute("data-motion", m);
    $$("#motionSeg button").forEach(function (b) {
      b.setAttribute("aria-selected", b.getAttribute("data-motion-opt") === m ? "true" : "false");
    });
    placeSegThumb("motionSeg", "motionThumb", '#motionSeg button[data-motion-opt="' + m + '"]');
    if (persist) api("/api/settings", "POST", { motion: m });
  }

  /* ---------------------------------------------------------- 渲染 */
  function fmtUptime(sec) {
    if (!sec) return "0";
    var m = Math.floor(sec / 60);
    if (m < 60) return String(m);
    return Math.floor(m / 60) + "h" + (m % 60);
  }
  function setBadge(el, kind, text) {
    if (!el) return;
    el.className = "badge " + (kind || "");
    el.textContent = text;
  }
  function setPill(el, cls, text) {
    if (!el) return;
    el.className = "pill " + (cls || "");
    el.innerHTML = '<i class="dot"></i>' + esc(text);
  }
  function setRing(pct) {
    var ring = $("#gpuRing"), c = 2 * Math.PI * 24;
    var fg = $(".fg", ring);
    fg.style.strokeDashoffset = (c * (1 - pct / 100)).toFixed(1);
    $(".pct", ring).textContent = Math.round(pct) + "%";
    ring.classList.toggle("warn", pct >= 85);
  }
  function countTo(el, target) {
    if (!el) return;
    var key = el.id, from = S.counts[key] == null ? 0 : S.counts[key];
    S.counts[key] = target;
    if (from === target || motionOff()) { el.textContent = String(target); return; }
    var t0 = performance.now(), dur = 500;
    (function step(t) {
      var p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      el.textContent = Math.round(from + (target - from) * e);
      if (p < 1) requestAnimationFrame(step);
    })(t0);
  }

  function render(st) {
    S.state = st;
    S.settings = st.settings || {};

    /* ---- 顶部胶囊 ---- */
    var d = st.dlna || {};
    if (d.starting) setPill($("#pillDlna"), "busy", "DLNA 启动中");
    else if (d.running) setPill($("#pillDlna"), "on", "DLNA 运行中");
    else setPill($("#pillDlna"), "", "DLNA 已停止");

    var sub = st.subtitle || {};
    if (sub.status === "ready") setPill($("#pillSub"), "on", "字幕服务就绪");
    else if (sub.status === "loading") setPill($("#pillSub"), "busy", "字幕服务加载中");
    else if (sub.status === "error") setPill($("#pillSub"), "", "字幕服务异常");
    else setPill($("#pillSub"), "", "字幕服务已停止");

    var g = st.gpu || {};
    var gpuPct = g.total_mb ? (g.used_mb / g.total_mb * 100) : 0;
    setPill($("#pillGpu"), g.used_mb ? (gpuPct >= 85 ? "busy" : "on") : "", "GPU " + (g.used_mb ? Math.round(gpuPct) + "%" : "—"));

    /* ---- 状态轨道 ---- */
    var railD = $("#railDlna");
    railD.className = "rail-node" + (d.starting ? " busy" : d.running ? " on" : "");
    $("#railDlnaD").textContent = d.running ? (d.url || "") : (d.starting ? "启动中…" : (d.error || "未启动"));
    $("#railDlnaU").textContent = d.running ? ((d.roots || []).length + " 个媒体根") : "";

    var railS = $("#railSub");
    railS.className = "rail-node" + (sub.status === "loading" ? " busy" : sub.status === "ready" ? " on" : "");
    $("#railSubD").textContent =
      sub.status === "ready" ? ("PID " + sub.pid + " · :" + sub.port) :
      sub.status === "loading" ? "正在加载模型…" :
      sub.status === "error" ? (sub.error || "异常") : "未启动";
    $("#railSubM").textContent = sub.health ? (sub.health.asr_model || "") : "";

    var railG = $("#railGpu");
    railG.className = "rail-node" + (g.used_mb ? (gpuPct >= 85 ? " busy" : " on") : "");
    $("#railGpuD").textContent = g.name ? g.name : "—";
    $("#railGpuU").textContent = g.total_mb ? (Math.round(g.used_mb / 1024 * 10) / 10 + " / " + Math.round(g.total_mb / 1024 * 10) / 10 + " GB") : "";

    /* ---- 指标 ---- */
    countTo($("#mRoots"), (d.roots || []).length);
    countTo($("#mUptime"), Math.floor((st.uptime || 0) / 60));
    var termCount = 0;
    if (sub.health && sub.health.glossary) {
      Object.keys(sub.health.glossary).forEach(function (k) { termCount += sub.health.glossary[k] || 0; });
    }
    if (termCount) countTo($("#mTerms"), termCount);
    setRing(gpuPct);
    $("#gpuText").textContent = g.total_mb ? (Math.round(g.used_mb) + " / " + Math.round(g.total_mb) + " MB") : "—";

    /* ---- 事件时间线 ---- */
    var tl = $("#timeline");
    var evs = (st.events || []).slice(-6).reverse();
    if (!evs.length) { tl.innerHTML = '<div class="empty">暂无事件</div>'; }
    else {
      tl.innerHTML = evs.map(function (e) {
        var cls = e.level === "err" ? "err" : e.level === "warn" ? "warn" : "ok";
        var t = new Date(e.ts * 1000);
        var hh = String(t.getHours()).padStart(2, "0") + ":" + String(t.getMinutes()).padStart(2, "0") + ":" + String(t.getSeconds()).padStart(2, "0");
        return '<div class="tl-item ' + cls + '"><div class="t">' + esc(e.msg) + '</div><div class="w">' + hh + "</div></div>";
      }).join("");
    }

    /* ---- DLNA 页 ---- */
    setBadge($("#dlnaBadge"), d.starting ? "warn" : d.running ? "ok" : (d.error ? "err" : ""),
      d.starting ? "启动中" : d.running ? "运行中" : d.error ? "错误" : "已停止");
    $("#dlnaStatusSub").textContent = d.running ? ("运行中 · " + (d.url || "")) : (d.error || "未启动");
    if (document.activeElement !== $("#dlnaPort")) $("#dlnaPort").value = d.port || 8899;
    $("#dlnaUrl").value = d.url || "—";
    setBadge($("#rootsBadge"), "", (d.roots || []).length + " 个");
    renderRoots(d.roots || []);
    renderDlnaLogs(d.logs || []);

    /* ---- 字幕页 ---- */
    setBadge($("#subBadge"), sub.status === "ready" ? "ok" : sub.status === "loading" ? "warn" : sub.status === "error" ? "err" : "",
      sub.status === "ready" ? "就绪" : sub.status === "loading" ? "加载中" : sub.status === "error" ? "错误" : "已停止");
    $("#subSub").textContent = sub.status === "ready"
      ? ("独立子进程 · PID " + sub.pid + " · 端口 " + sub.port)
      : sub.status === "loading" ? "独立子进程 · 正在加载模型（首次约 3~10s）"
      : sub.status === "error" ? ("独立子进程 · " + (sub.error || "异常"))
      : "独立子进程 · 未启动";
    $("#subLoadBar").style.display = sub.status === "loading" ? "" : "none";
    var h = sub.health || {};
    $("#subModel").textContent = h.asr_model || "—";
    $("#subDevice").textContent = h.device || "—";
    $("#subMt").textContent = h.translate_backend || "—";
    $("#subGloss").textContent = h.glossary ? Object.keys(h.glossary).map(function (k) { return k + " " + h.glossary[k]; }).join(" · ") : "—";

    /* ---- 字幕缓存 ---- */
    var sc = st.subtitleCache || {};
    setBadge($("#cacheBadge"), sc.count ? "ok" : "", (sc.count || 0) + " 个");
    $("#cacheSub").textContent = sc.count
      ? (sc.count + " 个视频已缓存 · 共 " + (sc.size_kb || 0) + " KB · 最近 "
         + (sc.newest ? new Date(sc.newest * 1000).toLocaleString() : "—"))
      : "还没有缓存字幕（首次播放会生成并保存）";
    renderTranslateCache(st.translate || {}, st.translateCache || {});

    /* ---- 设备同步页 ---- */
    renderSync(st.sync || {});

    /* ---- 设置页 ---- */
    $("#aboutIp").textContent = (st.host && st.host.lan_ip) || "—";
    $("#aboutPort").textContent = (st.host && st.host.port) || "—";
    $("#aboutLanApi").textContent = (st.host && st.host.lan_api_url) || "—";
    $("#verLine").textContent = "v" + (st.version || "—") + " · WebView2";    syncSettingsUI();
  }

  /* ---- 翻译层统计（批量 / 缓存命中 / 纠错 / 兜底） ---- */
  function renderTranslateCache(tr, tc) {
    var el = $("#cacheTrSub");
    if (!el) { return; }
    if (!tr || !tr.ready) { el.textContent = "翻译层：字幕服务未就绪"; return; }
    var s = tr.stats || {};
    var parts = ["LLM " + (s.batches || 0) + " 批"];
    var hits = s.cache_hits || 0;
    parts.push("命中 " + hits + (s.cache_disk_hits ? "（磁盘 " + s.cache_disk_hits + "）" : ""));
    if (s.fix_rounds) { parts.push("纠错 " + s.fix_rounds + " 轮"); }
    if (s.partial_batches) { parts.push("部分救回 " + s.partial_batches + " 批"); }
    if (s.fail_batches) {
      parts.push("失败 " + s.fail_batches + " 批");
      if (s.fallback_batches) { parts.push("兜底 " + s.fallback_batches + " 批"); }
    }
    var line = "翻译层：" + parts.join(" · ");
    if (tc && tc.count) { line += " · 译文缓存 " + tc.count + " 条 " + (tc.size_kb || 0) + " KB"; }
    if (s.degraded || s.fallback_error) {
      line += "（已降级：" + (s.fallback_error || "免费后端") + "）";
    }
    el.textContent = line;
  }

  function renderRoots(roots) {
    var box = $("#rootList");
    if (!roots.length) { box.innerHTML = '<div class="empty">还没有媒体根目录</div>'; return; }
    box.innerHTML = roots.map(function (p, i) {
      return '<div class="row"><svg class="ic" style="color:var(--ink-3)"><use href="#i-folder"/></svg>' +
        '<div class="grow"><div class="name mono" style="font-size:12px">' + esc(p) + "</div></div>" +
        '<span class="badge acc">已启用</span>' +
        '<button class="icon-btn del" data-del-root="' + i + '" title="移除"><svg class="ic"><use href="#i-trash"/></svg></button></div>';
    }).join("");
  }

  var lastDlnaLogLen = 0;
  function renderDlnaLogs(logs) {
    if (logs.length === lastDlnaLogLen) return;
    lastDlnaLogLen = logs.length;
    var inner = $("#dlnaLogInner");
    inner.innerHTML = logs.map(function (l) {
      var cls = /失败|错误|error/i.test(l) ? "err" : /停止|warn/i.test(l) ? "warn" : "ok";
      return '<div class="' + cls + '">' + esc(l) + "</div>";
    }).join("");
    inner.scrollTop = 1e6;
  }

  /* ---------------------------------------------------------- 设备同步 */
  var SY = { kind: "script", devices: [] };

  function renderSync(sy) {
    var connected = !!sy.connected;
    setBadge($("#syncDevBadge"), connected ? "ok" : "", connected ? "已连接" : "未连接");
    $("#syncDevSub").textContent = connected
      ? ("已连接 · " + (sy.serial || ""))
      : "未连接 · 请先扫描并连接 Quest";

    if (document.activeElement !== $("#syncAdbPath")) $("#syncAdbPath").value = sy.adb_path || "";
    if (document.activeElement !== $("#syncForce")) $("#syncForce").checked = !!sy.force_full;
    if (document.activeElement !== $("#syncDelete")) $("#syncDelete").checked = !!sy.delete_extra;

    var slots = sy.slots || {};
    ["script", "video"].forEach(function (kind) {
      var s = slots[kind] || {};
      var cap = kind === "script" ? "Script" : "Video";
      var badge = $("#sync" + cap + "Badge");
      setBadge(badge, s.busy ? "warn" : s.result ? "ok" : s.error ? "err" : "",
        s.busy ? "同步中" : s.result ? "已完成" : s.error ? "失败" : "待命");
      var local = $("#sync" + cap + "Local"), dev = $("#sync" + cap + "Device");
      if (local && document.activeElement !== local) local.value = s.local_folder || "";
      if (dev && document.activeElement !== dev) dev.value = s.device_folder || "";
      var res = $("#sync" + cap + "Result");
      if (res) {
        if (s.busy) res.textContent = "正在同步…";
        else if (s.error) res.textContent = "错误：" + s.error;
        else if (s.result) res.textContent = "本地 " + s.result.local + " · 设备 " + s.result.device + " · 本次推送 " + s.result.pushed;
        else res.textContent = "尚未同步";
      }
      var runBtn = $("#syncRun" + cap);
      if (runBtn) runBtn.disabled = !!s.busy;
    });
    // 日志框只显示当前选中类型
    var cur = slots[SY.kind] || {};
    renderSyncLogs(cur.logs || []);
  }
  var lastSyncLogLen = -1;
  function renderSyncLogs(logs) {
    if (logs.length === lastSyncLogLen) return;
    lastSyncLogLen = logs.length;
    var inner = $("#syncLogInner");
    if (!logs.length) { inner.innerHTML = '<div class="empty">暂无同步日志</div>'; return; }
    inner.innerHTML = logs.map(function (l) {
      var cls = /失败|错误|error/i.test(l) ? "err" : /====|警告/i.test(l) ? "warn" : "ok";
      return '<div class="' + cls + '">' + esc(l) + "</div>";
    }).join("");
    inner.scrollTop = 1e6;
  }

  function renderDeviceList(devices, keepSerial) {
    var sel = $("#syncDeviceSel");
    if (!sel) return;
    if (!devices.length) { sel.innerHTML = '<option value="">未发现设备</option>'; return; }
    sel.innerHTML = devices.map(function (d) {
      var label = d.serial + (d.model ? " · " + d.model : "") + (d.usb ? " · USB" : "");
      return '<option value="' + esc(d.serial) + '">' + esc(label) + "</option>";
    }).join("");
    if (keepSerial) sel.value = keepSerial;
  }

  function scanDevices(notify) {
    return api("/api/sync/devices", "POST", {}).then(function (r) {
      SY.devices = r.devices || [];
      renderDeviceList(SY.devices, "");
      if (notify) {
        if (!r.ok) toast("扫描失败", r.error || "adb 不可用", "err");
        else toast(SY.devices.length ? "发现 " + SY.devices.length + " 台设备" : "未发现设备",
          r.adb ? r.adb : "");
      }
      return r;
    });
  }

  function runSync(kind) {
    SY.kind = kind;
    lastSyncLogLen = -1;
    var btn = $("#syncRun" + (kind === "script" ? "Script" : "Video"));
    if (btn) btn.disabled = true;
    api("/api/sync/run", "POST", { kind: kind }).then(function (r) {
      if (!r.ok) {
        if (btn) btn.disabled = false;
        toast("无法开始同步", r.error || "", "err");
        return;
      }
      toast("开始同步", kind === "script" ? "脚本" : "视频");
      poll(true);
    });
  }

  function syncSettingsUI() {
    var s = S.settings;
    $("#setDlnaAuto").checked = !!s.dlna_auto_start;
    $("#setSubAuto").checked = !!s.subtitle_auto_start;
    $("#setCloseTray").checked = !!s.close_to_tray;
    $("#setStartMin").checked = !!s.start_minimized;
    if (document.activeElement !== $("#mtModel")) $("#mtModel").value = "";
    // 首次拉到设置后应用持久化的主题 / 动画强度
    if (!S.themeApplied) {
      S.themeApplied = true;
      if (s.theme) setTheme(s.theme, false);
      if (s.motion) setMotion(s.motion, false);
    }
  }

  /* ---------------------------------------------------------- 字幕配置 / 术语表 */
  function loadSubtitleConfig() {
    api("/api/subtitle/config").then(function (r) {
      if (!r.ok) return;
      var c = r.config || {};
      var asr = c.asr || {}, seg = c.segment || {}, tr = c.translate || {}, vad = c.vad || {};
      $("#asrModel").value = asr.model || "";
      $("#asrDevice").value = asr.device || "";
      $("#segMaxSec").value = seg.max_sec != null ? seg.max_sec : "";
      $("#segMaxChars").value = seg.max_chars != null ? seg.max_chars : "";
      $("#segPause").value = seg.pause_sec != null ? seg.pause_sec : "";
      $("#vadThreshold").value = vad.threshold != null ? vad.threshold : "";
      $("#mtBackend").value = tr.backend || "ollama";
      var ollama = tr.ollama || {};
      $("#mtModel").value = ollama.model || "";
      $("#mtBase").value = ollama.base_url || "";
    });
  }
  /* 术语表：界面不渲染条目（4000+ 条会撑爆 DOM），只显示每张表的统计 */
  function loadGlossary() {
    api("/api/glossary").then(function (r) {
      if (!r.ok) return;
      S.glossary = r.langs || { ja: {}, en: {} };
      renderGlossStats();
    });
  }
  function renderGlossStats() {
    var ja = Object.keys(S.glossary.ja || {}).length;
    var en = Object.keys(S.glossary.en || {}).length;
    var t = $("#glossTotal"); if (t) t.textContent = (ja + en) + " 条";
    var c1 = $("#glossJaCount"); if (c1) c1.textContent = ja + " 条";
    var c2 = $("#glossEnCount"); if (c2) c2.textContent = en + " 条";
    var m1 = $("#glossJaMeta"); if (m1) m1.textContent = "glossary_ja_zh.json";
    var m2 = $("#glossEnMeta"); if (m2) m2.textContent = "glossary_en_zh.json";
  }

  /* ---------------------------------------------------------- 事件绑定 */
  function bind() {
    /* 导航指示条初始位置 */
    setTimeout(function () { moveIndicator($('button[aria-current="true"]', nav)); }, 60);

    /* 主题 / 动效 */
    $("#themeToggle").addEventListener("click", function () {
      setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark", true);
    });
    $("#themeToggle2").addEventListener("click", function () {
      setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark", true);
    });
    initSeg("motionSeg", "motionThumb", function (b) { setMotion(b.getAttribute("data-motion-opt"), true); });

    /* DLNA */
    $("#dlnaStart").addEventListener("click", startDlna);
    $("#dlnaStop").addEventListener("click", function () {
      api("/api/dlna/stop", "POST", {}).then(function () { toast("DLNA 已停止"); poll(true); });
    });
    $("#addRoot").addEventListener("click", function () {
      var v = ($("#newRoot").value || "").trim();
      if (!v) { toast("请输入目录路径", "", "warn"); return; }
      var roots = (S.settings.dlna_roots || []).slice();
      if (roots.indexOf(v) >= 0) { toast("该目录已存在", "", "warn"); return; }
      roots.push(v);
      api("/api/settings", "POST", { dlna_roots: roots }).then(function () {
        $("#newRoot").value = "";
        toast("已添加媒体根", v);
        poll(true);
      });
    });
    $("#rootList").addEventListener("click", function (e) {
      var b = e.target.closest("[data-del-root]");
      if (!b) return;
      var i = parseInt(b.getAttribute("data-del-root"), 10);
      var roots = (S.settings.dlna_roots || []).slice();
      var removed = roots.splice(i, 1);
      api("/api/settings", "POST", { dlna_roots: roots }).then(function () {
        toast("已移除", removed[0] || "");
        poll(true);
      });
    });
    $("#dlnaCopy").addEventListener("click", function () {
      var v = $("#dlnaUrl").value;
      if (!v || v === "—") return;
      copyText(v, this);
    });
    $("#dlnaLogToggle").addEventListener("click", function () {
      var box = $("#dlnaLogBox"), open = box.classList.toggle("open");
      this.textContent = open ? "收起日志" : "展开日志";
      if (open) box.querySelector(".inner").scrollTop = 1e6;
    });

    /* 字幕服务 */
    $("#subStart").addEventListener("click", startSub);
    $("#subStop").addEventListener("click", function () {
      api("/api/subtitle/stop", "POST", {}).then(function () { toast("字幕服务已停止", "显存已释放"); poll(true); });
    });
    $("#saveMt").addEventListener("click", function () {
      var body = {
        translate: {
          backend: $("#mtBackend").value,
          ollama: { model: $("#mtModel").value, base_url: $("#mtBase").value }
        }
      };
      api("/api/subtitle/config", "POST", body).then(function (r) {
        toast(r.ok ? "翻译设置已保存" : "保存失败", r.ok ? "重启字幕服务后生效" : (r.error || ""), r.ok ? "ok" : "err");
      });
    });
    $("#saveAsr").addEventListener("click", function () {
      var body = {
        asr: { model: $("#asrModel").value, device: $("#asrDevice").value },
        segment: {
          max_sec: parseFloat($("#segMaxSec").value) || 8,
          max_chars: parseInt($("#segMaxChars").value, 10) || 50,
          pause_sec: parseFloat($("#segPause").value) || 0.8
        },
        vad: { threshold: parseFloat($("#vadThreshold").value) || 0.5 }
      };
      api("/api/subtitle/config", "POST", body).then(function (r) {
        toast(r.ok ? "识别设置已保存" : "保存失败", r.ok ? "重启字幕服务后生效" : (r.error || ""), r.ok ? "ok" : "err");
      });
    });

    $("#cacheClear").addEventListener("click", function () {
      api("/api/subtitle/cache/clear", "POST", {}).then(function (r) {
        if (r.ok) { toast("字幕缓存已清空"); poll(true); }
        else toast("清空失败", r.error || "", "err");
      });
    });

    /* 术语表：界面只做 CSV 导入/导出，不渲染条目 */
    initSeg("glossSeg", "glossThumb", function (b) {
      S.glossLang = b.getAttribute("data-glang-opt");
    });
    $("#glossExport").addEventListener("click", function () {
      if (!bridgeReady()) return;
      var lang = S.glossLang || "ja";
      var name = (lang === "ja" ? "glossary_ja_zh" : "glossary_en_zh") + ".csv";
      window.pywebview.api.pick_file("save", name, ["CSV 文件 (*.csv)", "所有文件 (*.*)"]).then(function (r) {
        if (!r || !r.ok) return;
        api("/api/glossary/export", "POST", { lang: lang, path: r.path }).then(function (res) {
          if (res.ok) toast("已导出 " + res.count + " 条", res.path);
          else toast("导出失败", res.error || "", "err");
        });
      });
    });
    $("#glossImport").addEventListener("click", function () {
      if (!bridgeReady()) return;
      var lang = S.glossLang || "ja";
      var replace = !!$("#glossReplace").checked;
      window.pywebview.api.pick_file("open", "", ["CSV 文件 (*.csv)", "所有文件 (*.*)"]).then(function (r) {
        if (!r || !r.ok) return;
        var body = { path: r.path, lang: lang };
        if (replace) body.mode = "replace";
        api("/api/glossary/import", "POST", body).then(function (res) {
          if (!res.ok) { toast("导入失败", res.error || "", "err"); return; }
          if (replace) {
            S.glossary[lang] = res.terms;
            renderGlossStats();
            toast("已整体替换 " + res.count + " 条", "已写盘并热重载");
          } else {
            var cur = S.glossary[lang] || {};
            var before = Object.keys(cur).length;
            Object.keys(res.terms).forEach(function (k) { cur[k] = res.terms[k]; });
            S.glossary[lang] = cur;
            renderGlossStats();
            toast("已解析 " + res.count + " 条（新增 " + (Object.keys(cur).length - before) + "）",
              "点「保存术语表」写入并热重载", "warn");
          }
        });
      });
    });
    $("#saveGloss").addEventListener("click", function () {
      var jobs = ["ja", "en"].map(function (lang) {
        var terms = S.glossary[lang] || {};
        return api("/api/glossary/save", "POST", { lang: lang, terms: terms });
      });
      Promise.all(jobs).then(function () {
        renderGlossStats();
        toast("术语表已保存并热重载");
      });
    });

    /* 设备同步 */
    $("#syncDevices").addEventListener("click", function () { scanDevices(true); });
    $("#syncConnect").addEventListener("click", function () {
      var serial = $("#syncDeviceSel").value;
      if (!serial) { toast("请先扫描并选择设备", "", "warn"); return; }
      api("/api/sync/connect", "POST", { serial: serial }).then(function (r) {
        if (r.ok) { toast("设备已连接", serial); poll(true); }
        else toast("连接失败", r.error || "", "err");
      });
    });
    $("#syncDisconnect").addEventListener("click", function () {
      api("/api/sync/disconnect", "POST", {}).then(function () { toast("已断开设备"); poll(true); });
    });
    $("#syncRunScript").addEventListener("click", function () { runSync("script"); });
    $("#syncRunVideo").addEventListener("click", function () { runSync("video"); });
    $("#syncAdbPath").addEventListener("change", function () {
      api("/api/settings", "POST", { adb_path: this.value.trim() });
    });
    $("#syncForce").addEventListener("change", function () {
      api("/api/settings", "POST", { sync_force_full: this.checked });
    });
    $("#syncDelete").addEventListener("change", function () {
      api("/api/settings", "POST", { sync_delete_extra: this.checked });
      if (this.checked) toast("将删除设备上多余文件", "请确认设备目录正确", "warn");
    });
    $("#syncScriptLocal").addEventListener("change", function () {
      api("/api/settings", "POST", { script_folder: this.value.trim() });
    });
    $("#syncVideoLocal").addEventListener("change", function () {
      api("/api/settings", "POST", { video_folder: this.value.trim() });
    });
    $("#syncScriptDevice").addEventListener("change", function () {
      api("/api/settings", "POST", { device_folder_script: this.value.trim() });
    });
    $("#syncVideoDevice").addEventListener("change", function () {
      api("/api/settings", "POST", { device_folder_video: this.value.trim() });
    });
    initSeg("syncLogSeg", "syncLogThumb", function (btn) {
      SY.kind = btn.getAttribute("data-kind");
      lastSyncLogLen = -1;
      poll(true);
    });
    // 目录选择（走 pywebview 原生对话框）
    $$("[data-pick]").forEach(function (b) {
      b.addEventListener("click", function () {
        var inp = document.getElementById(b.getAttribute("data-pick"));
        if (!inp) return;
        if (!bridgeReady()) return;
        window.pywebview.api.pick_folder(inp.value || "").then(function (r) {
          if (r && r.ok) {
            inp.value = r.path;
            inp.dispatchEvent(new Event("change"));
          }
        });
      });
    });

    /* 自绘标题栏按钮 */
    function winCall(name) {
      if (window.pywebview && window.pywebview.api && window.pywebview.api[name]) {
        window.pywebview.api[name]();
      } else {
        toast("窗口控制仅在桌面应用中可用", "", "warn");
      }
    }
    $("#winMin").addEventListener("click", function () { winCall("win_minimize"); });
    $("#winClose").addEventListener("click", function () { winCall("win_close"); });
    // 标题栏整体是拖动区（pywebview 会向上查找 .pywebview-drag-region），
    // 所以按钮/主题切换/状态胶囊必须吃掉 mousedown，否则点它们会变成拖窗口。
    $$(".titlebar button, .titlebar .pills").forEach(function (el) {
      el.addEventListener("mousedown", function (e) { e.stopPropagation(); });
    });

    /* 设置 */
    $("#setDlnaAuto").addEventListener("change", function () {
      api("/api/settings", "POST", { dlna_auto_start: this.checked });
    });
    $("#setSubAuto").addEventListener("change", function () {
      api("/api/settings", "POST", { subtitle_auto_start: this.checked });
    });
    $("#setCloseTray").addEventListener("change", function () {
      api("/api/settings", "POST", { close_to_tray: this.checked });
    });
    $("#setStartMin").addEventListener("change", function () {
      api("/api/settings", "POST", { start_minimized: this.checked });
      toast(this.checked ? "下次启动将直接隐藏到托盘" : "下次启动将显示主窗口");
    });
    $("#copyIp").addEventListener("click", function () {
      var ip = $("#aboutIp").textContent;
      if (ip && ip !== "—") copyText(ip, this);
    });
    $("#copyLanApi").addEventListener("click", function () {
      var u = $("#aboutLanApi").textContent;
      if (u && u !== "—") copyText(u, this);
    });
    $("#quitApp").addEventListener("click", function () {
      // 退出由后端执行（销毁窗口 + 收尾子进程）；这里不要调 window.close()，
      // 否则会被「关闭到托盘」拦截器拦下，只隐藏不退出。
      api("/api/quit", "POST", {}).then(function () {
        toast("正在退出", "窗口即将关闭");
        clearTimeout(S.pollTimer);
      });
    });

    /* 快捷操作 */
    $("#btnRefresh").addEventListener("click", function () { poll(true); toast("已刷新"); });
    $("#btnStartAll").addEventListener("click", function () { startDlna(); startSub(); });
    $("#qaDlnaStart").addEventListener("click", startDlna);
    $("#qaDlnaStop").addEventListener("click", function () {
      api("/api/dlna/stop", "POST", {}).then(function () { toast("DLNA 已停止"); poll(true); });
    });
    $("#qaSubStart").addEventListener("click", startSub);
    $("#qaSubStop").addEventListener("click", function () {
      api("/api/subtitle/stop", "POST", {}).then(function () { toast("字幕服务已停止", "显存已释放"); poll(true); });
    });

    /* 按钮波纹 */
    document.addEventListener("pointerdown", function (e) {
      var btn = e.target.closest(".btn");
      if (!btn || motionOff()) return;
      var r = btn.getBoundingClientRect();
      var sp = document.createElement("span");
      sp.className = "ripple";
      sp.style.left = (e.clientX - r.left) + "px";
      sp.style.top = (e.clientY - r.top) + "px";
      sp.style.width = sp.style.height = Math.max(r.width, r.height) + "px";
      btn.appendChild(sp);
      setTimeout(function () { sp.remove(); }, 260);
    });
  }

  function copyText(txt, btn) {
    try { navigator.clipboard && navigator.clipboard.writeText(txt); } catch (_) {}
    var old = btn.innerHTML;
    btn.innerHTML = '<svg class="ic"><use href="#i-check"/></svg>';
    btn.style.color = "var(--ok)";
    setTimeout(function () { btn.innerHTML = old; btn.style.color = ""; }, 1200);
  }

  function startDlna() {
    var port = parseInt($("#dlnaPort").value, 10) || 8899;
    if (!(S.settings.dlna_roots || []).length) { toast("请先添加媒体根目录", "", "warn"); return; }
    api("/api/settings", "POST", { dlna_port: port }).then(function () {
      api("/api/dlna/start", "POST", { port: port }).then(function (r) {
        toast(r.ok ? "DLNA 正在启动" : "启动失败", r.error || "", r.ok ? "ok" : "err");
        poll(true);
      });
    });
  }
  function startSub() {
    api("/api/subtitle/start", "POST", {}).then(function (r) {
      toast(r.ok ? "字幕服务正在启动" : "启动失败", r.error || "首次加载模型约 3~10s", r.ok ? "ok" : "err");
      poll(true);
    });
  }

  /* ---------------------------------------------------------- 轮询 */
  function poll(once) {
    api("/api/state").then(function (st) {
      if (st && st.ok) render(st);
    });
    if (once) return;
    clearTimeout(S.pollTimer);
    var delay = document.hidden ? 5000 : 1000;
    S.pollTimer = setTimeout(function () { poll(false); }, delay);
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(true); });

  /* ---------------------------------------------------------- 启动 */
  function boot() {
    bind();
    setTheme("dark", false);
    setMotion("full", false);
    poll(false);
    loadSubtitleConfig();
    loadGlossary();
    setTimeout(function () { loadSubtitleConfig(); }, 2500);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
