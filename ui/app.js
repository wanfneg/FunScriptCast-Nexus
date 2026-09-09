/* ============================================================
   FunScriptCast-Nexus —— 前端逻辑
   与 host_server.py 的 /api/* 通信；状态 1s 轮询（页面隐藏时暂停）
   ============================================================ */
(function () {
  "use strict";

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var S = { state: null, settings: {}, glossary: { ja: {}, en: {} }, pollTimer: null, counts: {} };

  function motionOff() { return document.documentElement.getAttribute("data-motion") === "off"; }

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

    /* ---- 设置页 ---- */
    $("#aboutIp").textContent = (st.host && st.host.lan_ip) || "—";
    $("#aboutPort").textContent = (st.host && st.host.port) || "—";
    $("#verLine").textContent = "v" + (st.version || "—") + " · WebView2";
    syncSettingsUI();
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

  function syncSettingsUI() {
    var s = S.settings;
    $("#setDlnaAuto").checked = !!s.dlna_auto_start;
    $("#setSubAuto").checked = !!s.subtitle_auto_start;
    $("#setCloseTray").checked = !!s.close_to_tray;
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
  function loadGlossary() {
    api("/api/glossary").then(function (r) {
      if (!r.ok) return;
      S.glossary = r.langs || { ja: {}, en: {} };
      renderGlossary("ja");
      renderGlossary("en");
    });
  }
  var GLOSSARY_RENDER_CAP = 60;   // 渲染上限：避免上千条目把 DOM 撑爆（搜索可过滤）
  function renderGlossary(lang) {
    var terms = S.glossary[lang] || {};
    var q = ($("#" + lang + "Search").value || "").toLowerCase();
    var keys = Object.keys(terms).filter(function (k) {
      return !q || k.toLowerCase().indexOf(q) >= 0 || String(terms[k]).toLowerCase().indexOf(q) >= 0;
    });
    $("#" + lang + "Count").textContent = Object.keys(terms).length + " 条";
    var shown = keys.slice(0, GLOSSARY_RENDER_CAP);
    var html = shown.map(function (k) {
      return '<div class="term-row"><input class="input mono" style="height:28px" data-gk="' + esc(k) + '" value="' + esc(k) + '">' +
        '<input class="input" style="height:28px" data-gv="' + esc(k) + '" value="' + esc(terms[k]) + '">' +
        '<button class="icon-btn" data-gdel="' + esc(k) + '" data-glang="' + lang + '"><svg class="ic"><use href="#i-trash"/></svg></button></div>';
    }).join("");
    if (keys.length > shown.length) {
      html += '<div class="hint" style="padding:8px 0">仅显示前 ' + shown.length + ' 条（共 ' + keys.length + ' 条），用上方搜索框过滤</div>';
    }
    $("#" + lang + "List").innerHTML = html || '<div class="empty">没有匹配的条目</div>';
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

    /* 术语表 */
    $("#saveGloss").addEventListener("click", function () {
      var jobs = ["ja", "en"].map(function (lang) {
        var terms = {};
        $$('[data-gk]').forEach(function (inp) {
          var row = inp.parentElement;
          var langOfRow = row.querySelector("[data-gdel]").getAttribute("data-glang");
          if (langOfRow !== lang) return;
          var k = inp.value.trim(), v = row.querySelector("[data-gv]").value.trim();
          if (k) terms[k] = v;
        });
        return api("/api/glossary/save", "POST", { lang: lang, terms: terms });
      });
      Promise.all(jobs).then(function () { toast("术语表已保存并热重载"); });
    });
    ["ja", "en"].forEach(function (lang) {
      $("#" + lang + "Search").addEventListener("input", function () { renderGlossary(lang); });
      $("#" + lang + "List").addEventListener("click", function (e) {
        var b = e.target.closest("[data-gdel]");
        if (!b) return;
        var k = b.getAttribute("data-gdel");
        delete S.glossary[lang][k];
        renderGlossary(lang);
        toast("已删除（记得点保存）", k, "warn");
      });
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
    $("#copyIp").addEventListener("click", function () {
      var ip = $("#aboutIp").textContent;
      if (ip && ip !== "—") copyText(ip, this);
    });
    $("#quitApp").addEventListener("click", function () {
      api("/api/quit", "POST", {}).then(function () { window.close(); });
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
