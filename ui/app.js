/* ============================================================
   FunScriptCast-Nexus —— 前端逻辑
   与 host_server.py 的 /api/* 通信；状态 1s 轮询（页面隐藏时暂停）
   ============================================================ */
(function () {
  "use strict";

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var S = { state: null, settings: {}, pollTimer: null, counts: {} };

  function motionOff() { return document.documentElement.getAttribute("data-motion") === "off"; }

  /* ------------------------------------------------ 轮询回填护栏（新增控件默认受保护）
     每秒一次的 /api/state 回填会把"用户刚改过"的控件改回服务器旧值。聚焦中的框有
     activeElement 可挡，但**已失焦、尚未落盘**的编辑挡不住：用户填完直接点旁边的按钮
     → 按钮 mousedown 让输入框失焦 → 下一次轮询把文本改回旧值，而按钮那一下用的还是
     旧值。R41 只给 #mtModel 打了个补丁；这里做成统一判据——聚焦中或 dirty 一律不回填，
     保存成功才清 dirty（保存失败就保持 dirty，宁可不再回填也不丢用户的输入）。 */
  var DIRTY = Object.create(null);       // 控件 id → 有未落盘的编辑
  var PENDING = Object.create(null);     // 控件 id → 进行中的保存 Promise

  function busyEditing(el) {
    return !!el && (document.activeElement === el || DIRTY[el.id] === true);
  }
  function markDirty(el) { if (el && el.id) DIRTY[el.id] = true; }
  function clearDirty(el) { if (el && el.id) delete DIRTY[el.id]; }

  /* 保存单个设置项（6 个同步字段 + 4 个设置开关共用一份实现）。
     记下 Promise 是为了"点同步前先把路径落盘"——服务端 /api/sync/run 读的是已保存设置。 */
  function saveSetting(id, key, value, onDone) {
    var body = {};
    body[key] = value;
    var p = api("/api/settings", "POST", body).then(function (r) {
      if (r && r.ok) clearDirty(document.getElementById(id));
      else if (r && r.error) {
        // 保存失败必须让用户看见并保持 dirty：dirty 没清，runSync 的落盘闸门
        // 会拒绝按"屏幕上的值"开工；此前失败被当成成功，同步会拿未保存的目录跑。
        // 网络失败由 api() 统一 toast（同 addRoots 口径），这里只报服务端明确拒绝。
        toast("设置保存失败", r.error, "err");
      }
      if (onDone) onDone(r);
      return r;
    });
    PENDING[id] = p;
    return p;
  }
  function flushSettings(ids) {
    return Promise.all(ids.map(function (id) { return PENDING[id] || Promise.resolve(); }));
  }

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
    el.innerHTML = '<svg class="ic"><use href="#' + (kind === "err" ? "i-triangle-alert" : kind === "warn" ? "i-triangle-alert" : "i-circle-check") + '"/></svg>' +
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
  function activeNavBtn() { return $('button[aria-current="true"]', nav); }

  /* 页面切换：旧页先模糊上移淡出，新页挂上 .enter 让子元素逐级落下。
     用定时器而不是 animationend —— 动画强度设为「关闭」时根本不会派发动画事件，
     监听 animationend 会让旧页永远留在屏幕上。 */
  var pageTimer = null;
  function showPage(name) {
    var btn = $('button[data-page="' + name + '"]', nav);
    if (btn) {
      $$("button[data-page]", nav).forEach(function (b) { b.removeAttribute("aria-current"); });
      btn.setAttribute("aria-current", "true");
      moveIndicator(btn);
    }
    var next = document.getElementById("page-" + name);
    if (!next) return;
    var cur = $(".page.active", content);
    clearTimeout(pageTimer);
    if (cur && cur !== next) {
      cur.classList.remove("active");
      if (!motionOff()) {
        cur.classList.add("leaving");
        pageTimer = setTimeout(function () { cur.classList.remove("leaving"); }, 170);
      }
    }
    next.classList.add("active");
    if (!motionOff()) {
      next.classList.remove("enter");
      void next.offsetWidth;      // 强制重排，让动画能重新触发
      next.classList.add("enter");
    }
    content.scrollTop = 0;
    /* 分段控件与 canvas 在隐藏页里量不到尺寸（getBoundingClientRect 全是 0），
       所以每次页面显示后都要重新摆一次；等一帧让 display 生效。 */
    requestAnimationFrame(function () { placeAllSegs(); });
  }
  nav.addEventListener("click", function (e) {
    var btn = e.target.closest("button[data-page]");
    if (btn) showPage(btn.getAttribute("data-page"));
  });
  window.addEventListener("resize", function () { moveIndicator(activeNavBtn()); placeAllSegs(); });

  /* ---------------------------------------------------------- 分段控件 */
  var segs = [];   // 已注册的分段控件，字体加载完/窗口缩放时统一重新摆位
  function initSeg(segId, thumbId, onPick) {
    var seg = document.getElementById(segId), thumb = document.getElementById(thumbId);
    if (!seg || !thumb) return;
    var ctl = { seg: seg, thumb: thumb };
    segs.push(ctl);

    ctl.place = function (btn) {
      if (!btn) return;
      /* 绝对定位子元素的 left:0/top:0 落在**内边距盒**上（边框内侧），
         所以位移要减掉**边框宽度**而不是内边距。减错的话滑块会整体偏掉
         一个边框宽（实测偏 2px），而且这种偏差肉眼几乎看不出来。 */
      var cs = getComputedStyle(seg);
      var padT = parseFloat(cs.paddingTop) || 0;
      var bl = seg.clientLeft || 0, bt = seg.clientTop || 0;
      var r = btn.getBoundingClientRect(), pr = seg.getBoundingClientRect();
      thumb.style.width = r.width + "px";
      thumb.style.height = Math.max(0, seg.clientHeight - padT * 2) + "px";
      thumb.style.transform = "translate(" + (r.left - pr.left - bl) + "px," + (r.top - pr.top - bt) + "px)";
    };

    seg.addEventListener("click", function (e) {
      var b = e.target.closest("button");
      if (!b) return;
      $$("button", seg).forEach(function (x) { x.setAttribute("aria-selected", x === b ? "true" : "false"); });
      ctl.place(b);
      if (onPick) onPick(b);
    });
    var cur = $('button[aria-selected="true"]', seg) || $("button", seg);
    if (cur) { cur.setAttribute("aria-selected", "true"); ctl.place(cur); }
  }
  function placeAllSegs() { segs.forEach(function (c) { c.place($('button[aria-selected="true"]', c.seg)); }); }

  /* ---------------------------------------------------------- 主题 / 动效 */
  function placeSegThumb(segId, thumbId, sel) {
    var seg = document.getElementById(segId);
    var ctl = null;
    for (var i = 0; i < segs.length; i++) if (segs[i].seg === seg) { ctl = segs[i]; break; }
    var b = $(sel, seg);
    if (ctl) ctl.place(b);
  }
  function setTheme(t, persist) {
    document.documentElement.setAttribute("data-theme", t);
    var label = t === "dark" ? "亮色" : "暗色";
    var tip = t === "dark" ? "切换到亮色" : "切换到暗色";
    // 图标跟着目标状态走：暗色下显示太阳（点了会变亮），反之显示月亮
    var icon = t === "dark" ? "i-sun" : "i-moon";
    ["themeLabel", "themeLabel2"].forEach(function (id) {
      var el = document.getElementById(id); if (el) el.textContent = label;
    });
    ["themeToggle", "themeToggle2"].forEach(function (id) {
      var el = document.getElementById(id);
      if (!el) return;
      el.title = tip;
      var u = el.querySelector("use");
      if (u) u.setAttribute("href", "#" + icon);
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
  function setRing(el, pct, txt) {
    // 通用圆环：pct 0~100；txt 缺省显示百分比。写错元素时静默跳过。
    if (!el) return;
    var c = 2 * Math.PI * 24;
    var p = Math.max(0, Math.min(100, Number(pct) || 0));
    var fg = $(".fg", el);
    if (fg) fg.style.strokeDashoffset = (c * (1 - p / 100)).toFixed(1);
    var lbl = $(".pct", el);
    if (lbl) lbl.textContent = txt != null ? txt : Math.round(p) + "%";
    el.classList.toggle("warn", p >= 85);
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
    updateVramEstimate(st);
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

    /* ---- 系统负载四环（CPU / GPU 利用率 / 内存 / 显存）——替代旧 SIGNAL 波形动画 ---- */
    var sy = st.sys || {};
    setRing($("#cpuRing"), sy.cpu_pct || 0);
    if ($("#cpuTxt")) $("#cpuTxt").textContent = (sy.cpu_pct || 0) + "%";
    var ramPct = sy.ram_total_mb ? Math.round(sy.ram_used_mb / sy.ram_total_mb * 100) : 0;
    setRing($("#ramRing"), ramPct);
    if ($("#ramTxt")) $("#ramTxt").textContent = sy.ram_total_mb
      ? (Math.round(sy.ram_used_mb / 1024) + " / " + Math.round(sy.ram_total_mb / 1024) + " GB") : "—";
    setRing($("#gpuUtilRing"), g.util || 0);
    if ($("#gpuUtilTxt")) $("#gpuUtilTxt").textContent = (g.util || 0) + "%";
    setRing($("#gpuRing"), gpuPct);
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
    // 显示**已保存设置**里的端口，而不是运行时状态（未启动时 d.port 恒为默认值，
    // 会把用户改过的端口刷回 8899，点「启动」就把错值写回设置了）
    if (document.activeElement !== $("#dlnaPort")) $("#dlnaPort").value = (S.settings && S.settings.dlna_port) || d.port || 8899;
    $("#dlnaUrl").value = d.url || "—";
    setBadge($("#rootsBadge"), "", (d.roots || []).length + " 个");
    renderRoots(d.roots || []);
    renderDlnaLogs(d.logs || []);

    /* ---- 字幕页 ---- */
    setBadge($("#subBadge"), sub.status === "ready" ? "ok" : sub.status === "loading" ? "warn" : sub.status === "error" ? "err" : "",
      sub.status === "ready" ? "就绪" : sub.status === "loading" ? "加载中" : sub.status === "error" ? "错误" : "已停止");
    $("#subSub").textContent = sub.status === "ready"
      ? ("运行中 · PID " + sub.pid)
      : sub.status === "loading" ? "正在加载模型…"
      : sub.status === "error" ? (sub.error || "异常")
      : "未启动";
    $("#subLoadBar").style.display = sub.status === "loading" ? "" : "none";
    var h = sub.health || {};
    $("#subModel").textContent = h.asr_model ? String(h.asr_model).split(/[\\/]/).pop() : "—";
    $("#subDevice").textContent = h.device || "—";
    $("#subMt").textContent = h.translate_backend || "—";
    /* 空闲回收透明化：让"服务为什么自己停了"看得见——最近一次识别请求的时间
       与自动回收规则（头显退出字幕不通知 PC，回收靠服务端空闲计时） */
    var idleEl = $("#subIdle");
    if (idleEl) {
      var idleMin = h.idle_release_min;
      var txt = (idleMin != null && idleMin > 0)
        ? ("空闲 " + idleMin + " 分钟无识别请求即自动回收显存")
        : "空闲回收：已关闭";
      if (h.last_req_ts) {
        var ago = Math.max(0, Math.floor(Date.now() / 1000 - h.last_req_ts));
        txt += " · 最近活动 " + (ago < 60 ? ago + " 秒前" : Math.floor(ago / 60) + " 分钟前");
      }
      idleEl.textContent = txt;
    }
    // 当前实际生效的识别引擎（/health 的 asr_backend）。显示"实际"而不是"配置"：
    // 选了 Whisper 但模型缺失回落 Qwen3 时，这里必须能看出来（与选择器不一致即异常）
    if ($("#subAsr")) {
      var ab = String(h.asr_backend || "").toLowerCase();
      $("#subAsr").textContent = ab === "whisper" ? "Whisper（kotoba）"
        : ab === "audiocpp" ? "Qwen3（audio.cpp）" : (h.asr_backend || "—");
    }

    /* 8756 上挂着别人的服务（上次强杀宿主留下的残留）：必须显式告警。
       它加载的是启动时的旧配置，用户改了模型/后端却不生效，光看界面完全看不出来。 */
    var fn = $("#subForeignNotice");
    if (fn) {
      var fp = sub.foreignPid;
      fn.style.display = fp ? "" : "none";
      if (fp) $("#subForeignMsg").textContent = "字幕服务被残留的旧进程占用（PID " + fp + "），点「结束并重启」恢复";
    }

    /* ---- 设备同步页 ---- */
    renderSync(st.sync || {});

    /* ---- 设置页 ---- */
    $("#aboutIp").textContent = (st.host && st.host.lan_ip) || "—";
    $("#aboutPort").textContent = (st.host && st.host.port) || "—";
    $("#aboutLanApi").textContent = (st.host && st.host.lan_api_url) || "—";
    var verTxt = "v" + (st.version || "—") + (st.dev_copy ? " · 开发副本" : "") + " · WebView2";
    $("#verLine").textContent = verTxt;
    var brandSub = $("#brandSub");
    if (brandSub) brandSub.textContent = st.dev_copy ? "开发副本" : "集成版";
    syncSettingsUI();
  }

  var lastRootsSig;
  function renderRoots(roots) {
    // 与日志面板同一套签名去重：每秒无条件重建 innerHTML 会清掉用户正在做的
    // 框选/复制；删除按钮此前按下标定位，与渲染快照强耦合（这 1s 内列表一变，
    // 删掉的可能不是用户看到的那条）——改成携带路径值，去列表里现找。
    var sig = roots.join("\u0001");
    if (sig === lastRootsSig) return;
    lastRootsSig = sig;
    var box = $("#rootList");
    if (!roots.length) { box.innerHTML = '<div class="empty">还没有媒体根目录</div>'; return; }
    box.innerHTML = roots.map(function (p) {
      return '<div class="row"><svg class="ic muted"><use href="#i-folder"/></svg>' +
        '<div class="grow"><div class="name mono" style="font-size:12px">' + esc(p) + "</div></div>" +
        '<span class="badge acc">已启用</span>' +
        '<button class="icon-btn del" data-del-root="' + esc(p) + '" title="移除"><svg class="ic"><use href="#i-trash-2"/></svg></button></div>';
    }).join("");
  }

  var lastDlnaLogSig = "";
  function renderDlnaLogs(logs) {
    // 签名用「长度+最后一条内容」而不是只看长度：服务端日志封顶后长度恒定，
    // 旧判据会让界面永久冻结在封顶前的那一批旧日志上。
    var sig = logs.length + "|" + (logs[logs.length - 1] || "");
    if (sig === lastDlnaLogSig) return;
    lastDlnaLogSig = sig;
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

    if (!busyEditing($("#syncAdbPath"))) $("#syncAdbPath").value = sy.adb_path || "";
    if (!busyEditing($("#syncForce"))) $("#syncForce").checked = !!sy.force_full;
    if (!busyEditing($("#syncDelete"))) $("#syncDelete").checked = !!sy.delete_extra;

    var slots = sy.slots || {};
    ["script", "video"].forEach(function (kind) {
      var s = slots[kind] || {};
      var cap = kind === "script" ? "Script" : "Video";
      var badge = $("#sync" + cap + "Badge");
      setBadge(badge, s.busy ? "warn" : s.result ? "ok" : s.error ? "err" : "",
        s.busy ? "同步中" : s.result ? "已完成" : s.error ? "失败" : "待命");
      var local = $("#sync" + cap + "Local"), dev = $("#sync" + cap + "Device");
      if (local && !busyEditing(local)) local.value = s.local_folder || "";
      if (dev && !busyEditing(dev)) dev.value = s.device_folder || "";
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
  var lastSyncLogSig;
  function renderSyncLogs(logs) {
    // 同 renderDlnaLogs：封顶后长度恒定，签名必须带上最后一条内容
    var sig = logs.length + "|" + (logs[logs.length - 1] || "");
    if (sig === lastSyncLogSig) return;
    lastSyncLogSig = sig;
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
    lastSyncLogSig = undefined;   // 强制重绘（类型切换/新一轮同步）
    var cap = kind === "script" ? "Script" : "Video";
    var btn = $("#syncRun" + cap);
    if (btn) btn.disabled = true;
    // 先把这一类别的路径/开关落盘，再发起同步：服务端是按**已保存设置**取的目录，
    // 不等待就 /api/sync/run 会用上一次的旧路径去同步（用户看着新路径、实际同步旧目录）。
    var ids = ["sync" + cap + "Local", "sync" + cap + "Device", "syncAdbPath",
               "syncForce", "syncDelete"];
    flushSettings(ids).then(function () {
      // 有保存失败的项就别开工：此时屏幕上的值和真正会执行的目录不一致，
      // 硬跑下去等于"按用户没确认过的路径"同步（甚至删除多余文件）。
      var stuck = ids.filter(function (id) { return DIRTY[id] === true; });
      if (stuck.length) {
        if (btn) btn.disabled = false;
        toast("设置未保存成功", "路径可能无效，请确认后再同步", "err");
        return;
      }
      api("/api/sync/run", "POST", { kind: kind }).then(function (r) {
        if (!r.ok) {
          if (btn) btn.disabled = false;
          toast("无法开始同步", r.error || "", "err");
          return;
        }
        toast("开始同步", kind === "script" ? "脚本" : "视频");
        poll(true);
      });
    });
  }

  function syncSettingsUI() {
    var s = S.settings;
    // 开关也要过护栏：点一下 → change 发 POST → 在它返回之前，本轮 /api/state
    // 带回来的还是旧值，无护栏回填会让开关"自己弹回去"再跳回来。
    if (!busyEditing($("#setDlnaAuto"))) $("#setDlnaAuto").checked = !!s.dlna_auto_start;
    if (!busyEditing($("#setSubAuto"))) $("#setSubAuto").checked = !!s.subtitle_auto_start;
    if (!busyEditing($("#setCloseTray"))) $("#setCloseTray").checked = !!s.close_to_tray;
    if (!busyEditing($("#setStartMin"))) $("#setStartMin").checked = !!s.start_minimized;
    // 注意：这里**不能**碰 #mtModel —— 它的值只归 loadSubtitleConfig 填。
    // 旧代码每秒轮询都把输入框清空，用户点保存（mousedown 已失焦）时读到的
    // 就是空串，把配置里已保存的模型名覆盖成 ""。
    // 首次拉到设置后应用持久化的主题 / 动画强度
    if (!S.themeApplied) {
      S.themeApplied = true;
      if (s.theme) setTheme(s.theme, false);
      if (s.motion) setMotion(s.motion, false);
    }
  }

  /* ---------------------------------------------------------- 字幕配置 / 术语表 */
  /* 本地翻译模型下拉：列表来自 /api/subtitle/models（安装目录 models\ 下的 GGUF）。
     进程内缓存一份；当前配置值不在列表里（手动改过 config）时补一个「当前」项，
     绝不静默改掉用户的配置。 */
  /* 显存估算（R66）：服务端按当前档位估算 + 宿主 NVML 实际空闲，给出推荐提示 */
  function updateVramEstimate(st) {
    var el = $("#vramEstimate");
    if (!el) return;
    var ve = (st && st.subtitle && st.subtitle.health && st.subtitle.health.vram_estimate) || null;
    var gpu = (st && st.gpu) || {};
    if (!ve || !ve.total_mb) { el.textContent = ""; return; }
    var totalGb = (ve.total_mb / 1024).toFixed(1);
    var freeMb = gpu.total_mb ? Math.max(0, gpu.total_mb - gpu.used_mb) : 0;
    var freeGb = (freeMb / 1024).toFixed(1);
    var tip = "预计显存占用 ≈ " + totalGb + " GB（转录 " + (ve.asr_mb / 1024).toFixed(1)
      + " + 翻译 " + (ve.mt_active_mb / 1024).toFixed(1) + " + 运行时）；显存共 "
      + ((gpu.total_mb || 0) / 1024).toFixed(1) + " GB，当前空闲约 " + freeGb + " GB。";
    if (freeMb >= ve.total_mb) tip += " 当前档位可流畅运行。";
    else tip += " ⚠ 空闲显存低于该档位预估，建议降低转录/翻译档位。";
    el.textContent = tip;
  }

  function renderLocalModelSelect(current, currentEn) {
    var sel = $("#mtLocalModel");
    var selEn = $("#mtLocalModelEn");
    if (!sel) return;
    function paint(models) {
      function fill(select, cur) {
        select.innerHTML = "";
        (models || []).forEach(function (m) {
          var o = document.createElement("option");
          o.value = m.path;
          o.textContent = m.name;
          select.appendChild(o);
        });
        if (cur && !(models || []).some(function (m) { return m.path === cur; })) {
          var o = document.createElement("option");
          o.value = cur;
          o.textContent = "（当前）" + String(cur).split("/").pop().replace(/\.gguf$/i, "");
          select.appendChild(o);
        }
        select.value = cur || (models && models[0] ? models[0].path : "");
      }
      fill(sel, current);
      fill(selEn, currentEn);
      var st = S.lastState;
      updateVramEstimate(st);
    }
    if (S.localModels) { paint(S.localModels); return; }
    api("/api/subtitle/models").then(function (r) {
      S.localModels = (r && r.ok) ? (r.models || []) : [];
      paint(S.localModels);
    });
  }

  /* ---------- 模型下载：识别 / 翻译模型缺什么下什么（走 hf-mirror，宿主负责） ---------- */
  var MODEL_NAMES = {
    "qwen3-asr-0.6b": "识别模型 · Qwen3-ASR-0.6B（显存约 1.3GB）",
    "qwen3-asr-1.7b": "识别模型 · Qwen3-ASR-1.7B（显存约 3.6GB，转录质量更高）",
    "sakura-7b": "翻译模型 · Sakura-7B（日语，显存约 4.4GB，翻译质量更好）",
    "sakura-1.5b": "翻译模型 · Sakura-1.5B（日语，显存约 1.4GB，低显存推荐）",
    "hymt2-7b": "翻译模型 · Hy-MT2-7B（英语，显存约 4.7GB，翻译质量更好）",
    "hymt2-1.8b": "翻译模型 · Hy-MT2-1.8B（英语，显存约 1.2GB，低显存推荐）"
  };
  var modelPollTimer = 0;
  function loadModels() {
    api("/api/models/catalog").then(function (r) {
      if (!r || !r.ok) return;
      var box = $("#modelDl");
      if (!box) return;
      box.innerHTML = "";
      var busy = false;
      (r.items || []).forEach(function (m) {
        var stTxt, btnTxt = "下载", canDl = false;
        if (m.state === "downloading") { stTxt = "下载中 " + (m.pct || 0) + "%"; busy = true; }
        else if (m.state === "error") { stTxt = "下载失败：" + (m.error || "未知错误"); btnTxt = "重试"; canDl = true; }
        else if (m.installed) { stTxt = "已安装"; }
        else { stTxt = "未安装 · 约 " + (m.size_gb || "?") + " GB"; canDl = true; }

        var row = document.createElement("div");
        row.className = "row flush";
        var ig = document.createElement("div");
        ig.className = "grow";
        var nm = document.createElement("div");
        nm.className = "name";
        nm.textContent = MODEL_NAMES[m.id] || m.label || m.id;
        var st = document.createElement("div");
        st.className = "sub";
        st.textContent = stTxt;
        ig.appendChild(nm); ig.appendChild(st);
        row.appendChild(ig);
        if (canDl) {
          var b = document.createElement("button");
          b.className = "btn ghost";
          b.textContent = btnTxt;
          b.addEventListener("click", function () {
            b.disabled = true;
            api("/api/models/download", "POST", { id: m.id }).then(function (rr) {
              if (rr && rr.ok) { loadModels(); }
              else { b.disabled = false; toast("下载失败", (rr && rr.error) || "", "err"); }
            });
          });
          row.appendChild(b);
        }
        box.appendChild(row);

        // 翻译模型下载完成 → 让本地模型下拉重新枚举（新模型立即可选）
        if (m.state === "done" && m.role === "translate" && !S.modelDoneSeen) S.modelDoneSeen = {};
        if (m.state === "done" && m.role === "translate" && !S.modelDoneSeen[m.id]) {
          S.modelDoneSeen[m.id] = true;
          S.localModels = null;
          loadSubtitleConfig();
        }
      });
      clearTimeout(modelPollTimer);
      if (busy) modelPollTimer = setTimeout(loadModels, 1500);
    });
  }

  function loadSubtitleConfig() {
    api("/api/subtitle/config").then(function (r) {
      if (!r.ok) return;
      var c = r.config || {};
      var asr = c.asr || {}, tr = c.translate || {};
      /* 识别引擎：归一到 select 的两个取值（服务端另认 faster-whisper/kotoba/cpp/ggml 别名）。
         兜底 audiocpp 与 index.html 首项一致——配置缺 backend 时不能把选择器甩到另一项。
         其余识别参数（模型路径/设备/分段/VAD）不进界面：属内部调优项，留在 config.json */
      var rawAb = String(asr.backend || "audiocpp").toLowerCase();
      var asrSel = $("#asrBackend");
      asrSel.value = "audiocpp";   // R65：whisper 转录已剔除，引擎仅 audiocpp
      S.asrBackendPrev = asrSel.value;   // 引擎即改即存，失败时弹回这个值
      /* 识别模型档位（R66）：audiocpp.model 路径含 1.7B 即高精度档 */
      var ac = asr.audiocpp || {};
      var tier = String(ac.model || "").indexOf("1.7B") >= 0 ? "1.7b" : "0.6b";
      var tierSel = $("#asrModelTier");
      if (tierSel && !busyEditing(tierSel)) tierSel.value = tier;
      // 兜底必须与 index.html 里 <select> 的首项一致（local）。写成 "ollama" 的话，
      // 配置里 backend 为空时会把选择器指向 Ollama，用户一保存就把后端切成
      // 本地根本没在跑的 Ollama（翻译整条挂掉）。
      $("#mtBackend").value = tr.backend || "local";
      var ollama = tr.ollama || {};
      $("#mtModel").value = ollama.model || "";
      $("#mtBase").value = ollama.base_url || "";
      /* 本地 llama.cpp：下拉选模型（数据来自 /api/subtitle/models）。
         英语模型（model_by_lang.en）必须回填进第二个下拉——此前从不回填，
         保存时会把下拉的"列表首项回退值"当成用户选择写进 model_by_lang.en，
         英语片从此被路由到错误模型。同时记下配置里 en 的原始状态（有没有非空值）
         与"用户动过英语下拉没有"，保存时据此决定写不写 model_by_lang。 */
      var loc = tr.local || {};
      var enFromCfg = (loc.model_by_lang || {}).en;
      /* 空串按"没配"算：translate_engine 的 or 链同样把空串当缺失走回退；
         把空串当"已有"会让二次保存把下拉首项回退值写进 en，复现 F10。 */
      S.mtLocalEnFromCfg = enFromCfg ? String(enFromCfg) : null;
      S.mtLocalEnTouched = false;
      renderLocalModelSelect(loc.model || "", S.mtLocalEnFromCfg || "");
      /* 云端：标准 OpenAI 兼容（base_url + model + key），key 不回明文，只显示尾号 */
      var oa = tr.openai || {};
      $("#mtCloudBase").value = oa.base_url || "";
      $("#mtCloudModel").value = oa.model || "";
      $("#mtCloudKey").value = "";
      /* 空闲回收显存（server.idle_release_min，分钟；0=永不）。配置缺这个键时
         按发运默认 5 分钟回填，不能让下拉停在第一项假装是用户选的 */
      var idleSel = $("#subIdleRelease");
      if (idleSel && !busyEditing(idleSel)) {
        var idleVal = (c.server || {}).idle_release_min;
        idleVal = (idleVal == null ? 5 : Number(idleVal));
        idleSel.value = String(idleVal);
        if (idleSel.selectedIndex < 0 || idleSel.value !== String(idleVal)) {
          // 配置里的值不在预设档位（手改过 config）：如实显示成一个额外选项
          var opt = document.createElement("option");
          var label = idleVal === 0 ? "永不" : (idleVal < 1 ? (idleVal * 60) + " 秒" : idleVal + " 分钟");
          opt.value = String(idleVal);
          opt.textContent = label;
          idleSel.appendChild(opt);
          idleSel.value = String(idleVal);
        }
        S.subIdlePrev = idleSel.value;   // 保存失败时弹回基准
      }
      syncMtGroups();
    });
  }
  /* 三套后端（本地 llama.cpp / 云端 OpenAI 兼容 / 本地 Ollama）参数完全不同，
     平铺在一起既乱又容易填错 —— 只显示当前选中的那一组。 */
  function syncMtGroups() {
    var sel = $("#mtBackend").value;
    Array.prototype.forEach.call(document.querySelectorAll("[data-mt-group]"), function (g) {
      g.style.display = (g.getAttribute("data-mt-group") === sel) ? "" : "none";
    });
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
    /* 添加媒体根：走 /api/dlna/roots —— 它会顺手校验路径是否存在。
       不校验的话，路径写错只表现为"头显里那个文件夹是空的"，猜不到原因。 */
    function addRoots(list) {
      var roots = (S.settings.dlna_roots || []).slice();
      var added = 0, dup = 0;
      (list || []).forEach(function (p) {
        p = String(p == null ? "" : p).trim();
        if (!p) return;
        if (roots.indexOf(p) >= 0) { dup++; return; }
        roots.push(p); added++;
      });
      if (!added) { toast(dup ? "这些目录已经在列表里了" : "请先选择或输入目录", "", "warn"); return; }
      api("/api/dlna/roots", "POST", { roots: roots }).then(function (r) {
        /* 保存失败必须可见：服务端写盘失败回 ok:false，此时不清输入框、不报成功
           （网络失败由 api() 统一 toast）。与 saveSetting 的处理同口径。 */
        if (!r || r.ok === false) {
          if (r && r.error) toast("媒体根目录保存失败", r.error, "err");
          return;
        }
        $("#newRoot").value = "";
        if (r.missing && r.missing.length) {
          toast("已添加，但这些目录不存在", r.missing.join("；"), "err");
        } else {
          toast("已添加 " + added + " 个媒体根目录");
        }
        // DLNA 运行中改媒体根不会热重载（媒体库是启动期构建的），必须明示
        // "要重启才生效"，否则用户只会在头显里看到"文件夹还是空的"。
        if (r.need_restart) {
          toast("需重启 DLNA 生效", r.restart_hint || "运行中的 DLNA 不会自动加载新目录", "warn");
        }
        poll(true);
      });
    }
    $("#addRoot").addEventListener("click", function () { addRoots([$("#newRoot").value]); });
    /* 系统「选择文件夹」：主入口。手打路径容易带进引号/全角字符，
       而那种错误在头显里只表现为"目录为空"。支持一次多选。 */
    $("#pickRoot").addEventListener("click", function () {
      if (!bridgeReady()) return;
      window.pywebview.api.pick_folder($("#newRoot").value || "", true).then(function (r) {
        if (!r || !r.ok) {
          if (r && r.error) toast("打开选择器失败", r.error, "err");
          return;
        }
        addRoots(r.paths || [r.path]);
      });
    });
    $("#rootList").addEventListener("click", function (e) {
      var b = e.target.closest("[data-del-root]");
      if (!b) return;
      // 按**路径值**删除，不再按下标：下标只在"渲染那一刻"与列表对齐，
      // 列表一旦在两次轮询之间变化，就会删错条目。
      var p = b.getAttribute("data-del-root");
      var roots = (S.settings.dlna_roots || []).slice();
      var i = roots.indexOf(p);
      if (i < 0) { toast("该目录已不在列表中", p || "", "warn"); poll(true); return; }
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
      api("/api/subtitle/stop", "POST", {}).then(function (r) {
        r = r || {};
        if (r.stopped) toast("字幕服务已停止", "模型内存已释放");
        else toast("字幕服务本来就没在运行", r.error || "", "warn");
        poll(true);
      });
    });
    $("#mtBackend").addEventListener("change", syncMtGroups);
    $("#saveMt").addEventListener("click", function () {
      /* model_by_lang.en：配置里本来就有 → 无条件回写当前选择；配置里没有 →
         只有用户真的动过英语下拉才写。否则会把 fill() 的"空值回退到列表首项"
         当成用户选择存进配置，把英语路由覆盖成错误模型（F10）。 */
      var localSave = { model: $("#mtLocalModel").value };
      if (S.mtLocalEnFromCfg != null || S.mtLocalEnTouched) {
        localSave.model_by_lang = { en: $("#mtLocalModelEn").value };
      }
      var body = {
        translate: {
          backend: $("#mtBackend").value,
          local: localSave,
          openai: {
            base_url: $("#mtCloudBase").value,
            model: $("#mtCloudModel").value
          },
          ollama: { model: $("#mtModel").value, base_url: $("#mtBase").value }
        }
      };
      /* key 只在真的填了才提交（留空表示沿用已保存的，避免把掩码写回配置） */
      var k = $("#mtCloudKey").value.trim();
      if (k) body.translate.openai.api_key = k;
      api("/api/subtitle/config", "POST", body).then(function (r) {
        if (r.ok) {
          toast("翻译设置已保存", "字幕服务正在重启以加载新模型", "ok");
          restartSubForConfig();
        } else {
          toast("保存失败", r.error || "", "err");
        }
      });
    });
    $("#testMt").addEventListener("click", function () {
      var box = $("#mtTestResult");
      box.style.display = "";
      box.className = "notice top-3";
      box.textContent = "测试中…（云端首字可能要十几秒）";
      api("/api/subtitle/translate-test", "POST", {}).then(function (r) {
        r = r || {};
        var lines = [(r.ok ? "✅ 通过" : "❌ 未通过") +
          "  后端=" + (r.backend || "?") +
          "  用时=" + (r.ms != null ? r.ms + "ms" : "?")];
        if (r.describe) lines.push("配置：" + r.describe);
        if (r.text) lines.push("原文：" + r.text);
        if (r.raw) lines.push("直连译文：" + r.raw);
        if (r.raw_error) lines.push("直连错误：" + r.raw_error);
        if (r.pipeline) lines.push("管线译文：" + r.pipeline);
        if (r.pipeline_error) lines.push("管线错误：" + r.pipeline_error);
        if (r.error) lines.push("错误：" + r.error);
        box.className = "notice top-3" + (r.ok ? "" : " warn");
        box.innerHTML = lines.map(function (s) { return "<div>" + esc(s) + "</div>"; }).join("");
      });
    });
    /* 识别引擎即改即存（选错自动弹回）；其余识别参数不进界面 */
    $("#asrModelTier").addEventListener("change", function () {
      var sel = this, tierPath = sel.value === "1.7b"
        ? "../../models/Qwen3-ASR-1.7B" : "../../models/Qwen3-ASR-0.6B";
      api("/api/subtitle/config", "POST", { asr: { audiocpp: { model: tierPath } } }).then(function (r) {
        if (r.ok) {
          toast("识别模型已保存", "字幕服务正在重启以加载 " + (sel.value === "1.7b" ? "1.7B" : "0.6B"), "ok");
          restartSubForConfig();
        } else {
          toast("保存失败", r.error || "", "err");
        }
      });
    });
    $("#asrBackend").addEventListener("change", function () {
      var sel = this, prev = S.asrBackendPrev || sel.value;
      api("/api/subtitle/config", "POST", { asr: { backend: sel.value } }).then(function (r) {
        if (r.ok) {
          S.asrBackendPrev = sel.value;
          toast("识别引擎已保存", "字幕服务正在重启以切换引擎", "ok");
          restartSubForConfig();
        } else {
          sel.value = prev;
          toast("保存失败", r.error || "", "err");
        }
      });
    });
    /* 空闲回收显存：改完即时生效（字幕服务在跑就自动重启加载新时长） */
    $("#subIdleRelease").addEventListener("change", function () {
      var sel = this, prev = S.subIdlePrev != null ? S.subIdlePrev : "5";
      var v = Number(sel.value);
      api("/api/subtitle/config", "POST", { server: { idle_release_min: v } }).then(function (r) {
        if (r.ok) {
          S.subIdlePrev = sel.value;
          toast("已保存", v > 0 ? ("空闲 " + (v < 1 ? (v * 60) + " 秒" : v + " 分钟") + "自动回收显存")
                              : "空闲回收已关闭", "ok");
          restartSubForConfig();
        } else {
          sel.value = prev;
          toast("保存失败", r.error || "", "err");
        }
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
      markDirty(this);
      saveSetting("syncAdbPath", "adb_path", this.value.trim());
    });
    $("#syncForce").addEventListener("change", function () {
      markDirty(this);
      saveSetting("syncForce", "sync_force_full", this.checked);
    });
    $("#syncDelete").addEventListener("change", function () {
      markDirty(this);
      saveSetting("syncDelete", "sync_delete_extra", this.checked);
      if (this.checked) toast("将删除设备上多余文件", "请确认设备目录正确", "warn");
    });
    $("#syncScriptLocal").addEventListener("change", function () {
      markDirty(this);
      saveSetting("syncScriptLocal", "script_folder", this.value.trim());
    });
    $("#syncVideoLocal").addEventListener("change", function () {
      markDirty(this);
      saveSetting("syncVideoLocal", "video_folder", this.value.trim());
    });
    $("#syncScriptDevice").addEventListener("change", function () {
      markDirty(this);
      saveSetting("syncScriptDevice", "device_folder_script", this.value.trim());
    });
    $("#syncVideoDevice").addEventListener("change", function () {
      markDirty(this);
      saveSetting("syncVideoDevice", "device_folder_video", this.value.trim());
    });
    initSeg("syncLogSeg", "syncLogThumb", function (btn) {
      SY.kind = btn.getAttribute("data-kind");
      lastSyncLogSig = undefined;
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

    /* 设置：change 时先标 dirty（落盘成功才清），避免"点了又弹回去" */
    $("#setDlnaAuto").addEventListener("change", function () {
      markDirty(this);
      saveSetting("setDlnaAuto", "dlna_auto_start", this.checked);
    });
    $("#setSubAuto").addEventListener("change", function () {
      markDirty(this);
      saveSetting("setSubAuto", "subtitle_auto_start", this.checked);
    });
    $("#setCloseTray").addEventListener("change", function () {
      markDirty(this);
      saveSetting("setCloseTray", "close_to_tray", this.checked);
    });
    $("#setStartMin").addEventListener("change", function () {
      markDirty(this);
      saveSetting("setStartMin", "start_minimized", this.checked);
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
    $("#subReclaim").addEventListener("click", function () {
      api("/api/subtitle/reclaim", "POST", {}).then(function (r) {
        if (r.nothing) { toast("没有发现残留服务", "", "warn"); poll(true); return; }
        toast(r.ok ? "已结束残留服务" : "回收失败",
              r.error || (r.killed ? "PID " + r.killed + " 已结束，正在按当前配置重启" : ""),
              r.ok ? "ok" : "err");
        poll(true);
      });
    });
    $("#qaSubStop").addEventListener("click", function () {
      api("/api/subtitle/stop", "POST", {}).then(function (r) {
        r = r || {};
        if (r.stopped) toast("字幕服务已停止", "模型内存已释放");
        else toast("字幕服务本来就没在运行", r.error || "", "warn");
        poll(true);
      });
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
    btn.innerHTML = '<svg class="ic"><use href="#i-circle-check"/></svg>';
    btn.style.color = "var(--ok)";
    setTimeout(function () { btn.innerHTML = old; btn.style.color = ""; }, 1200);
  }

  /* 字幕设置改动后自动重启字幕服务（R55）：模型/引擎都是启动期加载的常驻进程，
     只保存不重启的话界面显示"已切换"、实际还跑着旧模型（实测 7B→1.5B 不重启
     显纹丝不动，用户以为切了）。没在跑就不用重启，下次启动自然用新配置。 */
  function restartSubForConfig() {
    var st = (S.state && S.state.subtitle || {}).status;
    if (st !== "ready" && st !== "loading" && st !== "error") return;
    api("/api/subtitle/stop", "POST", {}).then(function () {
      setTimeout(function () {
        api("/api/subtitle/start", "POST", {}).then(function () { poll(true); });
      }, 800);   // 等端口完全释放（stop 的 taskkill 是同步的，留点余量）
    });
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
      if (st && st.ok) {
        // 渲染异常绝不能吃掉后面的重排定时器：此前 render 抛一次异常，整条轮询链
        // 就永久停摆（数值冻结在最后一帧，只有切标签页或点按钮才能救回来）。
        try {
          render(st);
        } catch (e) {
          console.error("render 失败（已跳过本帧，轮询继续）", e);
        }
      }
    });
    if (once) return;
    clearTimeout(S.pollTimer);
    var delay = document.hidden ? 5000 : 1000;
    S.pollTimer = setTimeout(function () { poll(false); }, delay);
  }
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(true); });

  /* ---------------------------------------------------------- 启动 */
  /* ================================================================
     画布层：指针环境光（事件驱动，无常驻帧循环）
     ================================================================ */

  /* 指针光晕。位置写进 CSS 变量，并做插值跟随——直接把鼠标坐标赋进去的话
     快速移动时是一格一格跳的，插值后才有"光被拖着走"的手感。
     ⚠️ 事件驱动而不是常驻 RAF：只在 pointermove 后跑一个 ≤350ms 的收尾突发，
     光追上鼠标就停帧。旧实现挂着 60fps 永动循环（当时还要画 SIGNAL 波形），
     波形删除后纯空转，白烧 CPU/GPU。 */
  var ptr = { tx: 0, ty: 0, x: 0, y: 0, on: false, raf: 0, until: 0 };
  function initPointerLight() {
    if (!$("#pointerLight")) return;
    window.addEventListener("pointermove", function (e) {
      ptr.tx = e.clientX; ptr.ty = e.clientY;
      if (!ptr.on) { ptr.on = true; ptr.x = ptr.tx; ptr.y = ptr.ty; document.body.classList.add("ptr"); }
      ptr.until = performance.now() + 350;
      if (!ptr.raf) ptr.raf = requestAnimationFrame(lightLoop);
    });
    window.addEventListener("pointerleave", function () {
      ptr.on = false; document.body.classList.remove("ptr");
    });
  }
  function lightLoop(now) {
    ptr.raf = 0;
    if (!ptr.on || motionOff()) return;
    stepPointerLight();
    var settled = Math.abs(ptr.tx - ptr.x) < 0.5 && Math.abs(ptr.ty - ptr.y) < 0.5;
    if (settled && now > ptr.until) return;   // 追上了且过了突发窗口 → 停帧
    ptr.raf = requestAnimationFrame(lightLoop);
  }
  function stepPointerLight() {
    ptr.x += (ptr.tx - ptr.x) * 0.13;
    ptr.y += (ptr.ty - ptr.y) * 0.13;
    var s = document.documentElement.style;
    s.setProperty("--mx", ptr.x.toFixed(1) + "px");
    s.setProperty("--my", ptr.y.toFixed(1) + "px");
  }

  function boot() {
    bind();
    setTheme("dark", false);
    setMotion("full", false);
    initPointerLight();
    poll(false);
    loadSubtitleConfig();
    loadModels();
    // 2.5s 后补拉一次字幕配置（防首次请求早于服务就绪），但用户已经开始改
    // 配置输入框时不要覆盖他的输入
    setTimeout(function () { if (!S.subCfgDirty) loadSubtitleConfig(); }, 2500);
    // 配置输入一旦被用户动过就标记：之后的自动回填一律让路
    ["asrBackend", "mtBackend", "mtModel", "mtBase", "mtLocalModel", "mtLocalModelEn", "mtCloudBase", "mtCloudModel", "mtCloudKey", "subIdleRelease"
    ].forEach(function (id) {
      var el = document.getElementById(id);
      if (el) el.addEventListener("input", function () {
        S.subCfgDirty = true;
        if (id === "mtLocalModelEn") S.mtLocalEnTouched = true;   // 用户亲手选过英语模型
      });
    });
    // 字体是异步落地的，加载完行高会变，指示块与分段滑块要重新对齐
    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(function () {
        moveIndicator(activeNavBtn()); placeAllSegs();
      });
    }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
