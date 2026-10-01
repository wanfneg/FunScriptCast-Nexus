/* ============================================================
   FunScriptCast-Nexus —— 前端逻辑
   与 host_server.py 的 /api/* 通信；状态 1s 轮询（页面隐藏时暂停）
   ============================================================ */
(function () {
  "use strict";

  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var S = { state: null, settings: {}, pollTimer: null, counts: {},
            /* 更新弹窗的三个状态位（F07）：dismissed=本次启动不再提示该版本；
               readyFor=该版本已弹过"可安装"；progDismissed=用户把下载进度弹窗
               收到后台（下载继续，弹窗不再每秒重弹） */
            updDismissed: null, updReadyFor: null, updMode: null, updProgDismissed: false };

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
    if (name === "library") loadLibrary();
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
    if (name === "library") requestAnimationFrame(function () { layoutVl(); });   /* 页面可见后再量尺寸 */
    if (!motionOff()) {
      next.classList.remove("enter");
      void next.offsetWidth;      // 强制重排，让动画能重新触发
      next.classList.add("enter");
    }
    content.scrollTop = 0;
    /* 分段控件与 canvas 在隐藏页里量不到尺寸（getBoundingClientRect 全是 0），
       所以每次页面显示后都要重新摆一次；等一帧让 display 生效。 */
    requestAnimationFrame(function () { placeAllSegs(); });
    requestAnimationFrame(function () { redrawPresetWaves(); });   // 切页后按新尺寸重画
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
    /* localStorage 同步留一份：下次启动靠 <head> 的内联脚本在首屏前套用，
       不等 /api/settings 回来（否则必跳变一次） */
    try { localStorage.setItem("nxs.theme", t); } catch (e) {}
    var label = t === "dark" ? "亮色" : "暗色";
    var tip = t === "dark" ? "切换到亮色" : "切换到暗色";
    // 图标跟着目标状态走：暗色下显示太阳（点了会变亮），反之显示月亮
    var icon = t === "dark" ? "i-sun" : "i-moon";
    ["themeLabel", "themeLabel2"].forEach(function (id) {
      var el = document.getElementById(id); if (el) el.textContent = label;
    });
    ["themeToggle"].forEach(function (id) {
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
    try { localStorage.setItem("nxs.motion", m); } catch (e) {}
    if (persist) api("/api/settings", "POST", { motion: m });
  }

  /* ---------------------------------------------------------- 渲染 */
  // （R103 清理：fmtUptime 死函数已删——全仓无调用方。）
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
    // R103 复核：聚焦中**或 dirty** 都不回填——旧判据只挡聚焦态，用户改完失焦
    // 去（比如）添加媒体根，≤1s 后输入被静默刷回（违背文件头回填不变式）。
    if (!(document.activeElement === $("#dlnaPort") || DIRTY["dlnaPort"]))
      $("#dlnaPort").value = (S.settings && S.settings.dlna_port) || d.port || 8899;
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
        else toast(SY.devices.length ? "发现 " + SY.devices.length + " 台设备" : "未发现设备", "");
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
    var ids = ["sync" + cap + "Local", "sync" + cap + "Device", "syncForce", "syncDelete"];
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
    if (!busyEditing($("#setAutoStart"))) $("#setAutoStart").checked = !!s.launch_on_boot;
    // 注意：这里**不能**碰 #mtModel —— 它的值只归 loadSubtitleConfig 填。
    // 旧代码每秒轮询都把输入框清空，用户点保存（mousedown 已失焦）时读到的
    // 就是空串，把配置里已保存的模型名覆盖成 ""。
    // 首次拉到设置后应用持久化的主题 / 动画强度
    if (!S.themeApplied) {
      S.themeApplied = true;
      if (s.theme) setTheme(s.theme, false);
      if (s.motion) setMotion(s.motion, false);
    }
    renderLibRoots(s.library_roots || []);
  }

  /* 媒体库目录（设置页）：与 DLNA 根目录同一套交互（选择器 + 按路径值删除）。
     保存走 /api/settings 的 library_roots；保存成功后触发一次重扫。 */
  var libRootSig = "";
  function renderLibRoots(roots) {
    var sig = roots.join("");
    if (sig === libRootSig) return;
    libRootSig = sig;
    var box = $("#libRootList");
    if (!box) return;
    if (!roots.length) { box.innerHTML = '<div class="empty">还没有目录</div>'; return; }
    box.innerHTML = roots.map(function (p) {
      return '<div class="row flush"><div class="grow"><div class="name mono">' + p + '</div></div>' +
             '<button class="btn ghost" data-del-libroot="' + p + '">移除</button></div>';
    }).join("");
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
    if (S.localModels && S.localModels.length) { paint(S.localModels); return; }
    api("/api/subtitle/models").then(function (r) {
      // R103 复核：失败不得写缓存（空数组为真值会把下拉毒化整个会话，只剩
      // "（当前）"兜底项且永不重试）——失败置 null，下次调用重新拉取。
      S.localModels = (r && r.ok) ? (r.models || []) : null;
      paint(S.localModels || []);
    });
  }

  /* 识别模型下拉（R72）：选项来自 /api/subtitle/asr-models（服务端扫描 models\
     下支持格式），本地没有的模型不出现在选项里。 */
  function fillAsrModelSelect(current) {
    var sel = $("#asrModel");
    if (!sel) return;
    api("/api/subtitle/asr-models").then(function (r) {
      if (busyEditing(sel)) return;
      var models = (r && r.ok) ? (r.models || []) : [];
      sel.innerHTML = "";
      models.forEach(function (m) {
        var o = document.createElement("option");
        o.value = m.value;
        o.textContent = m.name;
        sel.appendChild(o);
      });
      sel.value = models.some(function (m) { return m.value === current; }) ? current : "";
      S.asrModelPrev = sel.value;
    });
  }

  /* ---------- 模型下载：识别 / 翻译模型缺什么下什么（走 hf-mirror，宿主负责） ---------- */
  var MODEL_NAMES = {
    "audiocpp-cuda": "识别运行时 · CUDA（需 NVIDIA 显卡）",
    "qwen3-asr-0.6b": "识别模型 · Qwen3-ASR-0.6B（显存约 1.3GB）",
    "qwen3-asr-1.7b": "识别模型 · Qwen3-ASR-1.7B（显存约 3.6GB，转录质量更高）",
    "sakura-7b": "翻译模型 · Sakura-7B（日语，显存约 4.4GB，翻译质量更好）",
    "sakura-1.5b": "翻译模型 · Sakura-1.5B（日语，显存约 1.4GB，低显存推荐）",
    "hymt2-7b": "翻译模型 · Hy-MT2-7B（英语，显存约 4.7GB，翻译质量更好）",
    "hymt2-1.8b": "翻译模型 · Hy-MT2-1.8B（英语，显存约 1.2GB，低显存推荐）"
  };
  var modelPollTimer = 0;
  var modelPollFails = 0;
  function loadModels() {
    api("/api/models/catalog").then(function (r) {
      // R103 复核：失败不再直接 return（那会跳过末尾的重排 setTimeout，轮询链
      // 一次失败即永久停摆、下载进度冻结）——连续失败限速重试，成功即恢复。
      if (!r || !r.ok) {
        modelPollFails += 1;
        if (modelPollFails <= 20) modelPollTimer = setTimeout(loadModels, 5000);
        return;
      }
      modelPollFails = 0;
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

        // 下载完成 → 让相关控件同步（新模型立即可选 / GPU 后端自动启用可见）
        if (m.state === "done" && !S.modelDoneSeen) S.modelDoneSeen = {};
        if (m.state === "done" && m.role === "translate" && !S.modelDoneSeen[m.id]) {
          S.modelDoneSeen[m.id] = true;
          S.localModels = null;
          loadSubtitleConfig({ onlyModels: true });   // F08：只刷新下拉，不覆盖用户输入
        }
        if (m.state === "done" && m.role === "asr-runtime" && !S.modelDoneSeen[m.id]) {
          // R96：GPU 运行时下载完，服务端已自动切 cuda——整表刷新让「识别引擎」
          // 下拉补出 GPU 项（loadSubtitleConfig 自带 dirty 护栏，不覆盖用户输入）
          S.modelDoneSeen[m.id] = true;
          loadSubtitleConfig();
        }
      });
      clearTimeout(modelPollTimer);
      if (busy) modelPollTimer = setTimeout(loadModels, 1500);
    });
  }

  function loadSubtitleConfig(opts) {
    var onlyModels = !!(opts && opts.onlyModels);
    /* F08：两条护栏，缺一条就会静默吃掉用户没保存的输入。
       ① 只刷新模型下拉的路径（模型下载完成时走这条）：新模型要立刻可选，但**绝不碰**
          其余输入。此前那条路径直接整表重填——用户改到一半（比如正准备填云端 key），
          下载一完成表单就被服务端旧值覆盖，他再点保存就把旧值写回去（以为配好了云端、
          实际还在 local）。
       ② 整表重填前先看 dirty：用户动过任何一个配置输入就不再覆盖（与 2.5s 补拉同一判据）。
          传的是**当前下拉值**而不是配置值：新选项出现的同时保住用户的选择。 */
    if (onlyModels) {
      fillAsrModelSelect($("#asrModel") ? $("#asrModel").value : "");
      renderLocalModelSelect($("#mtLocalModel") ? $("#mtLocalModel").value : "",
                             $("#mtLocalModelEn") ? $("#mtLocalModelEn").value : "");
      return;
    }
    if (S.subCfgDirty) return;
    api("/api/subtitle/config").then(function (r) {
      if (!r.ok) return;
      var c = r.config || {};
      var asr = c.asr || {}, tr = c.translate || {};
      /* 识别引擎下拉已删（R97）：引擎固定 Qwen3（audiocpp），且只跑 GPU——
         gpu\ 运行时缺失时服务端直接报错提示下载，无 CPU 选项/回退。 */
      /* 识别模型下拉（R72）：只列 models\ 下实际存在的 qwen3_asr 模型，本地
         没有的不出现；配置指向的模型不在列表时保持空选，不静默改配置。 */
      fillAsrModelSelect(String((asr.audiocpp || {}).model || ""));
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

    /* 主题 */
    $("#themeToggle").addEventListener("click", function () {
      setTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark", true);
    });

    /* DLNA */
    $("#dlnaStart").addEventListener("click", startDlna);
    $("#dlnaStop").addEventListener("click", function () {
      api("/api/dlna/stop", "POST", {}).then(function () { toast("DLNA 已停止"); poll(true); });
    });
    /* 添加媒体根：走 /api/dlna/roots —— 它会顺手校验路径是否存在。
       不校验的话，路径写错只表现为"头显里那个文件夹是空的"，猜不到原因。 */
    var rootsOpChain = Promise.resolve();   // R103 复核：addRoots/删根串行化——
    /* 两者都是 GET→改→POST 读改写，并发时后写覆盖先写（F09 只修了串行背靠背），
       双击"添加"或添加后立刻删除会静默丢一次操作。链式排队把窗口关掉。 */
    function addRoots(list) {
      /* F09：先取**服务端当前值**再改，不拿本地缓存（S.settings）当基底全量回写。
         本地缓存只靠 1s 轮询刷新，背靠背两次操作会丢更新：删 A → 轮询带回仍含 A 的旧
         列表 → 添加 B 时把 A 一起 POST 回去（刚删的目录复活，而 toast 已经说过"已移除"）。 */
      rootsOpChain = rootsOpChain.then(function () {
      return api("/api/settings").then(function (cur) {
        var roots = (((cur || {}).settings || {}).dlna_roots || []).slice();
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
      });
      });
      return rootsOpChain;   // 链上排队：并发调用共享同一条串行链
    }
    $("#addRoot").addEventListener("click", function () { addRoots([$("#newRoot").value]); });
    /* 媒体库目录：添加（选择器，多选）与移除 */
    $("#libRootAdd").addEventListener("click", function () {
      if (!bridgeReady()) return;
      window.pywebview.api.pick_folder($("#newRoot").value || "", true).then(function (r) {
        if (!r || !r.ok) { if (r && r.error) toast("打开选择器失败", r.error, "err"); return; }
        addLibRoots(r.paths || [r.path]);
      });
    });
    var libRootChain = Promise.resolve();
    function addLibRoots(list) {
      libRootChain = libRootChain.then(function () {
        return api("/api/settings").then(function (cur) {
          var roots = (((cur || {}).settings || {}).library_roots || []).slice();
          var added = 0;
          (list || []).forEach(function (p) {
            p = String(p == null ? "" : p).trim();
            if (p && roots.indexOf(p) < 0) { roots.push(p); added++; }
          });
          if (!added) { toast("这些目录已经在列表里了", "", "warn"); return; }
          return api("/api/settings", "POST", { library_roots: roots }).then(function (r) {
            if (!r || r.ok === false) { toast("保存失败", (r && r.error) || "", "err"); return; }
            toast("已添加 " + added + " 个媒体库目录", "正在自动扫描生成海报墙", "ok");
            libRootSig = "";   // 强制重渲染
            api("/api/library/rescan", "POST", {}).then(function () { loadLibrary(); });
          });
        });
      });
      return libRootChain;
    }
    $("#libRootList").addEventListener("click", function (e) {
      var b = e.target.closest("[data-del-libroot]");
      if (!b) return;
      var p = b.getAttribute("data-del-libroot");
      libRootChain = libRootChain.then(function () {
        return api("/api/settings").then(function (cur) {
          var roots = (((cur || {}).settings || {}).library_roots || []).filter(function (x) { return x !== p; });
          return api("/api/settings", "POST", { library_roots: roots }).then(function (r) {
            if (!r || r.ok === false) { toast("移除失败", (r && r.error) || "", "err"); return; }
            toast("已移除", p, "ok");
            libRootSig = "";
          });
        });
      });
    });
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
      /* F09：同样先取服务端当前值再删。基于本地缓存全量回写时，"删 A、加 B"这类
         背靠背操作会让 A 复活（轮询带回的旧列表把 A 又写回去）。
         R103 复核：走同一条 rootsOpChain 串行链，与 addRoots 互斥。 */
      rootsOpChain = rootsOpChain.then(function () {
      return api("/api/settings").then(function (cur) {
        var roots = (((cur || {}).settings || {}).dlna_roots || []).slice();
        var i = roots.indexOf(p);
        if (i < 0) { toast("该目录已不在列表中", p || "", "warn"); poll(true); return; }
        var removed = roots.splice(i, 1);
        api("/api/settings", "POST", { dlna_roots: roots }).then(function (r) {
          // R103 复核：与 addRoots/saveSetting 同口径判 ok——失败不得提示"已移除"
          if (!r || r.ok === false) {
            toast("移除失败", (r && r.error) || "", "err");
            poll(true);
            return;
          }
          toast("已移除", removed[0] || "");
          poll(true);
        });
      });
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
          /* 保存成功后表单与服务端一致了，清掉 dirty（F08）：否则"用户动过"这个标记
             会一直为真，后续所有自动回填（含 2.5s 补拉）永久让路。 */
          S.subCfgDirty = false;
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
    /* 识别模型/引擎即改即存（失败弹回上一个值）；其余识别参数不进界面 */
    $("#asrModel").addEventListener("change", function () {
      var sel = this, val = sel.value, prev = S.asrModelPrev || "";
      if (!val || val === prev) return;
      api("/api/subtitle/config", "POST", { asr: { audiocpp: { model: val } } }).then(function (r) {
        if (r.ok) {
          S.asrModelPrev = val;
          toast("识别模型已保存", "字幕服务正在重启以加载 " + (sel.options[sel.selectedIndex] || {}).text, "ok");
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

    /* 更新（R73）：手动检查 / 状态区按钮 / 弹窗按钮 */
    $("#chkUpdate").addEventListener("click", function () {
      var btn = this;
      btn.disabled = true;
      api("/api/update/check", "POST").then(function (r) {
        btn.disabled = false;
        if (!r || !r.ok) { toast("检查更新失败", (r && r.error) || "网络不可用", "err"); return; }
        var st = S.lastState ? JSON.parse(JSON.stringify(S.lastState)) : {};
        st.update = r;
        renderUpdate(st);
      });
    });
    $("#updAction").addEventListener("click", function () {
      var u = (S.lastState || {}).update || {};
      if (u.state === "ready") {
        api("/api/update/install", "POST").then(function (r) {
          if (!r || !r.ok) toast("无法安装", (r && r.error) || "", "err");
        });
      } else if (u.state === "available") {
        api("/api/update/download", "POST").then(function (r) {
          if (r && r.ok) toast("开始下载", "完成后会询问是否安装", "ok");
          else toast("无法下载", (r && r.error) || "", "err");
        });
      }
    });
    $("#updModalGo").addEventListener("click", function () {
      $("#updModal").hidden = true;
      $("#updAction").click();
    });
    $("#updModalLater").addEventListener("click", function () {
      $("#updModal").hidden = true;
      if (S.updMode === "download") {
        var u = (S.lastState || {}).update || {};
        S.updDismissed = u.latest;   // 本次启动不再弹，设置页仍可手动下载
      } else if (S.updMode === "progress") {
        // F07：把进度弹窗收到后台——下载继续跑，但因为置了这个标记，轮询不会
        // 每秒把它重新弹出来（否则"后台运行"等于没点）。
        S.updProgDismissed = true;
      }
    });
    $("#setStartMin").addEventListener("change", function () {
      markDirty(this);
      saveSetting("setStartMin", "start_minimized", this.checked);
      toast(this.checked ? "下次启动将直接隐藏到托盘" : "下次启动将显示主窗口");
    });
    $("#setAutoStart").addEventListener("change", function () {
      markDirty(this);   // R103 复核：5 个开关里唯一漏了 dirty 护栏的（在途轮询会把开关弹回旧值）
      saveSetting("setAutoStart", "launch_on_boot", this.checked);
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
    // R103 复核：改用服务端实时配置判空（S.settings 是 1s 轮询快照，刚加完根
    // 就点启动会被旧快照误拦）；服务端 /api/dlna/start 本来就读最新设置。
    api("/api/settings").then(function (cur) {
      if (!((((cur || {}).settings || {}).dlna_roots) || []).length) {
        toast("请先添加媒体根目录", "", "warn");
        return;
      }
      api("/api/settings", "POST", { dlna_port: port }).then(function (r) {
        // R103 复核：判 ok——端口保存失败仍启动的话，端口只在本运行生效、
        // 未落盘，下次 auto_start 会回退旧端口
        if (!r || r.ok === false) {
          toast("端口保存失败", (r && r.error) || "", "err");
          return;
        }
        api("/api/dlna/start", "POST", { port: port }).then(function (r) {
          toast(r.ok ? "DLNA 正在启动" : "启动失败", r.error || "", r.ok ? "ok" : "err");
          poll(true);
        });
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
  /* ---- 更新（R73）：状态渲染 + 弹窗。启动自动检查与手动检查共用一条状态流。 ---- */
  function renderUpdate(st) {
    var u = st.update || {};
    var box = $("#updTitle"), desc = $("#updDesc"), act = $("#updAction");
    if ($("#updCurrent")) $("#updCurrent").textContent = "v" + (u.current || st.version || "—");
    if (!box) return;
    if (u.state === "checking") { box.textContent = "正在检查…"; desc.textContent = ""; act.hidden = true; }
    else if (u.state === "downloading") { box.textContent = "正在下载 v" + (u.latest || "?") + "（" + (u.pct || 0) + "%）"; desc.textContent = "下载完成后可就地安装"; act.hidden = true; }
    else if (u.state === "ready") { box.textContent = "v" + u.latest + " 安装包已就绪"; desc.textContent = "安装会关闭应用，完成后自动重新启动"; act.hidden = false; act.textContent = "立即安装"; }
    else if (u.state === "available") { box.textContent = "发现新版本 v" + u.latest; desc.textContent = "当前 v" + (u.current || st.version || "?"); act.hidden = false; act.textContent = "下载更新"; }
    else if (u.state === "error") { box.textContent = "检查更新失败"; desc.textContent = u.error || "网络不可用"; act.hidden = true; }
    else if (u.state === "none") { box.textContent = "已是最新版本"; desc.textContent = "当前 v" + (u.current || st.version || "?"); act.hidden = true; }
    else { box.textContent = "尚未检查"; desc.textContent = "应用启动时会自动检查一次"; act.hidden = true; }
    /* 弹窗全流程前台（R76）：下载中实时进度、不进后台，完成即转安装询问。
       F07 修复要点见下面各分支与 showUpdateModal 的按钮策略。 */
    if (u.state === "downloading") {
      if (!S.updProgDismissed) {     // 用户点了"后台运行"就不再每秒重弹
        showUpdateModal("正在下载 v" + (u.latest || ""), "下载进度：" + (u.pct || 0) + "%", "progress", u.pct || 0);
      }
    } else {
      S.updProgDismissed = false;    // 离开下载态即复位，否则会吞掉后面的"安装询问"
      if (u.state === "available" && u.has_update && S.updDismissed !== u.latest) {
        showUpdateModal("发现新版本 v" + u.latest,
          "当前 v" + (u.current || st.version || "?") + "，可下载更新安装包（约 " +
          Math.max(1, Math.round((u.size || 0) / 1048576)) + " MB）。安装会关闭应用，完成后自动重启。",
          "download");
      } else if (u.state === "ready" && S.updReadyFor !== u.latest) {
        S.updReadyFor = u.latest;
        showUpdateModal("下载完成",
          "v" + u.latest + " 已就绪。立即安装会关闭应用，安装完成后自动重新启动。", "install");
      } else if (u.state === "error" && !$("#updModal").hidden) {
        /* F07：下载/检查失败必须**收尾**已打开的弹窗。此前 error 分支只改设置页
           文字，而进度弹窗把两个按钮都隐藏了 ⇒ 全屏遮罩（position:fixed inset:0）
           把整个 UI 锁死，只能杀进程——而下载中断是常见路径（GitHub 直连常被重置）。
           只改"当前已经打开"的弹窗：用户关掉之后不再重弹。 */
        showUpdateModal("更新失败",
          (u.error || "网络不可用") + "。可在设置页重试，或稍后再试。", "error");
      }
    }
  }
  function showUpdateModal(title, text, mode, pct) {
    $("#updModalTitle").textContent = title;
    $("#updModalText").textContent = text;
    $("#updBar").hidden = mode !== "progress";
    if (mode === "progress") $("#updBarFill").style.width = (pct || 0) + "%";
    /* 按钮策略（F07）：**任何状态都必须留一条关闭路径**。
       · progress：只留"后台运行"（下载继续跑，弹窗关掉；此前两个按钮都隐藏，
         下载一旦卡住不报错也不完成，就是全屏死锁）；
       · error：只留"关闭"；
       · download/install：关闭 + 主操作。 */
    $("#updModalGo").hidden = (mode !== "download" && mode !== "install");
    $("#updModalLater").hidden = false;
    $("#updModalLater").textContent = mode === "progress" ? "后台运行"
                                   : (mode === "error" ? "关闭" : "稍后");
    $("#updModalGo").textContent = mode === "install" ? "立即安装" : "下载更新";
    $("#updModal").hidden = false;
    S.updMode = mode;
  }

  function poll(once) {
    api("/api/state").then(function (st) {
      if (st && st.ok) {
        // 渲染异常绝不能吃掉后面的重排定时器：此前 render 抛一次异常，整条轮询链
        // 就永久停摆（数值冻结在最后一帧，只有切标签页或点按钮才能救回来）。
        try {
          S.lastState = st;          // 此前只被读过从未赋值（updateVramEstimate 一直拿到 undefined）
          render(st);
          renderUpdate(st);
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

  /* ---------- 视频联动（浏览⇄播放双模 + 手机端对齐卡片/热力图/预设） ---------- */
  var libPollTimer = 0, libSeekDrag = false;
  var vlBrowsePath = "";        // ""=根（roots 卡片）
  var vlLastBrowse = { videos: [] };   // 当前目录的视频列表（内置播放器的上一个/下一个）
  var VL_PRESETS = null, vlTab = "stroke", vlHeatScript = null, vlHeatKey = "";
  var vlPlayingPreset = false, vlRandomMode = false, vlBoostMode = false;
  var vlDevState = "disconnected";   // disconnected | connecting | connected
  var VL_SPEED_MAX = 500;

  function fmtTime(sec) {
    sec = Math.max(0, Math.floor(sec || 0));
    var h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
    return h > 0 ? (h + ":" + String(m).padStart(2, "0") + ":" + String(s).padStart(2, "0"))
                 : (m + ":" + String(s).padStart(2, "0"));
  }

  /* --- 大框框：浏览模式 --- */
  function renderBrowse(data) {

    $("#vlBrowse").style.display = "";
    $("#vlPlayView").style.display = "none";
    $("#vlBack").style.display = vlBrowsePath ? "" : "none";
    $("#vlBrowseTitle").textContent = vlBrowsePath
      ? (vlBrowsePath.split(/[\\/]/).filter(Boolean).pop() || "浏览")
      : "媒体库";
    var dirs = data.dirs || [], vids = data.videos || [];
    var cnt = $("#vlCount");
    if (cnt) cnt.textContent = (dirs.length ? dirs.length + " 个目录 · " : "") + vids.length + " 个视频";
    var html = "";
    dirs.forEach(function (d) {
      html += '<div class="vl-folder" data-path="' + encodeURIComponent(d.path) + '">' +
        '<svg class="ic"><use href="#i-vk-folder"/></svg><div class="name">' + esc(d.name) + '</div></div>';
    });
    vids.forEach(function (v) {
      var thumb = v.thumb
        ? '<img loading="lazy" src="/api/library/thumb?name=' + encodeURIComponent(v.thumb) + '">'
        : '<svg class="ic lib-fileic"><use href="#i-vk-video"/></svg>';
      var badges = "";
      if (v.has_funscript) badges += '<span class="lib-badge"><svg class="ic"><use href="#i-vk-script"/></svg>脚本</span>';
      if (v.has_srt) badges += '<span class="lib-badge sub"><svg class="ic"><use href="#i-vk-sub"/></svg>字幕</span>';
      var dur = (v.dur > 0) ? '<span class="lib-dur">' + fmtTime(v.dur) + '</span>' : "";
      var prog = v.pos > 5 ? '<div class="lib-prog"><i style="width:' + Math.min(95, v.pos) + '%"></i></div>' : "";
      html += '<div class="lib-card" data-vpath="' + encodeURIComponent(v.path) + '">' +
        '<div class="lib-thumb">' + thumb +
        (badges ? '<span class="lib-badges">' + badges + '</span>' : "") + dur + prog + '</div>' +
        '<div class="lib-title">' + esc(v.name) + '</div></div>';
    });
    if (!html) html = '<div class="empty">这里没有视频</div>';
    $("#vlBrowseBody").innerHTML = html;
    vlLastBrowse = { videos: vids };   // 内置播放器"上一个/下一个"用
    if (window.requestAnimationFrame) requestAnimationFrame(function () { layoutVl(); });
  }
  var vlBrowseGen = 0;                  // 异步代际：连点两个目录，慢的旧响应不得覆盖新结果
  function browse(path) {
    vlBrowsePath = path || "";
    var gen = ++vlBrowseGen;
    api("/api/library/browse?path=" + encodeURIComponent(vlBrowsePath)).then(function (r) {
      if (gen !== vlBrowseGen) return;
      if (r && r.ok) renderBrowse(r);
      else toast("打开失败", (r && r.error) || "", "err");
    });
  }
  $("#vlBrowseBody").addEventListener("click", function (e) {
    var f = e.target.closest(".vl-folder");
    if (f) { browse(decodeURIComponent(f.getAttribute("data-path"))); return; }
    var card = e.target.closest(".lib-card");
    if (!card || !card.getAttribute("data-vpath")) return;
    var vp = decodeURIComponent(card.getAttribute("data-vpath"));
    /* 内置播放：直接在大框框里放，不再拉起外挂 mpv（外挂仍保留为兜底按钮） */
    var list = (vlLastBrowse.videos || []).map(function (v) { return { path: v.path, name: v.name }; });
    var idx = 0;
    list.forEach(function (it, i) { if (it.path === vp) idx = i; });
    openVideo(vp, list[idx] ? list[idx].name : vp.split(/[\\/]/).pop(), list, idx);
  });
  /* 返回上一级：严格的上一级。旧实现把"父级 == 某个根"当成"该回根列表"，
     于是在根目录里点返回会去 browse("E:") → 后端报"路径不在媒体库目录内"；
     在子目录里点返回又会跳过根目录直接跳回媒体库。 */
  $("#vlBack").addEventListener("click", function () {
    var p = (vlBrowsePath || "").replace(/[\\/]+$/, "");
    if (!p) return;
    var roots = ((S.settings && S.settings.library_roots) || []).map(function (r) {
      return String(r).replace(/[\\/]+$/, "").toLowerCase();
    });
    if (roots.indexOf(p.toLowerCase()) >= 0) { browse(""); return; }   // 就在某个根里 → 上一层是媒体库
    var up = p.split(/[\\/]/);
    up.pop();
    var parent = up.join("\\");
    if (!parent || parent.length <= 2) { browse(""); return; }         // 盘符根 → 媒体库
    browse(parent);                                                    // 其余老老实实上一层
  });

  /* =====================================================================
     内置播放器：HTML5 <video> 直接在大框框里播（用户明确要求，不再外挂 mpv）
     流地址优先级：① 宿主 /api/library/stream（1.0.56+）② DLNA /media/<key>
     （DLNA 服务器已支持 Range 206，1.0.55 就能用；外挂 mpv 只作兜底）
     ===================================================================== */
  var VL_STREAM = { api: null };          // null=未探测 / true=宿主有流接口 / false=走 DLNA
  var vlMed = { list: [], idx: -1, path: "", name: "", failed: false };
  var vlVidEl = null, vlChromeTimer = 0;
  function vlVid() { return vlVidEl || (vlVidEl = document.getElementById("vlVideo")); }
  function vlPlaying() { var v = vlVid(); return !!(v && vlMed.path && !v.paused && !v.ended); }

  /* DLNA key 规则（vendor/dlna/vr_dlna.py path_to_key）：单根=相对路径，多根=label/相对路径，
     label = 根目录 basename（host_server 里 MediaRoot(label=Path(p).name or "Videos")） */
  /* vlDlnaUrl 已删除：播放不再经过 DLNA */
  function vlStreamUrl(absPath, cb) {
    /* 电脑端播放只走宿主本地流（宿主直接读盘、Range/206）；
       DLNA 是给手机/VR 的出站共享，与电脑自身播放无关。 */
    cb("/api/library/stream?path=" + encodeURIComponent(absPath));
  }

  function vlShowChrome(on) {
    var w = $("#vlVWrap"); if (!w) return;
    w.classList.toggle("idle", !on);
  }
  function vlTouch() {
    vlShowChrome(true);
    clearTimeout(vlChromeTimer);
    vlChromeTimer = setTimeout(function () { if (vlPlaying()) vlShowChrome(false); }, 4000);
  }
  function vlSetPlayIcon() {
    var v = vlVid(), u = $("#vlPlay") && $("#vlPlay").querySelector("use");
    if (u) u.setAttribute("href", (v && !v.paused && !v.ended) ? "#i-pause" : "#i-play");
    var u2 = $("#vlPause") && $("#vlPause").querySelector("use");   // 页面上的 ⏸ 同步
    if (u2) u2.setAttribute("href", (v && !v.paused && !v.ended) ? "#i-pause" : "#i-play");
  }
  function vlVideoFail(msg) {
    vlMed.failed = true;
    var off = $("#vlVOff"), v = vlVid();
    if (v) { try { v.pause(); } catch (e) {} v.removeAttribute("src"); v.load(); }
    if (off) { off.hidden = false; if (msg) $("#vlVOffD").textContent = msg; }
    vlSetPlayIcon();
  }
  function openVideo(path, name, list, idx) {
    vlMed.path = path || "";
    vlMed.name = name || String(path || "").split(/[\\/]/).pop();
    vlMed.failed = false;
    if (list && list.length) { vlMed.list = list; vlMed.idx = idx || 0; }
    showPlayView(vlMed.path);
    $("#vlPlayName").textContent = vlMed.name;
    $("#vlPlayPath").textContent = vlMed.path;
    $("#vlVOff").hidden = true;
    var v = vlVid();
    v.style.display = "";
    vlStreamUrl(vlMed.path, function (url) {
      if (!url) { vlVideoFail("这个文件不在 DLNA 共享目录里，无法内置播放；可点下面的按钮用外部播放器。"); return; }
      v.src = url;
      v.load();
      var pr = v.play();
      if (pr && pr.catch) pr.catch(function () { /* 自动播放被拦或解码失败，等 error 事件 */ });
    });
    vlTouch();
    vlSyncVlUi();
  }
  function vlSyncVlUi() {
    var v = vlVid(); if (!v) return;
    var cur = v.currentTime || 0, dur = isFinite(v.duration) ? v.duration : 0;
    $("#vlVPos").textContent = fmtTime(cur);
    $("#vlVDur").textContent = dur ? fmtTime(dur) : "0:00";
    $("#vlPos").textContent = fmtTime(cur);
    $("#vlDur").textContent = dur ? fmtTime(dur) : "0:00";
    var seek = $("#vlVSeek");
    if (seek) seek.style.setProperty("--a", 0), seek.style.setProperty("--b", dur ? Math.min(1, cur / dur) : 0);
    var vol = $("#vlVVol");
    if (vol) { vol.style.setProperty("--a", 0); vol.style.setProperty("--b", v.muted ? 0 : (v.volume || 0)); }
    var mu = $("#vlMute") && $("#vlMute").querySelector("use");
    if (mu) mu.setAttribute("href", (v.muted || !v.volume) ? "#i-volume-x" : "#i-volume");
    drawHeat(cur, dur);
    vlSetPlayIcon();
  }
  function vlSeekTo(sec) {
    var v = vlVid(); if (!v) return;
    var dur = isFinite(v.duration) ? v.duration : 0;
    v.currentTime = Math.max(0, Math.min(dur || sec, sec));
    vlSyncVlUi();
  }
  function vlStep(delta) {
    if (!vlMed.list.length) return;
    var n = vlMed.list.length;
    var i = (vlMed.idx + delta + n) % n;
    var it = vlMed.list[i];
    if (it) openVideo(it.path, it.name, vlMed.list, i);
  }
  function vlInitPlayer() {
    var v = vlVid(); if (!v || v.__wired) return;
    v.__wired = true;
    ["timeupdate", "durationchange", "progress", "play", "pause", "ended", "volumechange", "seeked"]
      .forEach(function (ev) {
        v.addEventListener(ev, function () {
          vlSyncVlUi();
          if (ev === "pause" || ev === "ended") vlShowChrome(true);   // 暂停时常显
          if (ev === "pause" || ev === "ended") api("/api/quick", "POST", { kind: "pause", on: true }).then(pollDev);
          if (ev === "play") api("/api/quick", "POST", { kind: "resume", on: true }).then(pollDev);
          if (ev === "play" || ev === "pause") vlTouch();
        });
      });
    v.addEventListener("error", function () {
      vlVideoFail("内置播放器打不开这个文件：多数是编码不受支持（HEVC / 10bit / AV1），或文件已经不在了。");
    });
    v.addEventListener("click", function () { if (v.paused) v.play(); else v.pause(); vlTouch(); });
    v.addEventListener("play", function () { syncStart(vlMed.path); });
    v.addEventListener("timeupdate", function () { syncTick(v.currentTime); });
    v.addEventListener("ended", function () { syncStop(); });
    $("#vlPlay").addEventListener("click", function () { if (v.paused) v.play(); else v.pause(); vlTouch(); });
    $("#vlPrev").addEventListener("click", function () { vlStep(-1); });
    $("#vlNext").addEventListener("click", function () { vlStep(1); });
    $("#vlMute").addEventListener("click", function () { v.muted = !v.muted; vlSyncVlUi(); });
    /* 全屏作用在**文档根**：隐藏任何子容器都安全（见 styles.css 里那段注释）。
       副作用是 html 全屏后整页都在，所以靠 CSS 把 .vl-vwrap 提成铺满屏的浮层。 */
    $("#vlFull").addEventListener("click", function () {
      var root = document.documentElement;
      try {
        var pr = document.fullscreenElement ? document.exitFullscreen() : (root.requestFullscreen ? root.requestFullscreen() : null);
        if (pr && pr.catch) pr.catch(function () { toast("全屏切换失败", "浏览器拒绝了这次全屏请求", "warn"); });
      } catch (e) { toast("全屏切换失败", String(e.message || e), "warn"); }
    });
    $("#vlPip").addEventListener("click", function () {
      try {
        if (document.pictureInPictureElement) document.exitPictureInPicture();
        else if (v.requestPictureInPicture) v.requestPictureInPicture();
      } catch (e) { toast("画中画不可用", "", "warn"); }
    });
    /* ⏸ 大按钮：直接控制内置播放器（本页不再操作外部播放器） */
    $("#vlPause").addEventListener("click", function () {
      if (vlMed.path && !vlMed.failed) {
        if (v.paused) v.play(); else v.pause();
        vlTouch();
        return;
      }
      toast("没有正在播放的视频", "先在媒体库里点一个视频", "warn");
    });
    $("#vlVBack").addEventListener("click", function () {
      /* 回到媒体库（暂停并释放流，时间轴交回 mpv 轮询口径） */
      /* 先退全屏再动 DOM：虽然现在全屏元素是文档根、隐藏子容器已经安全，
         但退出动作放前面更稳（这里就是"全屏后点返回直接卡死"的现场）。 */
      try {
        if (document.fullscreenElement) {
          var ex = document.exitFullscreen();
          if (ex && ex.catch) ex.catch(function () { /* 浏览器已自行退出，忽略 */ });
        }
      } catch (e) {}
      try { v.pause(); } catch (e) {}
      v.removeAttribute("src"); v.load();
      vlMed.path = ""; vlMed.failed = false;
      vlCurPath = "";   // 离开播放态，免得轮询再走"回根"分支
      $("#vlPlayView").style.display = "none";
      $("#vlBrowse").style.display = "";
      vlHeatScript = null; vlHeatKey = ""; drawHeat();
      /* 回当前目录要**重新拉列表**：只拿缓存的 videos 会把子目录卡丢掉
         （多目录时返回后看到的是"0 张卡"，还没法再进子目录）。 */
      browse(vlBrowsePath || "");
      if (window.requestAnimationFrame) requestAnimationFrame(function () { layoutVl(); });
    });

    /* 进度/音量滑轨：复用页面的自研滑轨（--a/--b 驱动，几何一致）。
       seek 节流（手机端 SEEK_THROTTLE_MS 同义）：拖动中每 250ms 才真 seek 一次，
       松手立即落到最终位置 —— 否则每帧都 currentTime 会把解码器拖卡 */
    var vlSeekLast = 0, vlSeekTimer = 0;
    initSlider("vlVSeek", false, function (lo, hi, live) {
      var d = isFinite(v.duration) ? v.duration : 0;
      if (d <= 0) return;
      var doSeek = function () {
        v.currentTime = Math.max(0, Math.min(d, hi * d));
        vlTouch();
      };
      if (!live) { clearTimeout(vlSeekTimer); vlSeekTimer = 0; doSeek(); return; }
      var now = Date.now();
      if (now - vlSeekLast >= 250) { vlSeekLast = now; doSeek(); }
      else if (!vlSeekTimer) {
        vlSeekTimer = setTimeout(function () { vlSeekTimer = 0; vlSeekLast = Date.now(); doSeek(); }, 250);
      }
    });
    initSlider("vlVVol", false, function (lo, hi) {
      v.volume = Math.max(0, Math.min(1, hi / 100));
      v.muted = hi <= 0;
      vlSyncVlUi();
    });
    /* 鼠标不动 3s 收控制条；一动就出来 */
    var wrap = $("#vlVWrap");
    ["pointermove", "pointerdown", "wheel"].forEach(function (ev) { wrap.addEventListener(ev, vlTouch); });
    document.addEventListener("fullscreenchange", function () {
      var u = $("#vlFull") && $("#vlFull").querySelector("use");
      if (u) u.setAttribute("href", document.fullscreenElement ? "#i-minimize" : "#i-maximize");
    });
  }

  /* --- 大框框：播放模式（外挂 mpv 的页内镜像） --- */
  var vlCurPath = "";
  function showPlayView(path) {
    vlCurPath = path || vlCurPath;
    $("#vlBrowse").style.display = "none";
    $("#vlPlayView").style.display = "";
    $("#vlPlayPath").textContent = vlCurPath;
    if (window.requestAnimationFrame) requestAnimationFrame(function () { layoutVl(); });
  }

  /* --- 播放状态轮询（驱动播放视图/时间轴） --- */
  var vlPlayerOpen = false;
  function renderPlayState(p) {
    /* 视频联动页只用内置播放器：不再镜像外部 mpv 的播放状态 */
    vlPlayerOpen = !!(p && p.open);
  }
  function loadLibrary() {
    api("/api/player/state").then(function (p) {
      renderPlayState(p);
      if (p && p.open) {
        if ($("#vlPlayView").style.display === "none") showPlayView(p.path);
        clearTimeout(libPollTimer);
        libPollTimer = setTimeout(loadLibrary, 1000);
      } else {
        clearTimeout(libPollTimer);
        libPollTimer = setTimeout(loadLibrary, 8000);
      }
    });
  }

  /* --- 脚本热力图（手机 ScriptHeatmap.kt 同构：7 档色带/速度窗 50/位置分位窗 15/间隙 5s 重置） --- */
  var HEAT_STOPS = [[0, 0, 0], [30, 144, 255], [34, 139, 34], [255, 215, 0], [220, 20, 60], [147, 112, 219], [37, 22, 122]];
  /* 底色随主题：**整条同一个色**（此前是"类白→淡紫"的横向渐变，用户要求不要渐变）
     亮色 #ece9f5（类白偏淡紫）／暗色 #1c1728（类黑偏深紫），都不用纯白纯黑 */
  var HEAT_BG = { dark: "#1c1728", light: "#ece9f5" };
  var HEAT_INK = { dark: "#ffffff", light: "#1b1b1f" };   // 播放头
  function heatTheme() { return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark"; }
  function heatBg() { return HEAT_BG[heatTheme()]; }
  function heatPaint(ctx, w, h) {                 // 平铺同色，不做渐变
    ctx.fillStyle = heatBg();
    ctx.fillRect(0, 0, w, h);
  }
  function heatZero() {                           // 最低色档的插值起点 = 底色本身
    var c = heatBg();
    return [parseInt(c.slice(1, 3), 16), parseInt(c.slice(3, 5), 16), parseInt(c.slice(5, 7), 16)];
  }
  function heatColor(speed) {
    var n = 120.0;
    if (speed <= 0) return heatZero();
    if (speed > 5 * n) return HEAT_STOPS[6];
    var t = speed + n / 2;
    var i = Math.min(Math.floor(t / n), HEAT_STOPS.length - 2);
    var frac = Math.min(1, Math.max(0, (t - i * n) / n));
    var a = HEAT_STOPS[i], b = HEAT_STOPS[i + 1];
    return [a[0] + (b[0] - a[0]) * frac, a[1] + (b[1] - a[1]) * frac, a[2] + (b[2] - a[2]) * frac];
  }
  /* w/h 为设备像素（画布 backing store），调用方按 dpr 放大后传入 */
  function renderHeatBitmap(cv, script, timelineEnd, w, h) {
    var ctx = cv.getContext("2d");
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    heatPaint(ctx, w, h);
    var acts = script && script.actions;
    if (!acts || acts.length < 2 || timelineEnd <= 0) return;
    var d = w / (timelineEnd * 1000);
    var SPEED_WINDOW = 50, POSITION_WINDOW = 15, GAP = 5000;
    var speedWin = [], posWin = [], xPrev = 0;
    for (var b = 1; b < acts.length; b++) {
      var atPrev = acts[b - 1][0], atCur = acts[b][0];
      var x = d * atCur;
      if (atCur - atPrev > GAP) { speedWin = []; posWin = []; xPrev = x; continue; }
      /* 手机语义：|Δvalue(0..1)| × 100 ÷ Δt(秒) = %/秒。
         旧实现除的是毫秒（pos 又是 0..100），速度只有手机的 1/10 ——
         整条热力图永远落在最低那一档，看着"只有一种蓝"。 */
      var speed = Math.abs(acts[b][1] - acts[b - 1][1]) / Math.max(1e-9, (atCur - atPrev) / 1000);
      speedWin.push(speed);
      if (speedWin.length > SPEED_WINDOW) speedWin.shift();
      posWin.push(acts[b][1]);
      if (posWin.length > POSITION_WINDOW) posWin.shift();
      var avg = speedWin.reduce(function (a, c) { return a + c; }, 0) / speedWin.length;
      var col = heatColor(avg);
      if (posWin.length >= 2) {
        var sorted = posWin.slice().sort(function (x2, y2) { return x2 - y2; });
        var mid = Math.floor(sorted.length / 2);
        var lower = 0, upper = 0, i2;
        for (i2 = 0; i2 < mid; i2++) lower += sorted[i2];
        for (i2 = mid; i2 < sorted.length; i2++) upper += sorted[i2];
        var lowerAvg = lower / mid, upperAvg = upper / (sorted.length - mid);
        var top = h - lowerAvg / 100 * h;
        var barH = (lowerAvg - upperAvg) / 100 * h;
        ctx.fillStyle = "rgb(" + col.map(Math.round).join(",") + ")";
        ctx.fillRect(xPrev, top, Math.max(1, x - xPrev), barH);
      }
      xPrev = x;
    }
  }
  /* 热力条位图缓存（手机端 produceState 同构）：只在脚本/尺寸/主题变化时整幅重画 */
  var heatOff = document.createElement("canvas"), heatOffKey = "";
  function drawHeat(pos, dur) {
    var cv = $("#vlHeat");
    if (!cv) return;
    var dpr = window.devicePixelRatio || 1;
    var w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    var pw = Math.round(w * dpr), ph = Math.round(h * dpr);
    if (cv.width !== pw || cv.height !== ph) { cv.width = pw; cv.height = ph; }
    var ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    heatPaint(ctx, w, h);
    /* 统一时间基准：max(媒体时长, 脚本末帧)（手机端同款） */
    var sc = vlHeatScript;
    var scriptEnd = 0;
    if (sc && sc.actions && sc.actions.length) scriptEnd = sc.actions[sc.actions.length - 1][0] / 1000;
    var timelineEnd = Math.max(dur || 0, scriptEnd);
    if (sc) {
      var key = (vlHeatKey || "") + "|" + pw + "x" + ph + "|" + heatTheme();
      if (heatOffKey !== key) {
        heatOff.width = pw; heatOff.height = ph;
        renderHeatBitmap(heatOff, sc, timelineEnd, pw, ph);
        heatOffKey = key;
      }
      ctx.drawImage(heatOff, 0, 0, w, h);
    }
    if (timelineEnd > 0 && pos > 0) {
      var x = Math.min(w, w * pos / timelineEnd);
      ctx.fillStyle = HEAT_INK[heatTheme()];
      ctx.fillRect(x - 1, 0, 2, h);
    }
  }
  $("#vlHeat").addEventListener("click", function (e) {
    var rect = this.getBoundingClientRect();
    var frac = (e.clientX - rect.left) / rect.width;
    var sc = vlHeatScript;
    var scriptEnd = (sc && sc.actions && sc.actions.length) ? sc.actions[sc.actions.length - 1][0] / 1000 : 0;
    /* 内置播放器在放 → 直接 seek 视频；否则走 mpv 接口 */
    if (vlMed.path && !vlMed.failed) {
      var v = vlVid();
      var d = (v && isFinite(v.duration)) ? v.duration : 0;
      var end = Math.max(d, scriptEnd);
      if (end > 0) vlSeekTo(frac * end);
      return;
    }
    /* 没有内置播放时什么都不做（本页不再操作外部播放器） */
  });
  function loadHeat() {
    if (!document.querySelector("#page-library.active")) return;
    /* 内置播放器在放：时间轴跟视频走，不再轮询 mpv */
    if (vlMed.path && !vlMed.failed) {
      var v = vlVid();
      if (vlHeatKey !== vlMed.path || !vlHeatScript) {
        vlHeatKey = vlMed.path;
        api("/api/library/script?path=" + encodeURIComponent(vlMed.path)).then(function (r) {
          vlHeatScript = (r && r.ok) ? r : null;
          vlSyncVlUi();
        });
      } else if (v) {
        vlSyncVlUi();
      }
      return;
    }
    api("/api/player/state").then(function (p) {
      if (!p || !p.open) { vlHeatScript = null; vlHeatKey = ""; drawHeat(); return; }
      var want = p.path;
      if (vlHeatKey === want && vlHeatScript) { drawHeat(p.pos, p.dur); return; }
      api("/api/library/script?path=" + encodeURIComponent(p.path)).then(function (r) {
        vlHeatKey = want;
        vlHeatScript = (r && r.ok) ? r : null;
        drawHeat(p.pos, p.dur);
      });
    });
  }
  setInterval(loadHeat, 1000);

  /* --- 钢蓝组件（手机端同款） --- */
  function sliderRowHtml(label, valueText, id, min, max, val, enabled) {
    return '<div class="steel-slider-row"><div class="steel-slider-head">' +
      '<span class="steel-label">' + label + '</span>' +
      '<span class="steel-value" id="' + id + '_val">' + valueText + '</span></div>' +
      '<div class="vl-slider" id="' + id + '" data-single="1" data-min="' + min + '" data-max="' + max +
      '" data-val="' + val + '"' + (enabled === false ? ' data-disabled="1"' : '') + '>' +
      '<div class="track"></div><div class="fill"></div><div class="thumb" data-side="hi"></div></div></div>';
  }
  function linkBoxHtml(id, checked, labelText) {
    return '<label class="steel-link"><input type="checkbox" id="' + id + '"' + (checked ? " checked" : "") + '> ' + (labelText || "关联输出") + '</label>';
  }

  /* --- 左侧三页签卡（严格手机三卡：范围滑轨 + 速度滑轨 + 关联输出） --- */
  var VL_TABS = ["stroke", "idle", "burst"];
  function vlCfg() { return (S.settings && S.settings.video_link) || {}; }
  function saveVl(patch) {
    /* 本地先认值：S.settings 是 1s 轮询的快照，300ms 内连改两项时后一次 POST
       若还读旧快照，会把前一项回滚（对齐 bindLink 的写法） */
    if (S.settings) S.settings.video_link = Object.assign({}, vlCfg(), patch);
    api("/api/settings", "POST", { video_link: Object.assign({}, vlCfg(), patch) }).then(function (r) {
      if (!r || r.ok === false) toast("保存失败", (r && r.error) || "", "err");
    });
  }
  /* --- 双点行程滑轨（一条轨道两个把手，手机 SteelRangeSlider 同构） --- */
  function dualSliderHtml(id, min, max, lo, hi, label, valueText, disabled) {
    return '<div class="steel-slider-row"><div class="steel-slider-head">' +
      '<span class="steel-label">' + label + '</span>' +
      '<span class="steel-value" id="' + id + '_val">' + valueText + '</span></div>' +
      '<div class="vl-slider" id="' + id + '" data-min="' + min + '" data-max="' + max + '" data-lo="' + lo + '" data-hi="' + hi + '"' +
      (disabled ? ' data-disabled="1"' : '') + '>' +
      '<div class="track"></div><div class="fill"></div>' +
      '<div class="thumb" data-side="lo"></div><div class="thumb" data-side="hi"></div></div></div>';
  }
  /* 单点/双点同一实现：把手位置写进 --a/--b，几何全在 CSS（保证两条滑轨端点严格对齐） */
  function initSlider(id, dual, onChange) {
    var el = $("#" + id);
    if (!el) return;
    var min = Number(el.getAttribute("data-min")), max = Number(el.getAttribute("data-max"));
    var span = (max - min) || 1;
    var lo = el.hasAttribute("data-lo") ? Number(el.getAttribute("data-lo")) : min;
    var hi = el.hasAttribute("data-hi") ? Number(el.getAttribute("data-hi")) : Number(el.getAttribute("data-val"));
    var thumbs = { hi: el.querySelector('[data-side="hi"]') };
    if (dual) thumbs.lo = el.querySelector('[data-side="lo"]');
    function paint() {
      el.style.setProperty("--a", (lo - min) / span);
      el.style.setProperty("--b", (hi - min) / span);
    }
    paint();
    if (el.hasAttribute("data-disabled")) return;
    /* CSS 轨道两端各内缩 7.5px、把手 15px（styles.css .vl-slider）：换算必须按
       (clientX-7.5)/(宽-15)，按整宽算会拖不到 0 和 100（260px 容器两端各丢 ≈5.8 点） */
    function xToVal(clientX, r) {
      var x = Math.min(Math.max(7.5, clientX - r.left), Math.max(7.5, r.width - 7.5));
      return min + (x - 7.5) / Math.max(1, r.width - 15) * (max - min);
    }
    /* live=true 表示拖动进行中：只更新显示，不触发落盘（设备限位等 pointerup 才下发，
       否则拖一下就是一次 BLE apply_limits 往返，与运动帧交错） */
    function set(which, v, live) {
      v = Math.round(Math.min(max, Math.max(min, v)));
      if (dual) {
        if (which === "lo" && v > hi - 1) v = hi - 1;
        if (which === "hi" && v < lo + 1) v = lo + 1;
      }
      if (which === "lo") lo = v; else hi = v;
      el.setAttribute("data-lo", lo); el.setAttribute("data-hi", hi);
      paint();
      onChange(lo, hi, !!live);
    }
    Object.keys(thumbs).forEach(function (side) {
      var th = thumbs[side];
      if (!th) return;
      th.addEventListener("pointerdown", function (e) {
        e.preventDefault();
        try { th.setPointerCapture(e.pointerId); } catch (err) { /* 合成事件无有效 pointerId */ }
        var move = function (ev) {
          set(side, xToVal(ev.clientX, el.getBoundingClientRect()), true);
        };
        var up = function () {
          th.removeEventListener("pointermove", move);
          th.removeEventListener("pointerup", up);
          onChange(lo, hi, false);            // 松手才落盘
        };
        th.addEventListener("pointermove", move);
        th.addEventListener("pointerup", up);
      });
    });
    /* 点轨道也能跳（手机滑轨同款手感）：单击一步到位，直接落盘 */
    el.addEventListener("pointerdown", function (e) {
      if (e.target.classList.contains("thumb")) return;
      var r = el.getBoundingClientRect();
      var v = xToVal(e.clientX, r);
      set(dual ? (Math.abs(v - lo) <= Math.abs(v - hi) ? "lo" : "hi") : "hi", v, false);
    });
  }
  /* 拖动期间数值实时更新，落盘防抖 300ms（旧版只在 pointerup 存一次，
     数值要等切页签重画才变 —— 用户看到的就是"数字不动"） */
  var vlSaveTimer = 0, vlSavePending = null;
  function queueVl(patch) {
    vlSavePending = Object.assign(vlSavePending || {}, patch);
    clearTimeout(vlSaveTimer);
    vlSaveTimer = setTimeout(function () {
      var p = vlSavePending; vlSavePending = null;
      if (p) saveVl(p);
    }, 300);
  }
  function renderVlTabCard() {
    var box = $("#vlTabCard");
    if (!box) return;
    var cfg = vlCfg();
    var g = function (k, d) { return cfg[k] != null ? cfg[k] : d; };
    var mainLo = g("range_min", 0), mainHi = g("range_max", 100), mainSpd = g("max_speed", 500);
    var html = "";
    if (vlTab === "stroke") {
      html = '<div class="steel-card-title">设备行程与速度</div><div class="steel-rows">' +
        dualSliderHtml("vl_range", 0, 100, mainLo, mainHi, "限制输出范围", mainLo + "% - " + mainHi + "%") +
        sliderRowHtml("设备速度上限", mainSpd + " Units/s", "vl_max_speed", 0, VL_SPEED_MAX, mainSpd) + '</div>';
    } else if (vlTab === "idle") {
      var iLink = !!cfg.idle_link;
      var iLo = iLink ? mainLo : g("idle_min", 0), iHi = iLink ? mainHi : g("idle_max", 100);
      var iTxt = iLink ? (iLo + "% - " + iHi + "%（关联主输出）") : (iLo + "% - " + iHi + "%");
      html = '<div class="steel-card-title">待机缓动</div><div class="steel-rows">' +
        dualSliderHtml("vl_idle_range", 0, 100, iLo, iHi, "运动范围", iTxt, iLink) +
        linkBoxHtml("vl_idle_link", iLink) +
        sliderRowHtml("运动速度", g("idle_speed", 100) + " Units/s", "vl_idle_speed", 0, VL_SPEED_MAX, g("idle_speed", 100)) + '</div>';
    } else {
      var bLink = !!cfg.burst_link, bSpeedLink = !!cfg.burst_speed_link;
      var bLo = bLink ? mainLo : g("burst_min", 0), bHi = bLink ? mainHi : g("burst_max", 100);
      var bTxt = bLink ? (bLo + "% - " + bHi + "%（关联主输出）") : (bLo + "% - " + bHi + "%");
      var bSpd = bSpeedLink ? mainSpd : g("burst_speed", 500);
      var bSpdTxt = bSpeedLink ? (bSpd + " Units/s（关联主上限）") : (bSpd + " Units/s");
      html = '<div class="steel-card-title">一键爆发</div><div class="steel-rows">' +
        dualSliderHtml("vl_burst_range", 0, 100, bLo, bHi, "运动范围", bTxt, bLink) +
        linkBoxHtml("vl_burst_link", bLink) +
        sliderRowHtml("运动速度", bSpdTxt, "vl_burst_speed", 0, VL_SPEED_MAX, bSpd, !bSpeedLink) +
        linkBoxHtml("vl_burst_speed_link", bSpeedLink, "关联上限") + '</div>';
    }
    box.innerHTML = html;
    if (vlTab === "stroke") {
      initSlider("vl_range", true, function (lo, hi, live) {
        $("#vl_range_val").textContent = lo + "% - " + hi + "%";
        if (!live) queueVl({ range_min: lo, range_max: hi });
      });
      initSlider("vl_max_speed", false, function (lo, hi, live) {
        $("#vl_max_speed_val").textContent = hi + " Units/s";
        if (!live) queueVl({ max_speed: hi });
      });
    } else if (vlTab === "idle") {
      initSlider("vl_idle_range", true, function (lo, hi, live) {
        $("#vl_idle_range_val").textContent = lo + "% - " + hi + "%";
        if (!live) queueVl({ idle_min: lo, idle_max: hi });
      });
      initSlider("vl_idle_speed", false, function (lo, hi, live) {
        $("#vl_idle_speed_val").textContent = hi + " Units/s";
        if (!live) queueVl({ idle_speed: hi });
      });
      bindLink("vl_idle_link", "idle_link");
    } else {
      initSlider("vl_burst_range", true, function (lo, hi, live) {
        $("#vl_burst_range_val").textContent = lo + "% - " + hi + "%";
        if (!live) queueVl({ burst_min: lo, burst_max: hi });
      });
      initSlider("vl_burst_speed", false, function (lo, hi, live) {
        $("#vl_burst_speed_val").textContent = hi + " Units/s";
        if (!live) queueVl({ burst_speed: hi });
      });
      bindLink("vl_burst_link", "burst_link");
      bindLink("vl_burst_speed_link", "burst_speed_link");
    }
  }
  /* 关联勾选：存盘后立刻重画（滑轨要禁用、数值要跟着主范围走） */
  function bindLink(id, key) {
    var el = $("#" + id);
    if (!el) return;
    el.addEventListener("change", function () {
      var patch = {}; patch[key] = this.checked;
      if (S.settings) {
        S.settings.video_link = Object.assign({}, vlCfg(), patch);   // 本地先认，重画才拿得到新值
      }
      saveVl(patch);
      renderVlTabCard();
    });
  }
  $$(".vl-tabs button").forEach(function (b) {
    b.addEventListener("click", function () {
      vlTab = this.getAttribute("data-vl-tab");
      $$(".vl-tabs button").forEach(function (x) { x.removeAttribute("aria-selected"); });
      this.setAttribute("aria-selected", "true");
      renderVlTabCard();
    });
  });

  /* --- 中列快捷动作卡（严格手机：激活填充变色 + 文字切换） --- */
  function quickCmd(kind, on) {
    api("/api/quick", "POST", { kind: kind, on: !!on }).then(function (r) {
      if (!r || !r.ok) { toast("命令失败", (r && r.error) || "宿主未提供设备接口（需 1.0.57）", "err"); return; }
      if (kind === "stop") {
        SYNC.estopped = !!on;
        if (on) { SYNC.on = false; SYNC.want = false; }   // 急停停了会话（I6）：不自动恢复
      }
      pollDev();
    });
  }
  $("#vqIdle").addEventListener("click", function () { quickCmd("slow", !DEV.quick.slow); });
  $("#vqBurst").addEventListener("click", function () { quickCmd("orgasm", !DEV.quick.orgasm); });
  $("#vqStop").addEventListener("click", function () { quickCmd("stop", !DEV.quick.stop); });

  /* --- 右上三胶囊（严格手机三圆钮语义：BOOST 红 / 播放蓝 / RANDOM 绿） --- */
  /* =====================================================================
     预设类按钮的两段式确认（用户指定）：
     当前处于"脚本联动模式"（内置播放器在播 + 脚本同步已开启）时，点这三个按钮
     不直接执行，而是把按钮变成**红色告警态 + 文案「将退出联动，确定请再点击」**，
     再点一次才执行（并停止脚本同步，与手机端"进预设前清空脚本"同一语义）。
     ===================================================================== */
  var VL_ARM_TEXT = "将退出联动，确定请再点击";
  var vlArmed = null;          // 已进入告警态的按钮 id
  var vlArmTimer = 0;
  function vlInScriptLink() {
    /* 只在脚本同步真在跑时才需要二次确认——PC 是集成页，视频与预设可以共存，
       "视频在播但没脚本"时没有可退出的联动，弹确认只会让按钮状态显得混乱。
       （方向 B 的互斥——预设中开播视频要停预设——由宿主 arbiter 统一仲裁，不靠这里。） */
    return !!(typeof SYNC !== "undefined" && SYNC.on);
  }
  function vlDisarm(reset) {
    clearTimeout(vlArmTimer);
    if (!vlArmed) return;
    var btn = document.getElementById(vlArmed.id);
    vlArmed = null;
    if (btn) {
      btn.classList.remove("arm");
      /* 文字/配色不回填快照：交回 renderDev 按宿主状态重画（快照可能已过时） */
      if (typeof renderDev === "function") renderDev();
    }
  }
  function vlArm(btn) {
    vlDisarm();
    vlArmed = { id: btn.id };
    btn.classList.add("arm");
    btn.textContent = VL_ARM_TEXT;
    vlArmTimer = setTimeout(function () { vlDisarm(); }, 5000);   // 5 秒没再点就撤销
  }
  /** 预设类按钮统一入口：联动模式下第一下变红，第二下执行。 */
  function vlPresetButton(btn, run) {
    if (vlArmed && vlArmed.id === btn.id) {
      vlDisarm();
      syncStop();                     // 退出联动：停脚本同步（手机端进预设前清空脚本）
      run();
      return;
    }
    if (vlInScriptLink()) { vlArm(btn); return; }
    vlDisarm();
    run();
  }
  function presetCmd(action, extra) {
    var body = Object.assign({ action: action }, extra || {});
    api("/api/preset", "POST", body).then(function (r) {
      if (!r || r.ok === false) { toast("预设命令失败", (r && r.error) || "宿主未提供设备接口（需 1.0.57）", "err"); return; }
      pollDev();
    });
  }
  $("#vlBoost").addEventListener("click", function () { presetCmd("boost"); });
  $("#vlRandom").addEventListener("click", function () { presetCmd("random"); });
  /* 中间按钮 = 手机端 PresetPlayer.togglePlay()：一个按钮在播就停、没播就开始 */
  $("#vlPresetToggle").addEventListener("click", function () { vlPresetButton(this, function () { presetCmd("toggle_play"); }); });

  /* --- 预设网格（严格手机 PresetTile：两列、固定 4 个、不滚动） --- */
  function presetPoints(pr) {
    var pts = [];
    var i, acc;
    var felt = pr.previewTo > pr.previewFrom;
    if (felt) {
      if (pr.keyframes && pr.keyframes.length) {
        acc = 0;
        pr.keyframes.forEach(function (kf, idx) {
          if (idx > 0) acc += Math.abs(kf[0] - pr.keyframes[idx - 1][0]);
          pts.push([acc, kf[0]]);
        });
      } else {
        acc = 0;
        pts.push([0, pr.segments[0][0]]);
        pr.segments.forEach(function (sg) { acc += Math.abs(sg[1] - sg[0]); pts.push([acc, sg[1]]); });
      }
    } else if (pr.keyframes && pr.keyframes.length) {
      pr.keyframes.forEach(function (kf) { pts.push([kf[1], kf[0]]); });
    } else {
      var t = 0, pos = pr.segments[0][0];
      pts.push([0, pos]);
      pr.segments.forEach(function (sg) {
        /* x 轴 = 源时间轴毫秒：段时长优先 durationMs(sg[3])，否则 行程×1000/速度。
           旧版 `sg[2] || 0 || …` 拿到段速度(100)恒加 100ms，波形被压扁 */
        t += (sg.length > 3 && sg[3]) ? sg[3] : (Math.abs(sg[1] - sg[0]) * 1000 / Math.max(1, sg[2] || 1));
        pos = sg[1];
        pts.push([t, pos]);
      });
    }
    return pts;
  }
  function presetLoopSec(pr) {
    var travel = 0;
    var segs = pr.segments && pr.segments.length ? pr.segments : null;
    if (segs) segs.forEach(function (sg) { travel += Math.abs(sg[1] - sg[0]); });
    else {
      var kfs = pr.keyframes;
      for (var i = 1; i < kfs.length; i++) travel += Math.abs(kfs[i][0] - kfs[i - 1][0]);
    }
    return travel / 100.0;   // 速度 100 时的秒数（默认配速）
  }
  /** 按当前实际尺寸重画所有预设波形（切页/尺寸变化后调用；尺寸没变就跳过）*/
  function redrawPresetWaves() {
    var g = $("#vlPGrid");
    if (!g || !g.childElementCount) return;
    var d = window.devicePixelRatio || 1;
    $$(".vl-wave", g).forEach(function (cv) {
      var w = cv.clientWidth;
      if (!w) return;
      if (Math.abs(cv.width - Math.round(w * d)) > 2 || !cv.height || cv.height < 20) {
        var pr = (VL_PRESETS || []).find(function (x) { return x.id === cv.getAttribute("data-preset"); });
        if (pr) drawPresetWave(cv, pr);
      }
    });
  }
  function drawPresetWave(cv, pr) {
    var dpr = window.devicePixelRatio || 1;
    var w = cv.clientWidth || 130, h = cv.clientHeight || 40;
    /* 清晰度按 DPR 提升：backing store = CSS 尺寸 × dpr，**CSS 尺寸保持不变**，
       再用 setTransform(dpr…) 让绘制继续用 CSS 像素坐标。
       （上一版把 dpr 乘了两遍还撑大元素宽度，波形只占左边 1/dpr —— 就是用户看到的「偏移」）*/
    cv.width = Math.round(w * dpr);
    cv.height = Math.round(h * dpr);
    var ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);   // 让下面的绘制坐标继续用 CSS 像素
    var padX = 1, padY = 2;
    var topY = padY, botY = h - padY, spanY = botY - topY;
    function yOf(pos) { return botY - spanY * Math.min(1, Math.max(0, pos / 100)); }
    var pts = presetPoints(pr);
    var loopX = pts.length ? pts[pts.length - 1][0] : 0;
    if (pts.length < 2 || loopX <= 0) return;
    var felt = pr.previewTo > pr.previewFrom;
    var winFrom = felt ? loopX * pr.previewFrom / 1000 : 0;
    var winTo = felt ? loopX * pr.previewTo / 1000 : loopX;
    var winLen = winTo - winFrom;
    if (winLen <= 0) return;
    function xOf(x) { return padX + (w - 2 * padX) * ((x - winFrom) / winLen); }
    // 逐循环展开
    var raw = [];
    var cStart = Math.floor(winFrom / loopX), cEnd = Math.ceil(winTo / loopX);
    for (var c = cStart; c <= cEnd; c++) {
      pts.forEach(function (pt) { raw.push([c * loopX + pt[0], pt[1]]); });
    }
    // 折线 + 包络渐变填充（上深下浅 钢蓝）
    var line = new Path2D(), fill = new Path2D();
    fill.moveTo(xOf(winFrom), botY);
    var started = false;
    for (var i = 0; i < raw.length - 1; i++) {
      var ax = raw[i][0], ap = raw[i][1], bx = raw[i + 1][0], bp = raw[i + 1][1];
      if (bx <= winFrom || ax >= winTo) continue;
      var dx = bx - ax;
      var t0 = dx <= 0 ? 0 : Math.min(1, Math.max(0, (winFrom - ax) / dx));
      var t1 = dx <= 0 ? 1 : Math.min(1, Math.max(0, (winTo - ax) / dx));
      if (t1 < t0) continue;
      var p1 = [ax + dx * t0, ap + (bp - ap) * t0], p2 = [ax + dx * t1, ap + (bp - ap) * t1];
      if (!started) { line.moveTo(xOf(p1[0]), yOf(p1[1])); fill.lineTo(xOf(p1[0]), botY); started = true; }
      line.lineTo(xOf(p1[0]), yOf(p1[1])); fill.lineTo(xOf(p1[0]), yOf(p1[1]));
      line.lineTo(xOf(p2[0]), yOf(p2[1])); fill.lineTo(xOf(p2[0]), yOf(p2[1]));
    }
    if (!started) return;
    fill.lineTo(xOf(winTo), botY);
    fill.closePath();
    var grd = ctx.createLinearGradient(0, topY, 0, botY);
    grd.addColorStop(0, "rgba(74,95,138,0.24)");
    grd.addColorStop(1, "rgba(74,95,138,0.03)");
    ctx.fillStyle = grd;
    ctx.fill(fill);
    ctx.strokeStyle = "#4A5F8A";
    ctx.lineWidth = 1.5;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.stroke(line);
  }
  var vlSelRendered = "";
  function renderPresetCards() {
    var grid = $("#vlPGrid");
    if (!grid) return;
    if (VL_PRESETS == null) {
      fetch("/presets.json").then(function (r) { return r.json(); }).then(function (list) {
        VL_PRESETS = list;
        renderPresetCards();
      }).catch(function () { VL_PRESETS = []; });
      return;
    }
    /* 选中态**只认宿主**（手机端 PresetTile：selected = currentPresetId == def.id，
       currentId 为 null 时全部不选）——本地不留副本，否则宿主没选中时还会残留高亮 */
    var cur = (DEV && DEV.preset && DEV.preset.selected) ? DEV.preset.selected : "";
    if (!grid.childElementCount) {
      grid.innerHTML = VL_PRESETS.map(function (pr) {
        var loopSec = presetLoopSec(pr);
        var label = "循环 " + (loopSec >= 100 ? Math.round(loopSec) + "s" : loopSec.toFixed(1) + "s");
        return '<div class="vl-pcard" data-preset="' + pr.id + '">' +
          '<div class="vl-phead-row"><span class="vl-pname">' + pr.name + '</span>' +
          '<span class="vl-ploop">' + label + '</span></div>' +
          '<canvas class="vl-wave" data-preset="' + pr.id + '"></canvas></div>';
      }).join("");
      $$(".vl-wave", grid).forEach(function (cv) {
        var pr = (VL_PRESETS || []).find(function (x) { return x.id === cv.getAttribute("data-preset"); });
        if (pr) requestAnimationFrame(function () { drawPresetWave(cv, pr); });
      });
    }
    $$(".vl-pcard", grid).forEach(function (card) {
      var on = card.getAttribute("data-preset") === cur;
      card.classList.toggle("selected", on);
      var nm = card.querySelector(".vl-pname");
      if (nm) {
        nm.classList.toggle("sel", on);
        var base = (VL_PRESETS || []).find(function (x) { return x.id === card.getAttribute("data-preset"); });
        nm.textContent = (on && vlPlayingPreset ? "▶ " : "") + ((base && base.name) || "");
      }
    });
    if (cur !== vlSelRendered) {          /* 选中变化 → 滚动到可见（手机端随机跳转就是这个行为）*/
      var el = grid.querySelector('.vl-pcard[data-preset="' + cur + '"]');
      if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
      vlSelRendered = cur;
    }
  }
  $("#vlPGrid").addEventListener("click", function (e) {
    var card = e.target.closest(".vl-pcard");
    if (!card) return;
    var id = card.getAttribute("data-preset");
    /* 手机端：点卡片就是选中（不做"再点取消"），选中本身不开始播放 */
    api("/api/preset", "POST", { action: "select", id: id }).then(function () { pollDev(); });
  });

  /* =====================================================================
     设备通道（M2）：连接 BLE 玩具 / 快捷动作 / 预设播放。
     状态一律以 /api/device/state 为准（2 秒轮询），按钮只发命令，不自己编状态。
     ===================================================================== */
  var DEV = { available: null, connected: false, connecting: false, name: "", info: {},
              quick: { slow: false, orgasm: false, stop: false },
              preset: { playing: false, selected: null, random: false, boost: false } };

  function renderDev() {
    var btn = $("#devBtn"), lb = $("#devLabel"), sp = $("#devSpinner");
    if (!btn) return;
    btn.classList.remove("busy", "on");
    sp.hidden = true;
    if (DEV.available === false) { lb.textContent = "无 BLE 依赖"; btn.title = "宿主缺少 bleak，需重新打包"; return; }
    if (DEV.connecting) { btn.classList.add("busy"); sp.hidden = false; lb.textContent = "连接中"; return; }
    if (DEV.connected) { btn.classList.add("on"); lb.textContent = "断开设备"; btn.title = (DEV.name || "设备") + " 已连接（点击断开）"; }
    else { lb.textContent = "扫描并连接"; btn.title = "扫描并连接设备"; }
    var q = DEV.quick || {};
    $("#vqIdle").textContent = q.slow ? "停止缓动" : "待机缓动";
    $("#vqIdle").classList.toggle("filled-green", !!q.slow);
    $("#vqBurst").textContent = q.orgasm ? "停止爆发" : "一键爆发";
    $("#vqBurst").classList.toggle("filled-red", !!q.orgasm);
    $("#vqStop").textContent = q.stop ? "一键继续" : "一键急停";
    $("#vqStop").classList.toggle("filled-blue", !!q.stop);
    var p = DEV.preset || {};
    /* 告警态（两段式确认）优先：轮询不得覆盖 arm 文案，否则确认被 2s 轮询冲掉，
       用户第二次点击就落空/或直接执行——按钮状态"极其混乱"的主因 */
    if (!(vlArmed && vlArmed.id === "vlPresetToggle")) {
      $("#vlPresetToggle").textContent = p.playing ? "暂停预设" : "播放预设";
      $("#vlPresetToggle").classList.toggle("filled-blue", !!p.playing);
    }
    $("#vlRandom").classList.toggle("filled-green", !!p.random);
    $("#vlBoost").classList.toggle("filled-red", !!p.boost);
    var psEl = $("#vlPresetSpeed");                       // 预设速度回填（宿主为准）
    var psVal = Number(p.speed || 100);
    if (psEl) { psEl.style.setProperty("--a", 0); psEl.style.setProperty("--b", (psVal - 1) / 499); psEl.setAttribute("data-val", psVal); }
    if ($("#vlPresetSpeedVal")) $("#vlPresetSpeedVal").textContent = psVal;
    /* 选中态/播放态/滚动定位都在这里：手机端 LaunchedEffect(currentId) 滚到当前行
       同款——RANDOM 跳到哪个、用户点了哪个，都以宿主 currentPresetId 为准重画 */
    renderPresetCards();
    renderSetDev();      // 设置页「设备」块跟着一起刷新
  }
  /* 预设速度滑轨（1..500，默认 100）——手机端 presetSpeed：
     防抖 300ms 下发（宿主会同时落盘，见 /api/preset 的 speed 分支） */
  var vlPresetSpeedTimer = 0;
  initSlider("vlPresetSpeed", false, function (lo, hi, live) {
    var v = Math.max(1, Math.round(hi));
    if ($("#vlPresetSpeedVal")) $("#vlPresetSpeedVal").textContent = v;
    clearTimeout(vlPresetSpeedTimer);
    vlPresetSpeedTimer = setTimeout(function () { presetCmd("speed", { speed: v }); }, live ? 300 : 0);
  });
  /* 恢复默认 100（手机端 Screens.kt「恢复默认」，100 时即原始配速） */
  $("#vlPresetDefault").addEventListener("click", function () {
    if ($("#vlPresetSpeedVal")) $("#vlPresetSpeedVal").textContent = 100;
    presetCmd("speed", { speed: 100 });
  });

  /* ---- 设置页：设备与同步（M2）---- */
  var SET_DEV = { preset_speed: 100, a10_mode: null, oc_mode: false,
                  slow: { idle_detect_seconds: 5 } };
  function renderSetDev() {
    if (!$("#setDevName")) return;
    var i = DEV.info || {}, sy = DEV.sync || {}, on = !!DEV.connected;
    $("#setDevName").textContent = on ? (DEV.name || "设备") : "未连接设备";
    $("#setDevInfo").textContent = on
      ? ("硬件版本：" + (i.hardware || "—") + " · 固件版本：" + (i.software || "—") + " · 设备最大速度：" + (i.max_speed || "—"))
      : "硬件版本：— · 固件版本：— · 设备最大速度：—";
    $("#setDevConnect").textContent = on ? "断开设备" : "扫描并连接";
    $("#setA10").checked = Number(DEV.a10_mode) === 1;
    $("#setReversed").checked = !!DEV.reversed;
    if (document.activeElement !== $("#setOcMode")) $("#setOcMode").checked = !!SET_DEV.oc_mode;
    if ($("#setSyncState")) {
      $("#setSyncState").textContent = sy.active ? ("脚本同步中 · " + (sy.script || "")) : "脚本同步待命";
      $("#setSyncSub").textContent = sy.active
        ? ("已发 " + (sy.sent || 0) + " 帧 · 空闲跳过 " + (sy.skipped || 0) + " · 延迟 " + Math.round(sy.delay_ms || 0) + "ms")
        : (on ? "播放带脚本的视频会自动开始" : "未连接设备");
    }
    if ($("#setDelayVal")) $("#setDelayVal").textContent = Math.round(sy.delay_ms || 0) + " ms";
  }
  function saveDev(patch) {
    api("/api/device/settings", "POST", patch).then(function (r) {
      if (r && r.ok) { toast("已保存", "", "ok"); pollDev(); }
      else toast("保存失败", (r && r.error) || "宿主未提供设备接口（需 1.0.57）", "err");
    });
  }
  function loadSetDev() {
    api("/api/settings").then(function (r) {
      var d = (r && r.settings && r.settings.device) || {};
      SET_DEV = Object.assign(SET_DEV, d);
      if (d.slow) SET_DEV.slow = Object.assign({ idle_detect_seconds: 5 }, d.slow);
      /* 对齐手机端：skipIdleEnabled 默认 false、阈值默认 60s（AppViewModel.kt:896/899）——
         不再用"缺键视为开"的兜底 */
      if ($("#setSkipIdle")) $("#setSkipIdle").checked = d.skip_idle === true;
      if ($("#setIdleThreshold")) $("#setIdleThreshold").value = d.idle_threshold || 60;
      if ($("#setSlowIdle")) $("#setSlowIdle").value = SET_DEV.slow.idle_detect_seconds;
      if ($("#setOcMode")) $("#setOcMode").checked = !!d.oc_mode;
      if ($("#setScriptFolder") && r && r.settings) $("#setScriptFolder").value = r.settings.script_folder || "";
    });
  }
  function bindSetDev() {
    if (!$("#setDevConnect")) return;
    $("#setDevConnect").addEventListener("click", function () { $("#devBtn").click(); });
    $("#setDevRefresh").addEventListener("click", function () {
      api("/api/device/refresh", "POST", {}).then(function () { pollDev(); toast("已请求刷新", "设备信息稍后更新", "ok"); });
    });
    $("#setGotoSync") && $("#setGotoSync").addEventListener("click", function () {
      var b = document.querySelector('button[data-page="sync"]'); if (b) b.click();
    });
    $("#setSlowIdle").addEventListener("change", function () { saveDev({ slow: { idle_detect_seconds: Number(this.value) } }); });
    $("#setSkipIdle").addEventListener("change", function () { saveDev({ skip_idle: this.checked }); });
    $("#setIdleThreshold").addEventListener("change", function () { saveDev({ idle_threshold: Number(this.value) }); });
    initSlider("setManualPos", false, function (lo, hi) {
      if ($("#setManualVal")) $("#setManualVal").textContent = Math.round(hi) + "%";
    });
    $("#setManualMove").addEventListener("click", function () {
      var el = $("#setManualPos");
      var pct = Number(el && el.getAttribute("data-val") || 50);
      var b = el && el.style.getPropertyValue("--b");
      if (b) pct = Math.round(Number(b) * 100);
      /* speed=200：对齐手机端「移动到该位置」固定 200（Screens.kt:1835），
         不传的话宿主落到 max_speed（500），比手机端快 2.5 倍 */
      api("/api/device/move", "POST", { percent: pct, speed: 200 }).then(function (r) {
        toast(r && r.ok ? ("已移动到 " + pct + "%") : "移动失败（设备未连接？）", "", r && r.ok ? "ok" : "warn");
      });
    });
    Array.prototype.forEach.call(document.querySelectorAll("[data-delay]"), function (btn) {
      btn.addEventListener("click", function () {
        var cur = Number((DEV.sync && DEV.sync.delay_ms) || 0);
        var next = cur + Number(btn.getAttribute("data-delay"));
        next = Math.max(-2000, Math.min(2000, next));
        api("/api/sync/delay", "POST", { ms: next }).then(function () { pollDev(); toast("延迟 " + (next > 0 ? "+" : "") + next + " ms", "负值提前，正值滞后", "ok"); });
      });
    });
    $("#setA10").addEventListener("change", function () { saveDev({ a10_mode: this.checked ? 1 : 0 }); });
    $("#setReversed").addEventListener("change", function () { saveDev({ reversed: this.checked }); });
    $("#setOcMode").addEventListener("change", function () {
      var on = this.checked, self = this;
      if (on && !window.confirm("确认开启狂暴模式？\n扭矩约提升 30%，动力更强；\n若行程、限速设置不当，受伤风险将明显增加。")) { self.checked = false; return; }
      saveDev({ oc_mode: on });
    });
    $("#setScriptFolderPick") && $("#setScriptFolderPick").addEventListener("click", function () {
      var cur = $("#setScriptFolder").value || "";
      if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder) {
        window.pywebview.api.pick_folder(cur, true).then(function (r) {
          var p = (r && (r.path || r.folder)) || (typeof r === "string" ? r : "");
          if (p) { $("#setScriptFolder").value = p; api("/api/settings", "POST", { script_folder: p }); }
        });
      }
    });
    $("#setScriptFolder") && $("#setScriptFolder").addEventListener("change", function () {
      api("/api/settings", "POST", { script_folder: this.value }).then(function () { toast("已保存", "", "ok"); });
    });
  }
  loadSetDev();

  /* ---- 脚本同步：播放进度 → 设备 ---- */
  /* 「播放视频时同步驱动设备」开关已删（手机端没有此项，视频在播即同步） */
  var SYNC = { on: false, last: 0, path: "", estopped: false, scriptless: "", starting: false, want: false };
  var syncStartGen = 0;
  function syncStart(path) {
    SYNC.want = true;               // "这个视频该联动"——设备不在也先记下，连上后看护会补起
    if (!DEV.connected || !path || SYNC.starting) return;
    SYNC.starting = true;
    var gen = ++syncStartGen;       // 代际丢弃：连开两个视频，慢的旧 start 响应不得回退状态
    api("/api/sync/start", "POST", { path: path }).then(function (r) {
      SYNC.starting = false;
      if (gen !== syncStartGen) return;
      /* deferred：爆发在跑，宿主让脚本让路（手机端 externalControl 同款）——
         不算"同步已开"，看护会在爆发结束后自动补起 */
      if (r && r.ok) { SYNC.on = !r.deferred; SYNC.path = r.script || path; }
      else if (r && r.error && !r.no_device) { SYNC.on = false; SYNC.want = false; SYNC.scriptless = path; }
    });
  }
  function syncStop() {
    SYNC.on = false;
    SYNC.want = false;              // 用户明确停联动：看护不得再自动拉起
    api("/api/sync/stop", "POST", {});
  }
  function syncTick(t) {
    if (!SYNC.on) return;
    var v = vlVid();
    if (v && v.paused) return;    // 暂停中（含拖进度条）不追帧：设备不被 seek 目标拖着跑
    var now = Date.now();
    if (now - SYNC.last < 180) return;
    SYNC.last = now;
    api("/api/sync/tick", "POST", { t: t }).then(function (r) {
      /* 「跳过无动作部分」（对齐手机端 maybeSkipIdle）：宿主判定脚本静止段超过阈值时
         让**视频快进**到下一动作点；离目标太远才跳，防 seek 环 */
      if (r && r.ok && r.seek_to != null && isFinite(r.seek_to)) {
        var vv = vlVid();
        if (vv && !vv.paused && !vv.seeking && Math.abs((vv.currentTime || 0) - r.seek_to) > 1.5) vlSeekTo(r.seek_to);
      }
    });
  }

  function pollDev() {
    api("/api/device/state").then(function (r) {
      if (!r || !r.ok || !r.device) { DEV.available = false; renderDev(); return; }
      var d = r.device;
      DEV.available = d.available !== false;
      DEV.connected = !!d.connected;
      DEV.name = d.name || "";
      DEV.info = d.info || {};
      DEV.quick = d.quick || DEV.quick;
      DEV.preset = d.preset || DEV.preset;
      DEV.sync = d.sync || null;                    // 设置页"脚本同步中"回显（此前从未赋值）
      /* 高级设备设置的回显（伪装设备/反转方向）：此前从未拷贝这两个键，
         renderSetDev 读的 DEV.a10_mode / DEV.reversed 恒为 undefined →
         设置页开关永远显示"关"，不反映设备当前状态 */
      DEV.reversed = !!d.reversed;
      DEV.a10_mode = d.a10_mode;
      vlPlayingPreset = !!(DEV.preset && DEV.preset.playing);   // 预设卡"▶"回显（此前从未赋值）
      renderDev();
      /* 同步看护：视频在播、设备在线、该联动(want)但会话没起来 → 自动补起。
         覆盖"先播视频加载脚本、后连设备"（syncStart 当时因未连接早退）与
         断线重连后会话丢失。不碰的两种情况：急停（会话已按 I6 停止且不自动恢复，
         用户重按播放/重开联动才复活）；用户自己停过联动或视频无脚本（want=false）。 */
      var v = vlVid();
      if (SYNC.want && DEV.connected && !DEV.quick.stop && !SYNC.estopped
          && vlMed.path && v && !v.paused && SYNC.scriptless !== vlMed.path
          && !SYNC.starting && (!DEV.sync || !DEV.sync.active)) {
        syncStart(vlMed.path);
      }
    }).catch(function () { DEV.available = false; renderDev(); });
  }
  setInterval(pollDev, 2000);
  pollDev();

  $("#devBtn").addEventListener("click", function () {
    if (DEV.connecting) return;
    if (DEV.connected) {
      api("/api/device/disconnect", "POST", {}).then(function (r) {
        toast(r && r.ok ? "设备已断开" : "断开失败", "", r && r.ok ? "ok" : "err");
        pollDev();
      });
      return;
    }
    DEV.connecting = true; renderDev();
    toast("正在扫描设备", "BLE 扫描约 6 秒", "ok");
    api("/api/device/scan", "POST", {}).then(function (r) {
      DEV.connecting = false;
      if (!r || !r.ok) { renderDev(); toast("扫描失败", (r && r.error) || "宿主未提供设备接口（需 1.0.57）", "err"); return; }
      var hit = (r.devices || []).filter(function (d) { return d.supported; })[0];
      if (!hit) { renderDev(); toast("未发现受支持的设备", "支持 ServeU / VorzePiston", "warn"); return; }
      DEV.connecting = true; renderDev();
      api("/api/device/connect", "POST", { address: hit.address, toy: hit.toy }).then(function (c) {
        DEV.connecting = false;
        if (c && c.ok) {
          var info = (c.state && c.state.info) || {};
          toast("已连接 " + (hit.name || hit.address), info.hardware ? ("硬件 v" + info.hardware + " · 固件 v" + info.software + " · 最高 " + info.max_speed) : "", "ok");
        } else {
          toast("连接失败", (c && c.error) || "", "err");
        }
        pollDev();
      });
    });
  });

  /* --- 布局自适应：⏸ 保持正圆、预设/浏览网格"正好 2 行"（草图口径） --- */
  function layoutVl() {
    var row = $(".vl-btnrow"), b = $("#vlPause");
    if (row && b) {
      var s = Math.max(26, Math.min(row.clientHeight, row.clientWidth * 40 / 770));
      b.style.width = s + "px"; b.style.height = s + "px";
    }
    var g = $("#vlPGrid");
    if (g) {
      var h = g.clientHeight;
      if (h > 0) g.style.gridAutoRows = Math.max(46, (h - 10) / 2) + "px";
    }
    var bg = $("#vlBrowseBody");
    if (bg) {
      var bh = bg.clientHeight;
      if (bh > 0) bg.style.gridAutoRows = Math.max(118, (bh - 10) / 2) + "px";
    }
    drawHeat();
    /* 卡片宽度变了要按新尺寸重画波形（否则沿用旧 backing，看着像只画了一半）*/
    var _g = $("#vlPGrid");
    if (_g) $$(".vl-wave", _g).forEach(function (cv) {
      var _w = cv.clientWidth, _d = (window.devicePixelRatio || 1);
      if (_w && Math.abs(cv.width - Math.round(_w * _d)) > 2) {
        var _pr = (VL_PRESETS || []).find(function (x) { return x.id === cv.getAttribute("data-preset"); });
        if (_pr) drawPresetWave(cv, _pr);
      }
    });   /* __wavesRedraw */
  }
  window.addEventListener("resize", function () { heatOffKey = ""; layoutVl(); });

  /* --- 标题栏最大化 --- */
  /* 最大化/还原。
     ⚠ 1.0.55 的 exe 里宿主 win_maximize 用的是 ctypes.wintypes.WINDOWPLACEMENT ——
     那个结构体在 ctypes.wintypes 里不存在，调用必然抛错（实测返回 ok:false）。
     窗口是全屏 API 也救不了：WebView2 里 requestFullscreen 只改 DOM 状态，
     pywebview 不处理 ContainsFullScreenElementChanged，原生窗口一动不动
     （实测点击前后窗口都是 960x806 at 360,103）。所以宿主修好之前，
     这里就**如实报错**，不做"看起来在动其实没动"的假动作。
     host_server.py 里的实现已重写并单独验证过，装 1.0.56 后自动生效。 */
  $("#winMax").addEventListener("click", function () {
    var api = window.pywebview && window.pywebview.api;
    if (!api || !api.win_maximize) {
      toast("最大化不可用", "宿主接口缺失", "warn");
      return;
    }
    try {
      var p = api.win_maximize();
      if (p && p.then) {
        p.then(function (r) {
          if (r && r.ok) return;
          toast("最大化暂不可用", "1.0.55 宿主该接口有 bug（源码已修），装 1.0.56 后恢复", "warn");
        }, function () { toast("最大化暂不可用", "宿主调用失败", "warn"); });
      }
    } catch (e) {
      toast("最大化暂不可用", "宿主调用失败", "warn");
    }
  });

  // 进页面初始化
  vlInitPlayer();
  browse("");
  renderVlTabCard();
  renderDev();      // 设备/快捷动作/预设状态由 /api/device/state 轮询驱动
  setInterval(redrawPresetWaves, 1500);   // 兜底：尺寸稳定后补画（切页/首次布局）
  renderPresetCards();
  // hash 深链（自检截图用）：#library / #library/browse=<路径> / #library/play=<路径>
  window.addEventListener("hashchange", function () { vlApplyHash(); });
  vlApplyHash();
  function vlApplyHash() {
    var h = decodeURIComponent(location.hash || "");
    if (h.indexOf("#library") !== 0) return;
    if (h.indexOf("#library/browse=") === 0) {
      showPage("library");
      browse(h.slice("#library/browse=".length));
    } else if (h.indexOf("#library/play=") === 0) {
      showPage("library");
      var vp = h.slice("#library/play=".length);
      /* 深链直接进内置播放器（原来这里会拉起外部 mpv） */
      openVideo(vp, String(vp).split(/[\\/]/).pop(), [], 0);
    } else if (h === "#library") {
      showPage("library");
    }
  }

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
    /* 主题/动效：默认浅色 + 用户上次的选择已由 <head> 内联脚本在首屏前套好，
       这里只把图标/文案对齐当前值。绝不在 boot 里硬套 dark——那就是
       "启动先深色再跳浅色"的元凶。 */
    setTheme(document.documentElement.getAttribute("data-theme") || "light", false);
    setMotion(document.documentElement.getAttribute("data-motion") || "full", false);
    initPointerLight();
    poll(false);
    loadSubtitleConfig();
    loadModels();
    // 2.5s 后补拉一次字幕配置（防首次请求早于服务就绪），但用户已经开始改
    // 配置输入框时不要覆盖他的输入
    setTimeout(function () { if (!S.subCfgDirty) loadSubtitleConfig(); }, 2500);
    // 配置输入一旦被用户动过就标记：之后的自动回填一律让路
    ["mtBackend", "mtModel", "mtBase", "mtLocalModel", "mtLocalModelEn", "mtCloudBase", "mtCloudModel", "mtCloudKey", "subIdleRelease"
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
