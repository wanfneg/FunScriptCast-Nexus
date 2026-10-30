/* ============================================================
   FunScriptCast-Nexus —— 前端逻辑
   与 Rust 宿主的 /api/* 通信；状态 1s 轮询（页面隐藏时暂停）
   ============================================================ */
(function () {
  "use strict";

  /* ---- 全局错误上报 ----
     打包版是 WebView2 无控制台窗口，前端异常此前**完全不可见**：一个元素缺失
     导致 bind() 中途抛错，后面的按钮（关闭窗口、添加目录…）全部静默失效，
     用户只看到"点了没反应"，日志里一个字都没有。
     这里把未捕获异常与 Promise rejection 送到宿主 /api/logs，排障时有据可查。 */
  function reportJsError(kind, msg, detail) {
    try {
      fetch("/api/js-error", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ kind: kind, message: String(msg || ""),
                               detail: String(detail || ""),
                               url: location.href })
      }).catch(function () {});
    } catch (e) { /* 上报失败不能再抛 */ }
  }
  window.addEventListener("error", function (e) {
    reportJsError("error", e.message,
      (e.filename || "") + ":" + (e.lineno || 0) + ":" + (e.colno || 0) +
      (e.error && e.error.stack ? "\n" + e.error.stack : ""));
  });
  window.addEventListener("unhandledrejection", function (e) {
    var r = e.reason;
    reportJsError("rejection", (r && (r.message || r)) || "unknown",
      r && r.stack ? r.stack : "");
  });

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
  function applyLibAspect(v) {
    document.documentElement.style.setProperty(
      "--lib-aspect", v === "4:3" ? "4 / 3" : "16 / 9"
    );
    requestAnimationFrame(function () {
      if (typeof layoutLibGrid === "function") layoutLibGrid();
    });
  }
  function saveSetting(id, key, value, onDone) {
    var body = {};
    body[key] = value;
    // 同键串行化：快速连点产生并发 POST 时，服务端 patch 读改写的到达顺序
    // 不定，落盘值可能与 UI 相反（后写者覆盖先写者、且两次都显示已保存）
    var prev = PENDING[id] || Promise.resolve();
    var p = prev.catch(function () {}).then(function () {
      return api("/api/settings", "POST", body);
    }).then(function (r) {
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
    /* 15s 超时：宿主挂起时调用方不能永久 pending（如 syncTick 的 inflight 闸门
       会永卡，设备从此一帧不动）。当前所有调用点都是普通 JSON 请求（无 SSE/长连接
       —— /api/library/stream 是直接喂给 <video> 的 src，不走 fetch），统一 15s。 */
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 15000);
    return fetch(path, {
      method: method || "GET",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
      signal: ctrl.signal
    }).then(function (r) { return r.json(); }).catch(function (e) {
      if (e && e.name === "AbortError") toast("请求超时", "15 秒内无响应：" + path, "err");
      else toast("请求失败", String(e), "err");
      return { ok: false };
    }).finally(function () { clearTimeout(timer); });
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
    /* 切到媒体库页：仅在**未打开视频**时刷新列表。打开过视频就保持播放视图——
       browse() 渲染会强制切回浏览视图（vlPlayView 隐藏）但 <video> 仍在播，
       会出现"窗口没了、视频还在放"（用户实测 2026-10-02）。正在播放时什么也不动。 */
    if (name === "library" && !vlMed.path) browse(vlBrowsePath || "");
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
  /* 主题固定浅色（redesign 后的既定设计：切换按钮已隐藏、内联脚本与 boot 均
     强制 light）。保留 setTheme 只是兜底把 data-theme 归位到 light——
     不再写 localStorage/服务端：旧代码写 nxs.theme 却无人读，注释声称
     "下次启动由内联脚本恢复"也不属实（半拆除状态，本次清理）。 */
  function setTheme() {
    document.documentElement.setAttribute("data-theme", "light");
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
    var _subModel = sub.health ? String(sub.health.asr_model || "") : "";
    $("#railSubM").textContent = _subModel ? _subModel.split(/[\\/]/).pop() : "";

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
        : ab === "audiocpp" ? "Qwen3（audio.cpp）"
        : (h.asr_backend || "—");
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
    if (brandSub) brandSub.textContent = st.dev_copy ? "开发副本" : "本机控制台";
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
      setTheme();                 // 固定浅色；旧设置里的 dark 不再生效
      if (s.motion) setMotion(s.motion, false);
    }
    var asp = s.library_card_aspect || "16:9";
    var aspEl = $("#setLibAspect");
    if (aspEl && !busyEditing(aspEl)) aspEl.value = asp;
    applyLibAspect(asp);
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
      // 路径来自用户/扫描结果，可能含 < > & " —— 与上方 renderRoots 同样 esc() 转义
      return '<div class="row flush"><div class="grow"><div class="name mono">' + esc(p) + '</div></div>' +
             '<button class="btn ghost" data-del-libroot="' + esc(p) + '">移除</button></div>';
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
    "vision-module": "AI 实时识别模块（边播边推理）",
    "audiocpp-runtime": "识别运行时 · audio.cpp（CPU）",
    "qwen3-asr": "识别模型 · Qwen3-ASR-0.6B",
    "llama-runtime": "翻译运行时 · llama.cpp（CUDA）",
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
      // AI 实时识别模块属于“视频联动/AI 识别”，不是字幕依赖，不在字幕页展示
      (r.items || []).filter(function (m) { return m.role !== "vision-module"; }).forEach(function (m) {
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
        if (m.state === "done" && (m.role === "translate" || m.role === "translate-model") && !S.modelDoneSeen[m.id]) {
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
    /* ---- 关键绑定优先 + 逐段隔离 ----
       历史教训（2026-10-18）：bind() 里有 50 处 `$("#id").addEventListener`
       没有任何 null 保护。只要**任意一个**元素缺失，整条 bind() 就在那里抛错，
       它后面的所有按钮全部静默失效——用户看到的是"关闭窗口点不动、添加目录
       点了没反应"，而日志里一个字都没有（打包版是 WebView2 无控制台）。
       所以：① 窗口控制与目录添加这两类"用户最先撞上"的绑定放在最前面；
       ② 每段用 safe() 包住，一段失败只记日志，不拖垮后面。 */
    function safe(label, fn) {
      try {
        fn();
      } catch (e) {
        reportJsError("bind", label + " 绑定失败: " + (e && e.message),
                      e && e.stack ? e.stack : "");
        console.error("[bind] " + label + " 失败", e);
      }
    }

    /* 窗口控制（关闭/最小化）——必须最先绑上，否则用户无法退出程序 */
    safe("窗口控制", function () {
      function winCall(name) {
        var api2 = window.pywebview && window.pywebview.api;
        if (!api2 || !api2[name]) { toast("窗口操作不可用", "pywebview 未就绪", "err"); return; }
        api2[name]().then(function (r) {
          if (r && r.ok === false) toast("窗口操作失败", r.error || "", "err");
        }).catch(function (e) { toast("窗口操作失败", String(e), "err"); });
      }
      var wmin = $("#winMin"), wclose = $("#winClose");
      if (wmin) wmin.addEventListener("click", function () { winCall("win_minimize"); });
      if (wclose) wclose.addEventListener("click", function () { winCall("win_close"); });
    });

    /* 导航指示条初始位置 */
    setTimeout(function () { moveIndicator($('button[aria-current="true"]', nav)); }, 60);

    /* 主题固定浅色：切换按钮已被样式隐藏（redesign 既定设计），不再绑定 */

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
        // GET 失败（超时/网络错）时 api() 回 {ok:false}，此时绝不能拿空列表
        // 当基底全量回写——POST 一旦成功会把原有全部目录清空（数据丢失）
        if (!cur || cur.ok === false) {
          toast("读取当前目录列表失败", "已取消本次操作，请稍后重试", "err");
          return;
        }
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
      }).catch(function (e) {
        toast("添加媒体根失败", String(e), "err");
      });
      return rootsOpChain;   // 链上排队：并发调用共享同一条串行链
    }
    $("#addRoot").addEventListener("click", function () { addRoots([$("#newRoot").value]); });
    /* 媒体库目录：添加（选择器）与移除。
       整段用 safe() 隔离：这里曾因选择器参数问题静默失效，而它前面任何一处
       抛错也会连带打死这段（bind 是单条直线执行流）。 */
    safe("媒体库目录", function () {
    $("#libRootAdd").addEventListener("click", function () {
      if (!bridgeReady()) return;
      /* multiple=false：文件夹选择在 Windows 上不支持多选（见 pick_folder docstring）。
         传 true 时对话框返回空 → 前端以为用户没选 → "点了完全没反应"。 */
      window.pywebview.api.pick_folder($("#newRoot").value || "", false).then(function (r) {
        if (!r || !r.ok) {
          if (r && r.error) toast("选择目录失败", r.error, "err");
          else if (r && r.cancelled) { /* 用户主动取消，不打扰 */ }
          else toast("选择目录失败", "没有拿到路径", "err");
          return;
        }
        addLibRoots(r.paths && r.paths.length ? r.paths : (r.path ? [r.path] : []));
      }).catch(function (e) {
        toast("选择目录失败", String(e), "err");
      });
    });
    var libRootChain = Promise.resolve();
    function addLibRoots(list) {
      /* ⚠ 整条链必须吞掉异常：Promise 链一旦进入 rejected 状态，后续 .then
         会全部跳过——表现就是"第一次添加失败后，之后再点添加永远没反应"
         （用户实际踩到）。所以每一步都自带 catch，链本身始终是 resolved。 */
      libRootChain = libRootChain.then(function () {
        return api("/api/settings").then(function (cur) {
          // GET 失败时拿空列表当基底回写 = 清空全部媒体库目录（数据丢失）
          if (!cur || cur.ok === false) {
            toast("读取当前目录列表失败", "已取消本次操作，请稍后重试", "err");
            return;
          }
          var roots = (((cur || {}).settings || {}).library_roots || []).slice();
          var added = 0;
          (list || []).forEach(function (p) {
            p = String(p == null ? "" : p).trim();
            if (p && roots.indexOf(p) < 0) { roots.push(p); added++; }
          });
          if (!added) {
            toast(list && list.length ? "这些目录已经在列表里了" : "没有选中任何目录", "", "warn");
            return;
          }
          return api("/api/settings", "POST", { library_roots: roots }).then(function (r) {
            if (!r || r.ok === false) { toast("保存失败", (r && r.error) || "", "err"); return; }
            toast("已添加 " + added + " 个媒体库目录", "正在自动扫描生成海报墙", "ok");
            libRootSig = "";   // 强制重渲染
            // rescan 失败（比如"扫描已在进行"返回 400）不该影响添加结果，
            // 也不该让链 rejected —— 单独 catch 掉。
            api("/api/library/rescan", "POST", {}).then(function () {
              browse(vlBrowsePath || "");
            }).catch(function () {});
          });
        });
      }).catch(function (e) {
        toast("添加目录失败", String(e), "err");
      });
      return libRootChain;
    }
    $("#libRootList").addEventListener("click", function (e) {
      var b = e.target.closest("[data-del-libroot]");
      if (!b) return;
      var p = b.getAttribute("data-del-libroot");
      libRootChain = libRootChain.then(function () {
        return api("/api/settings").then(function (cur) {
          // 同 addLibRoots：GET 失败不能拿空列表回写（会清空全部目录）
          if (!cur || cur.ok === false) {
            toast("读取当前目录列表失败", "已取消本次移除，请稍后重试", "err");
            return;
          }
          var roots = (((cur || {}).settings || {}).library_roots || []).filter(function (x) { return x !== p; });
          return api("/api/settings", "POST", { library_roots: roots }).then(function (r) {
            if (!r || r.ok === false) { toast("移除失败", (r && r.error) || "", "err"); return; }
            toast("已移除", p, "ok");
            libRootSig = "";
          });
        });
      });
    });
    });
    /* 系统「选择文件夹」：主入口。手打路径容易带进引号/全角字符，
       而那种错误在头显里只表现为"目录为空"。支持一次多选。 */
    $("#pickRoot").addEventListener("click", function () {
      if (!bridgeReady()) return;
      /* 同 #libRootAdd：文件夹选择不支持多选，传 true 会拿到空结果。 */
      window.pywebview.api.pick_folder($("#newRoot").value || "", false).then(function (r) {
        if (!r || !r.ok) {
          if (r && r.error) toast("选择目录失败", r.error, "err");
          return;
        }
        addRoots(r.paths && r.paths.length ? r.paths : (r.path ? [r.path] : []));
      }).catch(function (e) { toast("选择目录失败", String(e), "err"); });
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
        // GET 失败（超时/网络错）时 api() 回 {ok:false}，此时绝不能拿空列表
        // 当基底全量回写——POST 一旦成功会把原有全部目录清空（数据丢失）
        if (!cur || cur.ok === false) {
          toast("读取当前目录列表失败", "已取消本次操作，请稍后重试", "err");
          return;
        }
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
      api("/api/sync/disconnect", "POST", {}).then(function (r) {
        if (r && r.ok) toast("已断开设备");
        else toast("断开失败", (r && r.error) || "", "err");
        poll(true);
      });
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
          } else if (r && r.error) {
            toast("选择目录失败", r.error, "err");
          }
        }).catch(function (e) { toast("选择目录失败", String(e), "err"); });
      });
    });

    /* 自绘标题栏按钮：**绑定已在 bind() 开头**（关键绑定优先，见那里的说明）。
       这里只保留拖动区的 mousedown 拦截。 */
    // 标题栏整体是拖动区：Rust 宿主不再有 pywebview 的隐式拖拽，
    // 必须在空白处 mousedown 时显式调用 drag_window（按钮/胶囊自己吃掉事件）。
    $$(".titlebar button, .titlebar .pills, .titlebar .dev-connect").forEach(function (el) {
      el.addEventListener("mousedown", function (e) { e.stopPropagation(); });
    });
    var titlebar = document.querySelector(".titlebar");
    if (titlebar) {
      titlebar.addEventListener("mousedown", function (e) {
        if (e.button !== 0) return;
        if (e.target.closest("button, input, select, textarea, a, .pills, .dev-connect")) return;
        var api2 = window.pywebview && window.pywebview.api;
        if (api2 && api2.drag_window) {
          try { api2.drag_window(); } catch (err) {}
        }
      });
    }

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
    if ($("#setLibAspect")) $("#setLibAspect").addEventListener("change", function () {
      applyLibAspect(this.value);
      saveSetting("setLibAspect", "library_card_aspect", this.value);
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

  /* R125 修复（全项目审查中危）：#dlnaPort 一直没接 markDirty——回填判据
     （:322）里 dirty 分支是半成品，改完端口一失焦就被 1s 轮询刷回旧值，
     点「启动」用旧端口并落盘。与其它控件同款接上。 */
  $("#dlnaPort").addEventListener("input", function () { markDirty(this); });
  $("#dlnaPort").addEventListener("change", function () { markDirty(this); });

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
        clearDirty($("#dlnaPort"));   // 已落盘：轮询可恢复回填（R125 配套）
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

    /* 切回浏览视图时，正在播放的视频必须停住——视图一隐藏 <video> 会继续播
       （无声窗口还在出声、进度还在走），用户实测过"窗口没了视频还在放"。 */
    var _pv = vlVid();
    if (_pv && !_pv.paused) { try { _pv.pause(); } catch (e) {} }
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
      /* 观看进度条：pos 是**秒**（后端语义），显示宽度必须换算成百分比——
         旧实现把秒当百分比（35.2 秒画成 35.2%）且以“>5 秒”作显示条件，
         导致“只有进度秒数大的那个视频有诡异长条”。看过 1% 以上才显示，封顶 95%。 */
      var _pct = (v.dur > 0 && v.pos > 0) ? Math.min(95, v.pos / v.dur * 100) : 0;
      var prog = _pct >= 1 ? '<div class="lib-prog"><i style="width:' + _pct.toFixed(1) + '%"></i></div>' : "";
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
    /* 内置播放：直接在大框框里放（外挂 mpv 已弃用删除） */
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
     内置播放器：HTML5 <video> 直接在大框框里播
     流地址优先级：① 宿主 /api/library/stream（1.0.56+）② DLNA /media/<key>
     （DLNA 服务器已支持 Range 206）
     ===================================================================== */
  var VL_STREAM = { api: null };          // null=未探测 / true=宿主有流接口 / false=走 DLNA
  var vlMed = { list: [], idx: -1, path: "", name: "", failed: false };
  var vlVidEl = null, vlChromeTimer = 0;
  function vlVid() { return vlVidEl || (vlVidEl = document.getElementById("vlVideo")); }
  function vlPlaying() { var v = vlVid(); return !!(v && vlMed.path && !v.paused && !v.ended); }

  /* DLNA key 规则（vendor/dlna/vr_dlna.py path_to_key）：单根=相对路径，多根=label/相对路径，
     label = 根目录 basename（宿主 MediaRoot 的 label 规则） */
  /* vlDlnaUrl 已删除：播放不再经过 DLNA */
  function vlStreamUrl(absPath, cb) {
    /* 电脑端播放只走宿主本地流（宿主直接读盘、Range/206）；
       DLNA 是给手机/VR 的出站共享，与电脑自身播放无关。 */
    cb("/api/library/stream?path=" + encodeURIComponent(absPath));
  }

  function vlShowChrome(on) {
    var w = $("#vlVWrap"); if (!w) return;
    w.classList.toggle("idle", !on);
    if (!on) { try { drawMiniHeat(); } catch (e) {} }   /* 隐藏后立刻按新尺寸重画 */
  }
  var vlLastMove = 0;
  function vlTouch() {
    vlShowChrome(true);
    vlLastMove = Date.now();
    clearTimeout(vlChromeTimer);
    vlChromeTimer = setTimeout(function () {
      /* 只有"确实没再动过"才隐藏：避免 pointermove 抖动把计时器无限续期 */
      if (vlPlaying() && Date.now() - vlLastMove >= 3900) vlShowChrome(false);
    }, 4000);
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
  /* 内置播放器进度上报：此前只有外挂 mpv 会记进度（_on_progress），WebView <video>
     看过的视频永远没有"观看进度"（用户实测：看过的视频卡片没有进度条）。 */
  var libProgLast = 0;
  function reportProgress(force) {
    var v = vlVid();
    if (!v || !vlMed.path || vlMed.failed) return;
    var now = Date.now();
    if (!force && now - libProgLast < 5000) return;      // 节流 5s
    if (!isFinite(v.duration) || v.duration <= 0) return;
    libProgLast = now;
    api("/api/library/progress", "POST",
        { path: vlMed.path, pos: v.currentTime || 0, dur: v.duration });
  }
  /* 播放器按钮：AI 识别开关（Win11 实时字幕已整体移除） */
  document.addEventListener("click", function (ev) {
    var t = ev.target;
    if (!t || !t.closest) return;
    if (t.closest("#vlAiScript") || t.closest("#vlAiScript2")) {
      if (AIV.on) aivStop(); else aivStart();
    }
  });
  /* ---------- 播放器热力曲线（移植手机端 ScriptHeatmap.kt） ----------
     算法逐条对齐：
       · 每个动作段按 |Δpos|/dt 得速度(%/s)，均摊到它覆盖的像素列
       · 每列取段均值 → 95 分位归一化（单尖峰不会压平其余）
       · [1,2,1]/4 轻平滑把柱状连成曲线；强度 ≤2% 的列不画（无基线）
       · 色带 黑→蓝→绿→金→红→紫→深紫（n=120 分档）
       · 已播段 alpha=1，未播 0.45                                        */
  var HEAT_COLORS = [[0,0,0],[30,144,255],[34,139,34],[255,215,0],
                     [220,20,60],[147,112,219],[37,22,122]];
  function heatColorRGB(speed) {
    var n = 120;
    if (speed <= 0) return HEAT_COLORS[0];
    if (speed > 5 * n) return HEAT_COLORS[6];
    var t = speed + n / 2;
    var i = Math.min(Math.floor(t / n), HEAT_COLORS.length - 2);
    var f = Math.max(0, Math.min(1, (t - i * n) / n));
    var a = HEAT_COLORS[i], b = HEAT_COLORS[i + 1];
    return [a[0] + (b[0]-a[0])*f, a[1] + (b[1]-a[1])*f, a[2] + (b[2]-a[2])*f];
  }
  var HEAT_GAP_MS = 5000;          // 长空档视为无动作（手机端 GAP_THRESHOLD_MS 同量级）
  function buildHeatMask(actions, durationSec, width) {
    if (!actions || actions.length < 2 || !(durationSec > 0) || width < 4) return null;
    var durMs = durationSec * 1000;
    var acc = new Float64Array(width), cnt = new Int32Array(width);
    // `/api/library/script` 返回的是 [at,pos] 数组；AI live 是 {at,pos} 对象。
    // 两种都收（旧实现只认对象，导致脚本热力曲线/mini 条一直空白）。
    var pt = function (a) {
      return Array.isArray(a) ? { at: a[0], pos: a[1] } : a;
    };
    for (var b = 1; b < actions.length; b++) {
      var prev = pt(actions[b-1]), cur = pt(actions[b]);
      if (prev.at >= durMs) break;
      var dt = (cur.at - prev.at) / 1000;
      if (dt <= 0) continue;
      if (dt * 1000 > HEAT_GAP_MS) continue;
      var speed = Math.abs(cur.pos - prev.pos) / dt;      // %/s
      var c0 = Math.min(width-1, Math.max(0, Math.floor(prev.at / durMs * width)));
      var c1 = Math.min(width-1, Math.max(c0, Math.floor(cur.at / durMs * width)));
      for (var c = c0; c <= c1; c++) { acc[c] += speed; cnt[c]++; }
    }
    var raw = new Float64Array(width), samples = [];
    for (var c2 = 0; c2 < width; c2++) {
      if (!cnt[c2]) continue;
      raw[c2] = acc[c2] / cnt[c2];
      samples.push(raw[c2]);
    }
    if (!samples.length) return null;
    samples.sort(function(x,y){ return x-y; });
    var p95 = samples[Math.min(samples.length-1, Math.floor((samples.length-1)*0.95))];
    if (!(p95 > 0)) return null;
    var cols = new Array(width);
    for (var c3 = 0; c3 < width; c3++) {
      var l = raw[Math.max(0,c3-1)], r = raw[Math.min(width-1,c3+1)];
      var intensity = Math.max(0, Math.min(1, (((l + 2*raw[c3] + r) / 4) / p95)));
      cols[c3] = intensity <= 0.02 ? null : { rgb: heatColorRGB(raw[c3]), h: intensity };
    }
    return cols;
  }
  var HEAT = { key: "", cols: null, w: 0, miniKey: "", miniCols: null };
  function heatActions() {
    return AIV.liveActions || (vlHeatScript && vlHeatScript.actions) || null;
  }
  /* 视频画面实际渲染矩形（移植手机端 Screens.kt computeVideoRectPx）：
     · fit(contain)：等比缩放，pillarbox/letterbox 黑边不覆盖
     · zoom/stretch：画面铺满容器 → 全宽贴底
     aspect<=0 或尺寸未测得 → null（不显示 mini 条） */
  function computeVideoRect(containerW, containerH) {
    var v = vlVid();
    var aspect = (v && v.videoWidth && v.videoHeight) ? (v.videoWidth / v.videoHeight) : 0;
    if (!(aspect > 0) || containerW <= 0 || containerH <= 0) return null;
    var mode = getComputedStyle(v).objectFit || "contain";
    if (mode === "cover" || mode === "fill") {
      return { widthPx: containerW, bottomPx: 0, leftPx: 0 };
    }
    var cAspect = containerW / containerH;
    var w, h;
    if (cAspect > aspect) {            // 容器更宽 → 画面高度顶满，左右黑边
      h = containerH; w = containerH * aspect;
    } else {                            // 容器更高 → 画面宽度顶满，上下黑边
      w = containerW; h = containerW / aspect;
    }
    var sidePx = Math.max(0, (containerW - w) / 2);
    var bottomPx = Math.max(0, (containerH - h) / 2);
    return { widthPx: w, bottomPx: bottomPx, leftPx: sidePx };
  }

  /* mini 热力条（手机端 MiniHeatStrip）：20px 高，8px 能量条贴底 + 位置白线，
     底部衬底渐变让亮画面上也看得清。只在 .idle（控件隐藏）时可见。
     宽度/位置对齐"视频画面实际渲染区域"（与手机端 computeVideoRectPx 同逻辑）。 */
  function drawMiniHeat() {
    var cv = $("#vlMiniHeat"); if (!cv) return;
    var v = vlVid(); if (!v) return;
    var dpr = window.devicePixelRatio || 1;
    var wrap = $("#vlVWrap");
    if (!wrap) return;
    /* 画面矩形：宽度取画面实际宽度，底部留 letterbox 黑边高度 */
    var rect = computeVideoRect(wrap.clientWidth, wrap.clientHeight);
    if (!rect) return;                       /* 无宽高比/无尺寸 → 手机端同样不显示 */
    cv.style.width = Math.round(rect.widthPx) + "px";
    cv.style.left = Math.round(rect.leftPx) + "px";
    cv.style.right = "auto";
    cv.style.bottom = Math.round(rect.bottomPx) + "px";
    var cw = Math.round(rect.widthPx), ch = 20;
    var w = Math.round(cw * dpr), h = Math.round(ch * dpr);
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    var g = cv.getContext("2d");
    g.clearRect(0, 0, w, h);
    var dur = v.duration || 0;
    var acts = heatActions();
    if (!acts || acts.length < 2) { g.clearRect(0, 0, w, h); return; }  /* 手机端: script==null 不显示 */
    if (window.MINI_HEATMAP === false) { g.clearRect(0, 0, w, h); return; }  /* 设置里关掉 */
    var key = "mini|" + (vlMed.path || "") + "|" + Math.round(dur) + "|" + w + "|" +
              (acts ? acts.length : 0) + "|" + (AIV.liveActions ? "ai" : "script");
    if (HEAT.miniKey !== key) {
      HEAT.miniKey = key;
      HEAT.miniCols = buildHeatMask(acts, dur, w);
    }
    var grad = g.createLinearGradient(0, 0, 0, h);
    grad.addColorStop(0, "rgba(0,0,0,0)");
    grad.addColorStop(1, "rgba(0,0,0,0.35)");
    g.fillStyle = grad;
    g.fillRect(0, 0, w, h);
    if (!HEAT.miniCols) return;
    var barH = Math.round(8 * dpr);
    var top = Math.max(0, h - barH);
    var frac = dur > 0 ? Math.max(0, Math.min(1, (v.currentTime || 0) / dur)) : 0;
    var headX = w * frac;
    for (var x = 0; x < w; x++) {
      var col = HEAT.miniCols[x];
      if (!col) continue;
      var bh = Math.max(1, 1 + col.h * (barH - 1));
      g.globalAlpha = (x < headX) ? 1.0 : 0.45;
      g.fillStyle = "rgb(" + Math.round(col.rgb[0]) + "," +
                    Math.round(col.rgb[1]) + "," + Math.round(col.rgb[2]) + ")";
      g.fillRect(x, h - bh, 1, bh);
    }
    g.globalAlpha = 1;
    if (headX > 0) {
      g.fillStyle = "rgba(255,255,255,0.9)";
      g.fillRect(Math.round(headX) - 1, top, Math.max(1, Math.round(1.5 * dpr)), barH);
    }
  }
  function drawHeatCurve() {
    var cv = $("#vlHeatCurve"); if (!cv) return;
    var v = vlVid(); if (!v) return;
    var w = Math.max(4, Math.round(cv.clientWidth * (window.devicePixelRatio || 1)));
    var h = Math.max(4, Math.round(cv.clientHeight * (window.devicePixelRatio || 1)));
    var dur = v.duration || 0;
    var acts = (SYNC && SYNC.live && SYNC.liveActions) ? SYNC.liveActions : heatActions();
    var key = (vlMed.path || "") + "|" + Math.round(dur) + "|" + w + "|" +
              (acts ? acts.length : 0);
    if (HEAT.key !== key) {
      HEAT.key = key; HEAT.w = w;
      HEAT.cols = buildHeatMask(acts, dur, w);
    }
    if (cv.width !== w) cv.width = w;
    if (cv.height !== h) cv.height = h;
    var g = cv.getContext("2d");
    g.clearRect(0, 0, w, h);
    if (!HEAT.cols) return;
    var frac = dur > 0 ? Math.max(0, Math.min(1, (v.currentTime || 0) / dur)) : 0;
    var headX = w * frac;
    for (var x = 0; x < w; x++) {
      var col = HEAT.cols[x];
      if (!col) continue;
      var bh = Math.max(2, 2 + col.h * (h - 2));
      g.globalAlpha = (x < headX) ? 1.0 : 0.45;
      g.fillStyle = "rgb(" + Math.round(col.rgb[0]) + "," +
                    Math.round(col.rgb[1]) + "," + Math.round(col.rgb[2]) + ")";
      g.fillRect(x, h - bh, 1, bh);
    }
    g.globalAlpha = 1;
  }

  /* ---------- AI 实时识别（边播边生成脚本 → 走现有 sync/tick 下发） ---------- */
  var AIV = { on: false, state: "idle", coverage: 0, n: 0, pct: 0, err: "" };

  /* 预热遮罩：暂停画面 + 明确进度提示（转码/加载/首批推理） */
  function aivShowWarm(on) {
    var box = $("#vlAiWarm");
    if (!box) return;
    box.hidden = !on;
    if (!on) return;
    var pct = AIV.pct || 0;
    var bar = $("#vlAiWarmBar");
    if (bar) bar.style.width = Math.max(2, Math.min(100, pct)) + "%";
    var sub = $("#vlAiWarmSub");
    if (sub) {
      /* 阶段优先用引擎自己报的 note（"加载模型 / 转码 480p30 / 镜头切分 / 识别中"）——
         只按百分比猜阶段会在转码期（coverage 恒 0）一直显示"正在识别画面 0%"，
         用户分不清是在干活还是卡死。 */
      var note = String(AIV.note || "");
      if (AIV.state === "idle" || AIV.state === "warming") {
        if (note) {
          sub.textContent = note + (pct >= 1 ? "（已识别 " + pct.toFixed(1) + "%）" : "");
        } else if (AIV.state === "idle") {
          sub.textContent = "正在启动识别服务…";
        } else {
          sub.textContent = "正在准备（加载模型 / 转码视频）…";
        }
      } else {
        sub.textContent = "正在识别画面（已识别 " +
          (AIV.coverage / 1000).toFixed(0) + "s / " + pct.toFixed(1) + "%）…";
      }
    }
    var hint = $("#vlAiWarmHint");
    if (hint) {
      hint.textContent = "预热完成后自动继续播放；也可点「取消」直接播放（无 AI 动作）";
    }
  }

  function aivRender() {
    var box = $("#vlAiVision"), txt = $("#vlAvText"), meta = $("#vlAvMeta"),
        dot = $("#vlAvDot"), btn = $("#vlAiScript2");
    // 实际按钮 id 是 vlAiScript2（旧 vlAiScript 已从页面移除）：激活态点亮
    if (btn) btn.classList.toggle("on", AIV.on);
    if (!box) return;
    box.hidden = !AIV.on;
    if (!AIV.on) return;
    var label = {
      idle: "AI 识别：未开始", warming: "AI 识别：预热中",
      running: "AI 识别：跟随中", done: "AI 识别：全片完成", error: "AI 识别：出错"
    }[AIV.state] || ("AI 识别：" + AIV.state);
    if (txt) txt.textContent = label;
    if (meta) {
      meta.textContent = AIV.state === "warming"
        ? ("已识别 " + (AIV.coverage / 1000).toFixed(0) + "s（" + AIV.pct + "%）")
        : (AIV.n + " 个动作 · 已识别 " + (AIV.coverage / 1000).toFixed(0) + "s");
    }
    if (dot) {
      dot.className = "vl-av-dot " +
        (AIV.state === "error" ? "err" : AIV.state === "running" ? "ok" : "warm");
    }
  }

  /* 设置同步：mini 热力条开关（对齐手机端 miniHeatmapEnabled，默认开） */
  function miniHeatSync() {
    api("/api/state").then(function (r) {
      if (!r || !r.ok) return;
      var s = r.settings || {};
      window.MINI_HEATMAP = (s.mini_heatmap !== false);
    }).catch(function () {});
  }
  miniHeatSync();

  function aivPoll() {
    if (!AIV.on) return;
    api("/api/vision/state").then(function (r) {
      if (!r || !r.ok) return;
      AIV.state = r.engine_state || "idle";
      AIV.coverage = r.coverage_ms || 0;
      AIV.n = r.n_actions || 0;
      AIV.pct = r.warmup_pct || 0;
      AIV.err = r.error || "";
      AIV.note = r.note || "";
      aivRender();
      /* 预热够了：撤遮罩 + 自动继续播放（用户不必盯着）。
         必须覆盖到暂停位置/当前播放位置：播放一旦跑在识别覆盖前面，同步引擎
         会因"时间超出动作表"一帧都不发（现场症状就是设备完全不动）。 */
      var vNow = vlVid();
      var headMs = vNow ? (vNow.currentTime || 0) * 1000 : (AIV.startAt || 0) * 1000;
      var needMs = Math.max(3000, headMs + 1500);
      if ((AIV.state === "running" || AIV.state === "done") && AIV.coverage >= needMs) {
        aivShowWarm(false);
        var v = vlVid();
        if (v && v.paused) {
          /* 不在第一次失败后锁死：只要还暂停就每 1.5s 重试一次。
             WebView2 可能拒绝无手势的 play()，静音重试成功后恢复原静音状态。 */
          var nowTry = Date.now();
          if (!AIV._playTryAt || nowTry - AIV._playTryAt > 1500) {
            AIV._playTryAt = nowTry;
            var pTry = v.play();
            if (pTry && pTry.catch) pTry.catch(function () {
              var wasMuted = v.muted;
              v.muted = true;
              var pMute = v.play();
              if (pMute && pMute.then) {
                pMute.then(function () { v.muted = wasMuted; })
                     .catch(function () { v.muted = wasMuted; });
              }
            });
          }
        }
      } else if (AIV.state === "running" && vNow && !vNow.paused && headMs > AIV.coverage - 1500) {
        /* 播放追上识别覆盖前沿：暂停缓冲，等识别追上来再继续。
           识别速度目前慢于实时，不拦播放的话设备会停在"动作表之外"，看起来完全没动作。 */
        try { vNow.pause(); } catch (e) {}
        aivShowWarm(true);
        var wsub0 = $("#vlAiWarmSub");
        if (wsub0) {
          wsub0.textContent = "识别进度缓冲中…（已识别 " + Math.floor(AIV.coverage / 1000) +
            "s，播放到 " + Math.floor(headMs / 1000) + "s）";
        }
      } else if (AIV.state === "warming" || AIV.state === "idle") {
        /* 预热期宿主探测偶发超时会回报 idle：此时视频正在播就**不要**重新翻开遮罩，
           否则用户看到的是"遮罩闪回、一直转圈"（AI 预热完不继续的现场观感之一）。 */
        var vw = vlVid();
        if (!(AIV.state === "idle" && vw && !vw.paused)) aivShowWarm(true);
        else {
          /* 守卫盲区兜底：idle 且视频在播时不翻遮罩，但也不能全静默——
             在 AI 状态行挂一句纯文字提示（下一拍状态变化时 aivRender 会覆盖回来） */
          var vt = $("#vlAvText");
          if (vt) vt.textContent = "AI 未就绪";
        }
        var wb = $("#vlAiWarmBar");
        if (wb) wb.style.width = Math.max(2, Math.min(100, AIV.pct || 0)) + "%";
      } else if ((AIV.state === "error" || (r.error && !r.running)) && !AIV._failShown) {
        /* 预热失败：**必须**收起遮罩并恢复播放，否则视频被暂停、遮罩一直转圈，
           用户只能强关程序。错误信息要留在界面上（toast），不要静默吞掉。 */
        aivShowWarm(false);
        var ve = vlVid();
        if (ve && ve.paused) ve.play().catch(function () {});
        /* err 为空时用 note 顶上（错误态 note 带"原生流式出错：…"的真实原因），
           两头都空才落到兜底文案 */
        var msg = AIV.err || AIV.note || "识别服务启动失败";
        AIV._failShown = true;
        aivShowWarm(false);
        aivRender();
        toast("AI 识别失败", msg, "err");
        AIV.on = false;
      }
    });
  }

  function aivStart() {
    if (!vlMed.path) { toast("AI 识别", "先打开一个视频", "warn"); return; }
    AIV.on = true; AIV._autoplayed = false; AIV._failShown = false;
    /* 预热期间暂停视频：识别是流式的，播放位置一旦跑在识别覆盖前面，
       同步引擎会判定"当前时间超出动作表"而一帧都不发（设备不动的根因）。
       记下暂停位置，等识别覆盖到这里再自动继续播放。 */
    var v0 = vlVid();
    AIV.startAt = v0 ? (v0.currentTime || 0) : 0;
    if (v0 && !v0.paused) { try { v0.pause(); } catch (e) {} }
    aivShowWarm(true);
    aivRender();
    api("/api/vision/open", "POST", { video: vlMed.path }).then(function (r) {
      if (!r || !r.ok) {
        AIV.on = false; aivShowWarm(false); aivRender();
        toast("AI 识别启动失败", (r && r.error) || "未知错误", "err");
        return;
      }
      toast("AI 识别", "正在预热（转码 + 加载模型，约 30-60 秒）", "info");
      /* AI 动作要靠前端 20ms tick 才能下发。这里**无条件**接管 SYNC：
         无脚本视频的 syncStart 失败响应会把 SYNC.on 置 false，连 AI tick 一起关掉。 */
      SYNC.on = true; SYNC.ai = true;
    });
  }

  function aivStop(silent) {
    AIV.on = false; AIV.liveActions = null; AIV.note = ""; aivShowWarm(false);
    if (SYNC.ai) { SYNC.on = false; SYNC.ai = false; }
    aivRender(); drawHeatCurve();
    /* 返回 Promise：换片要先等旧引擎关掉，再 open 新视频，
       否则 close 后到会把刚打开的新引擎关掉。 */
    return api("/api/vision/close", "POST", {}).then(function () {
      if (!silent) toast("AI 识别", "已停止", "info");
    }).catch(function () {});
  }
  setInterval(aivPoll, 1000);

  function openVideo(path, name, list, idx) {
    reportProgress(true);                                // 切换前先保存上一个的进度
    /* 换片必须先停 AI：旧的流式引擎还绑定着上一部视频，宿主 live 会继续
       按上一部的动作表下发（现场症状就是切集后设备还在演上一部）。 */
    var wasAi = AIV.on;
    var aiClosed = wasAi ? aivStop(true) : Promise.resolve();
    vlMed.path = path || "";
    vlMed.name = name || String(path || "").split(/[\\/]/).pop();
    vlMed.failed = false;
    if (list && list.length) { vlMed.list = list; vlMed.idx = idx || 0; }
    showPlayView(vlMed.path);
    $("#vlPlayName").textContent = vlMed.name;
    $("#vlPlayPath").textContent = vlMed.path;
    var _vt = $("#vlVTitle"); if (_vt) _vt.textContent = vlMed.name || "—";
    $("#vlVOff").hidden = true;
    var v = vlVid();
    v.style.display = "";
    vlStreamUrl(vlMed.path, function (url) {
      if (!url) { vlVideoFail("这个文件不在 DLNA 共享目录里，无法内置播放。"); return; }
      v.src = url;
      v.load();
      var pr = v.play();
      if (pr && pr.catch) pr.catch(function () { /* 自动播放被拦或解码失败，等 error 事件 */ });
      /* 旧引擎完全关闭后再为新视频重新预热 */
      if (wasAi) aiClosed.then(function () { aivStart(); });
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
    drawHeatCurve();
    if ($("#vlVWrap") && $("#vlVWrap").classList.contains("idle")) drawMiniHeat();
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
          if (ev === "pause" || ev === "ended") reportProgress(true); // 落盘观看进度
          if (ev === "play") api("/api/quick", "POST", { kind: "resume", on: true }).then(pollDev);
          if (ev === "play" || ev === "pause") vlTouch();
        });
      });
    v.addEventListener("error", function () {
      vlVideoFail("内置播放器打不开这个文件：多数是编码不受支持（HEVC / 10bit / AV1），或文件已经不在了。");
    });
    v.addEventListener("click", function () { if (v.paused) v.play(); else v.pause(); vlTouch(); });
    v.addEventListener("play", function () { syncStart(vlMed.path); });
    v.addEventListener("timeupdate", function () { syncTick(v.currentTime); reportProgress(false); });
    v.addEventListener("ended", function () { syncStop(); });
    /* ⚠ 历史教训（2026-10-18 用户实测）：引用已移除的 DOM 元素再 addEventListener
       会在模块顶层抛 null 异常，整个 IIFE 中断、bind() 永不执行。
       旧控件（vlPlay/vlPrev/vlNext/vlMute）已从 index.html 移除且无替代，
       绑定代码本次直接删除——保留"任何新绑定前先判空"的纪律即可。 */
    /* 视频全屏 ≠ 应用窗口最大化：
       - 视频全屏：隐藏应用所有外壳，让 <video> 铺满屏幕，并调用宿主原生全屏盖住任务栏；
       - 右上角最大化：只切换窗口最大化，仍保留标题栏/侧栏。
       WebView2 的 requestFullscreen 只改 DOM、不改变原生窗口，所以用 body class
       控制布局，再由宿主把窗口切成全屏。 */
    function videoFullscreenOn() {
      return document.body.classList.contains("vl-video-fs");
    }
    function setVideoFullscreen(on) {
      on = !!on;
      document.body.classList.toggle("vl-video-fs", on);
      /* 非全屏布局（fitVideoAspect）给 #vlVWrap 写过内联 width/height，
         内联样式优先级高于 CSS 的 inset:0——不清掉的话全屏时视频区
         锁死在进入前那一块（上半屏有画面、下半空白）。退出全屏后
         fitVideoAspect 会重算回写，所以这里只清不加。 */
      if (on) {
        var ow = $("#vlVWrap");
        if (ow) {
          ow.style.width = ""; ow.style.height = "";
          ow.style.flex = ""; ow.style.margin = "";
        }
      }
      var fullBtn = $("#vlFull");
      var u = fullBtn && fullBtn.querySelector("use");
      if (u) u.setAttribute("href", on ? "#i-minimize-2" : "#i-maximize");
      if (fullBtn) fullBtn.title = on ? "退出全屏" : "全屏";
      var api = window.pywebview && window.pywebview.api;
      if (api && api.win_fullscreen) {
        try { api.win_fullscreen(on); } catch (e) {}
      }
      if (window.requestAnimationFrame) requestAnimationFrame(function () { layoutVl(); });
    }
    $("#vlFull").addEventListener("click", function () {
      setVideoFullscreen(!videoFullscreenOn());
    });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && videoFullscreenOn()) {
        e.preventDefault();
        setVideoFullscreen(false);
      }
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
    /* ---- 顶部功能区（手机端 PlayerTopBar 同构） ---- */
    var backFn = function () {
      if (videoFullscreenOn()) setVideoFullscreen(false);
      try { v.pause(); } catch (e) {}
      v.removeAttribute("src"); v.load();
      vlMed.path = ""; vlMed.failed = false;
      if (AIV.on) aivStop();
      vlCurPath = "";
      $("#vlPlayView").style.display = "none";
      $("#vlBrowse").style.display = "";
      vlHeatScript = null; vlHeatKey = ""; drawHeat();
      browse(vlBrowsePath || "");
      if (window.requestAnimationFrame) requestAnimationFrame(function () { layoutVl(); });
    };
    if ($("#vlVBack2")) $("#vlVBack2").addEventListener("click", backFn);
    var titleEl = $("#vlVTitle");
    if (titleEl) titleEl.textContent = vlMed.name || "—";
    /* 字幕 pill：与旧 AI 字幕按钮同语义 */
    if ($("#vlCcPill")) $("#vlCcPill").addEventListener("click", function () {
      toast("字幕", "外部字幕请用「AI 字幕」；本页暂不支持内嵌字幕轨", "info");
    });
    /* AI 识别 / 字幕 pill 只由上面的 document 委托统一处理：
       这里再绑一次会让同一次点击执行两遍（先 open 再 close、面板开关两次等于没开）。 */
    if ($("#vlAiWarmCancel")) $("#vlAiWarmCancel").addEventListener("click", function () {
      /* 取消 = 放弃 AI 识别（关掉引擎，释放显存）并立刻恢复播放。
         只隐藏遮罩不关引擎的话，后台仍在转码/推理，用户会看到显存被占、
         风扇狂转却"什么都没发生"。 */
      aivStop(true);
      var vv = vlVid(); if (vv && vv.paused) vv.play().catch(function () {});
      toast("AI 识别", "已取消预热，继续播放（无 AI 动作）", "info");
    });
    if ($("#vlMore")) {
      var moreMenu = $("#vlMoreMenu");
      $("#vlMore").addEventListener("click", function (e) {
        e.stopPropagation();
        if (!moreMenu) return;
        moreMenu.hidden = !moreMenu.hidden;
        if (!moreMenu.hidden) {
          var mute = $("#vlMoreMute");
          if (mute) mute.textContent = v.muted ? "取消静音" : "静音";
        }
      });
      if (moreMenu) {
        moreMenu.addEventListener("click", function (e) {
          var btn = e.target.closest("[data-more]");
          if (!btn) return;
          var act = btn.getAttribute("data-more");
          if (act === "mute") { v.muted = !v.muted; vlSyncVlUi(); }
          else if (act === "speed125") { v.playbackRate = 1.25; toast("倍速", "1.25x", "info"); }
          else if (act === "speed100") { v.playbackRate = 1.0; toast("倍速", "1.0x", "info"); }
          else if (act === "pip") { try { v.requestPictureInPicture && v.requestPictureInPicture(); } catch (err) {} }
          moreMenu.hidden = true;
        });
        document.addEventListener("click", function (e) {
          if (!moreMenu.hidden && !e.target.closest("#vlMoreMenu") && !e.target.closest("#vlMore")) {
            moreMenu.hidden = true;
          }
        });
      }
    }
    /* 手动选择 .funscript：原生文件框 → 后端按任意脚本路径加载（热力图 + 设备同步） */
    if ($("#vlPickScript")) $("#vlPickScript").addEventListener("click", function () {
      if (!(vlMed.path && !vlMed.failed)) {
        toast("选择脚本", "请先打开一个视频，再选择配套脚本", "err");
        return;
      }
      var load = function (path) {
        api("/api/library/script", "POST", { video: vlMed.path, script: path }).then(function (r) {
          if (!r || !r.ok) { toast("脚本加载失败", (r && r.error) || "未知错误", "err"); return; }
          vlHeatScript = { ok: true, actions: r.actions || [] };
          vlHeatKey = vlMed.path;
          drawHeat(); drawHeatCurve();
          toast("脚本已加载", (r.script || path), "ok");
        });
      };
      var api2 = window.pywebview && window.pywebview.api;
      if (api2 && api2.pick_file) {
        api2.pick_file("open", "", ["funscript"]).then(function (r) {
          if (r && r.ok && r.path) load(r.path);
        });
        return;
      }
      var inp = document.createElement("input");
      inp.type = "file"; inp.accept = ".funscript";
      inp.onchange = function () { if (inp.files && inp.files[0]) load(inp.files[0].path || inp.files[0].name); };
      inp.click();
    });
    var _vb = $("#vlVBack"); if (_vb) _vb.addEventListener("click", function () {
      /* 回到媒体库（暂停并释放流） */
      /* 先退视频全屏再动 DOM。 */
      if (videoFullscreenOn()) setVideoFullscreen(false);
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
  }

  /* --- 大框框：播放模式（内置播放器） --- */
  var vlCurPath = "";
  function showPlayView(path) {
    vlCurPath = path || vlCurPath;
    $("#vlBrowse").style.display = "none";
    $("#vlPlayView").style.display = "";
    $("#vlPlayPath").textContent = vlCurPath;
    if (window.requestAnimationFrame) requestAnimationFrame(function () { layoutVl(); });
  }

  /* 播放状态轮询已删除：外挂 mpv 弃用后由内置播放器状态直接驱动（vlMed/vlVid） */

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
    /* 内置播放器在放 → 直接 seek 视频 */
    if (vlMed.path && !vlMed.failed) {
      var v = vlVid();
      var d = (v && isFinite(v.duration)) ? v.duration : 0;
      var end = Math.max(d, scriptEnd);
      if (end > 0) vlSeekTo(frac * end);
      return;
    }
    /* 没有内置播放时什么都不做 */
  });
  function loadHeat() {
    if (!document.querySelector("#page-library.active")) return;
    /* 内置播放器在放：时间轴跟视频走 */
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
    /* 没有内置播放（外挂 mpv 已删除）→ 无热力图 */
    vlHeatScript = null; vlHeatKey = ""; drawHeat();
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
      /* R125 修复（全项目审查高危②）：取整按值域自适应——整数刻度滑轨
         （音量 0..100、速度 1..500 等，span>1）照旧 Math.round；**浮点值域滑轨
         （进度条 vlVSeek 0..1，span 恰为 1）必须保留小数**，否则进度被二值化成
         0/1：拖到 [0,50%) 回开头、[50%,100%] 跳片尾（实测复现）。 */
      var integral = (max - min) > 1;
      v = (integral ? Math.round : function (x) { return Math.round(x * 1000) / 1000; })(
        Math.min(max, Math.max(min, v)));
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
        el.__dragging = true;   // 轮询回填判据：拖动中不能被服务器旧值回写（回弹抖动）
        var move = function (ev) {
          set(side, xToVal(ev.clientX, el.getBoundingClientRect()), true);
        };
        var up = function () {
          th.removeEventListener("pointermove", move);
          th.removeEventListener("pointerup", up);
          el.__dragging = false;
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
        linkBoxHtml("vl_burst_speed_link", bSpeedLink) + '</div>';
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
      /* R125 修复（全项目审查中危）：用响应携带的最新 quick 状态**立即**更新
         本地快照——旧代码只等 2s 轮询，快速二次点击读到旧值会重发同向命令
         （与用户意图相反）。服务端是绝对方向命令（非服务端 toggle），回显有真值。 */
      if (r.quick) {
        DEV.quick = r.quick;
        renderDev(DEV);
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
    /* 手机端：点卡片就是选中（不做"再点取消"），选中本身不开始播放。
       失败必须提示：未知预设服务端回 ok:false（HTTP 200），只判 then 会静默 */
    api("/api/preset", "POST", { action: "select", id: id }).then(function (r) {
      if (!r || r.ok === false) {
        toast("预设选择失败", (r && r.error) || "未知预设", "err");
      }
      pollDev();
    });
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
    // on + connected 双类：redesign 的扁平连接态（.connected）要盖过旧红底（.on），
    // 特异性相同靠后加载的 redesign 胜出，缺一不可
    if (DEV.connected) { btn.classList.add("on", "connected"); lb.textContent = "断开设备"; btn.title = (DEV.name || "设备") + " 已连接（点击断开）"; }
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
    // 拖动中不回填：2s 轮询用服务器旧值回写会把 thumb 弹回去（落盘值不受影响，
    // 但拖动过程视觉打架——与 busyEditing 同一原则）
    if (psEl && !psEl.__dragging) { psEl.style.setProperty("--a", 0); psEl.style.setProperty("--b", (psVal - 1) / 499); psEl.setAttribute("data-val", psVal); }
    if ($("#vlPresetSpeedVal") && !(psEl && psEl.__dragging)) $("#vlPresetSpeedVal").textContent = psVal;
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
    /* 设备高级设置（对齐手机端 DeviceSettingsPage）：
       · 未连接 → 整组禁用变灰 + 底部提示「连接设备后可用」；
       · 回显只认**设备真值**——伪装=当前连接通道档案（连上 Vorze 通道→开）、
         狂暴=设备 D0 的 motorPower（100=开 / 75=关）；**不再回退本机设置**——
         旧逻辑在设备信息缺失时显示本地值，"手机平台上已关的狂暴"在 PC 会显示成开。 */
    ["setA10", "setReversed", "setOcMode"].forEach(function (id) {
      var el = $("#" + id); if (el) el.disabled = !on;
    });
    if ($("#advHint")) $("#advHint").hidden = on;
    /* R125 修复（全项目审查中危）：伪装开关回显改认**设备的 mode_override 真值**
       （DEV.a10_mode，用户设置后宿主持久保存的稳定状态）；连接后从未改过（null）
       才按当前通道档案播种（Vorze 通道→开）。旧写法只认 DEV.toy——非 vorze 设备
       上用户勾选（写入 a10_mode=1 成功）会被 2s 轮询按 toy 恒刷回 false（开关
       不可用且反复重启设备），与"以用户意图为唯一真源"的手机端语义相反。 */
    var _a10 = DEV.a10_mode;
    $("#setA10").checked = (_a10 === null || _a10 === undefined)
      ? String(DEV.toy || "").toLowerCase() === "vorze"
      : Number(_a10) === 1;
    $("#setReversed").checked = !!DEV.reversed;
    var _mp = Number((DEV.info || {}).motor_power) || 0;
    $("#setOcMode").checked = on && _mp >= 100;   // 75=默认档（关）；100=开
    /* 手动控制位置：未连接时禁用（对齐手机端 enabled = deviceConnected）——
       滑轨用 data-disabled（CSS 已挡 pointer-events），按钮用 disabled */
    var _posEl = $("#setManualPos");
    if (_posEl) { if (on) _posEl.removeAttribute("data-disabled"); else _posEl.setAttribute("data-disabled", "1"); }
    var _moveEl = $("#setManualMove");
    if (_moveEl) _moveEl.disabled = !on;
    if ($("#setSyncState")) {
      $("#setSyncState").textContent = sy.active ? ("脚本同步中 · " + (sy.script || "")) : "脚本同步待命";
      $("#setSyncSub").textContent = sy.active
        ? ("已发 " + (sy.sent || 0) + " 帧 · 空闲跳过 " + (sy.skipped || 0) + " · 延迟 " + Math.round(sy.delay_ms || 0) + "ms")
        : (on ? "播放带脚本的视频会自动开始" : "未连接设备");
    }
    /* 延迟数值：手机端显示秒（"%.2fs"），不是毫秒 */
    if ($("#setDelayVal")) $("#setDelayVal").textContent = ((Number(sy.delay_ms) || 0) / 1000).toFixed(2) + "s";
  }
  function saveDev(patch) {
    /* 本地先认值：开关回显读 SET_DEV（未连接时的回退源），不认值会在保存后
       被 2s 轮询拨回旧值（点了没反应/弹回的观感） */
    Object.keys(patch || {}).forEach(function (k) {
      var v = patch[k];
      if (v && typeof v === "object" && !Array.isArray(v)) {
        SET_DEV[k] = Object.assign({}, SET_DEV[k], v);
      } else {
        SET_DEV[k] = v;
      }
    });
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
        /* 不做夹紧：手机端 setSyncDelay(delayMs ± delta) 无任何上下限（点击即 ±） */
        var next = cur + Number(btn.getAttribute("data-delay"));
        api("/api/sync/delay", "POST", { ms: next }).then(function () {
          pollDev();
          toast("延迟 " + (next / 1000).toFixed(2) + "s", "负值提前，正值滞后", "ok");
        });
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
        window.pywebview.api.pick_folder(cur, false).then(function (r) {
          var p = (r && (r.path || r.folder)) || (typeof r === "string" ? r : "");
          if (p) {
            $("#setScriptFolder").value = p;
            api("/api/settings", "POST", { script_folder: p }).then(function (r) {
              if (!r || r.ok === false) toast("保存失败", (r && r.error) || "", "err");
            });
          }
          /* 对齐其余 pick 的口径：服务端报错要提示（用户主动取消不打扰） */
          else if (r && r.error) toast("选择目录失败", r.error, "err");
        }).catch(function (e) { toast("选择目录失败", String(e), "err"); });
      }
    });
    $("#setScriptFolder") && $("#setScriptFolder").addEventListener("change", function () {
      api("/api/settings", "POST", { script_folder: this.value }).then(function (r) {
        // 设置文件损坏时服务端拒绝全部写盘（guard_read_error 回 400）：
        // 无条件报"已保存"会让用户以为存上了
        if (r && r.ok) toast("已保存", "", "ok");
        else toast("保存失败", (r && r.error) || "", "err");
      });
    });
  }
  bindSetDev();   /* 曾经只 load 不 bind：设置页全部监听器（高级开关/空闲秒数/手动移动/
                     延迟按钮…）一个都没绑上——"点了没有任何反应"的根因（2026-10-01 起断链） */
  loadSetDev();

  /* ---- 脚本同步：播放进度 → 设备 ---- */
  /* 「播放视频时同步驱动设备」开关已删（手机端没有此项，视频在播即同步） */
  var SYNC = { on: false, last: 0, inflight: false, path: "", estopped: false, scriptless: "", starting: false, want: false };
  var syncStartGen = 0;
  function syncStart(path) {
    SYNC.want = true;               // "这个视频该联动"——设备不在也先记下，连上后看护会补起
    /* 双保险：api() 已有 15s 超时，但若上一视频恰留下一拍僵尸在途请求，inflight
       闸门仍会压住新视频的开头几拍——换视频即视为旧拍作废，直接放闸。 */
    SYNC.inflight = false;
    /* AI 模式由 vision/open 启动 live 引擎，不需要脚本同步；
       这里再发 /api/sync/start，无脚本视频的失败响应会把 SYNC.on 置 false。 */
    if (AIV.on) { SYNC.on = true; SYNC.ai = true; return; }
    if (!DEV.connected || !path || SYNC.starting) return;
    SYNC.starting = true;
    var gen = ++syncStartGen;       // 代际丢弃：连开两个视频，慢的旧 start 响应不得回退状态
    api("/api/sync/start", "POST", { path: path }).then(function (r) {
      SYNC.starting = false;
      if (gen !== syncStartGen) return;
      /* 在途响应晚到：这期间用户开了 AI，按 AI 模式处理，别覆盖 SYNC.on */
      if (AIV.on) { SYNC.on = true; SYNC.ai = true; return; }
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
  /* 一拍 = 一次 /api/sync/tick。真正限制节拍的是**宿主回包**，不是定时器：
     定时器每 20ms 调用，节流按"上一次**回包返回**的时刻"算，所以有效周期 =
     20ms 与宿主往返（含 /api/vision/actions 的 3s 上限）之和。
     旧写法在**发出**时就推进 SYNC.last，往返超过 20ms 时后续每一拍都被拦下，
     等于串行；预热期引擎被占、宿主 /state 探测慢时，实际下发被拖到 ~1.5s 一拍
     （25 倍丢拍）——AI 模式下设备"几乎不动"的现场症状之一。 */
  function syncTick(t, rate) {
    if (!SYNC.on) return;
    var v = vlVid();
    if (v && v.paused) return;    // 暂停中（含拖进度条）不追帧：设备不被 seek 目标拖着跑
    if (SYNC.inflight) return;    // 上一拍还没回来：不并发堆叠请求
    if (Date.now() - SYNC.last < 20) return;   // 20ms：严格对齐手机端主循环 LOOP_INTERVAL_MS=20
    SYNC.inflight = true;
    api("/api/sync/tick", "POST", { t: t, rate: rate || 1 }).then(function (r) {
      /* 「跳过无动作部分」（对齐手机端 maybeSkipIdle）：宿主判定脚本静止段超过阈值时
         让**视频快进**到下一动作点；离目标太远才跳，防 seek 环 */
      if (r && r.ok && r.seek_to != null && isFinite(r.seek_to)) {
        var vv = vlVid();
        if (vv && !vv.paused && !vv.seeking && Math.abs((vv.currentTime || 0) - r.seek_to) > 1.5) vlSeekTo(r.seek_to);
      }
    }).catch(function () {
      /* 失败也要放开闸门，否则一次网络抖动会让 tick 永久停摆 */
    }).then(function () {
      SYNC.inflight = false;
      SYNC.last = Date.now();     // 节拍从**回包之后**重新计时（见上方说明）
    });
  }
  /* 高频 tick 驱动：timeupdate 只有 ~250ms 且抖动，做不了"段边界发帧"的检测
     （慢段顿挫、快段迟发）——定时器每 20ms 读一次播放器时间，与手机端主循环
     同节拍；timeupdate 保留作后台保底（隐藏时定时器可能被节流）。 */
  setInterval(function () {
    var v = vlVid();
    if (v && !v.paused && !v.seeking) syncTick(v.currentTime, v.playbackRate || 1);
  }, 20);

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
      /* 高级设备设置的回显（伪装设备/反转方向/狂暴模式）：
         伪装开关按"设备当前档案"推（toy），狂暴按设备回读的 motor_power——
         与手机端 AppViewModel 的"设备侧回读"同源 */
      DEV.reversed = !!d.reversed;
      DEV.a10_mode = d.a10_mode;
      DEV.toy = d.toy || "";
      vlPlayingPreset = !!(DEV.preset && DEV.preset.playing);   // 预设卡"▶"回显（此前从未赋值）
      renderDev();
      /* 同步看护：视频在播、设备在线、该联动(want)但会话没起来 → 自动补起。
         覆盖"先播视频加载脚本、后连设备"（syncStart 当时因未连接早退）与
         断线重连后会话丢失。不碰的两种情况：急停（会话已按 I6 停止且不自动恢复，
         用户重按播放/重开联动才复活）；用户自己停过联动或视频无脚本（want=false）。 */
      var v = vlVid();
      /* R125 修复（全项目审查中危）：看护条件补 `!vlPlayingPreset`——arbiter 接管
         停脚本后 SYNC.want 不清（只有急停/失败/显式停清它），预设开播 ≤2s 被
         看护 syncStart 顶掉（实测复现）。同构的缓动场景已有专门回归
         （test_slow_survives_script_watchdog），预设场景漏防。 */
      if (SYNC.want && DEV.connected && !DEV.quick.stop && !SYNC.estopped
          && !vlPlayingPreset
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
    toast("正在扫描设备", "扫到支持的设备即连（最长 10 秒）", "ok");
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
  /* 视频播放区固定 16:9：在可用舞台内等比缩放并居中；窗口比例由宿主按它反推。 */
  function fitVideoAspect() {
    var pv = $("#vlPlayView"), wrap = $("#vlVWrap");
    if (!pv || !wrap || pv.offsetParent === null) return;
    if (document.fullscreenElement || document.body.classList.contains("vl-video-fs")) return;
    var path = $(".vl-screen-path");
    var availW = pv.clientWidth;
    var availH = pv.clientHeight - (path ? path.offsetHeight + 8 : 0);
    if (availW < 16 || availH < 9) return;
    var h = Math.min(availH, availW * 9 / 16);
    var w = h * 16 / 9;
    wrap.style.width = Math.round(w) + "px";
    wrap.style.height = Math.round(h) + "px";
    wrap.style.flex = "0 0 auto";
    wrap.style.margin = "auto";
  }

  /* 媒体库网格：卡片缩略图用 aspect-ratio 时不会自动贡献固有行高，
     169 张卡会把 auto 行压成几像素并叠在一起。按列宽 + 选定比例算固定行高。 */
  function layoutLibGrid() {
    var bg = $("#vlBrowseBody");
    if (!bg) return;
    var cols = (getComputedStyle(bg).gridTemplateColumns || "")
      .split(" ")
      .filter(Boolean).length;
    if (cols < 1) return;
    var gap = 10;
    var cw = (bg.clientWidth - gap * (cols - 1)) / cols;
    var asp = (getComputedStyle(document.documentElement)
      .getPropertyValue("--lib-aspect") || "16 / 9").trim();
    var p = asp.split("/");
    var ratio = p.length === 2 ? (parseFloat(p[0]) / parseFloat(p[1])) : (16 / 9);
    if (!(ratio > 0)) ratio = 16 / 9;
    var title = document.querySelector(".lib-card .lib-title");
    var titleH = title ? title.offsetHeight : 0;
    var rowH = Math.max(80, Math.round(cw / ratio + titleH + 2));
    bg.style.gridAutoRows = rowH + "px";
  }

  function layoutVl() {
    fitVideoAspect();
    layoutLibGrid();
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
     Rust 宿主的 win_maximize 已重写并单独验证过，不受旧实现影响。 */
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
    // 坏编码的 hash（孤立 % 等）会让 decodeURIComponent 抛 URIError——
    // 这里在 IIFE 顶层被同步调用，抛错会中断整个初始化链（boot/bind 全部失效，
    // 与 2026-10-18 的事故同型）
    var h = "";
    try {
      h = decodeURIComponent(location.hash || "");
    } catch (e) {
      h = location.hash || "";
    }
    if (h.indexOf("#library") !== 0) return;
    if (h.indexOf("#library/browse=") === 0) {
      showPage("library");
      browse(h.slice("#library/browse=".length));
    } else if (h.indexOf("#library/play=") === 0) {
      showPage("library");
      var vp = h.slice("#library/play=".length);
      /* 深链直接进内置播放器；列表带当前这一部（元素结构与浏览列表一致）——
         传空列表会让「上一个/下一个」沿用上一次会话的旧列表切到不相关视频 */
      var nm = String(vp).split(/[\\/]/).pop();
      openVideo(vp, nm, [{ path: vp, name: nm }], 0);
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
    /* 主题/动效：固定浅色（<head> 内联脚本已强制 data-theme=light），
       这里只对齐动效。绝不在 boot 里硬套 dark——那就是
       "启动先深色再跳浅色"的元凶。 */
    setTheme();
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
