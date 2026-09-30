# -*- coding: utf-8 -*-
"""app.js R110 修复：竖版文件夹卡 + 双点滑轨 + 预设网格全量可滚 + 钢蓝滑轨行。"""
import ast

p = 'ui/app.js'
s = open(p, encoding='utf-8').read()

# ── 1) 浏览：文件夹卡改竖版（图标上、名称下）；视频卡不变 ────────────────
old = '''    (data.dirs || []).forEach(function (d) {
      html += '<div class="vl-folder" data-path="' + encodeURIComponent(d.path) + '">' +
        '<svg class="ic"><use href="#i-folder"/></svg><div class="name">' + d.name + '</div></div>';
    });'''
new = '''    (data.dirs || []).forEach(function (d) {
      html += '<div class="vl-folder" data-path="' + encodeURIComponent(d.path) + '">' +
        '<svg class="ic"><use href="#i-folder"/></svg><div class="name">' + d.name + '</div></div>';
    });'''
# 结构不变（CSS 已改竖版），跳过
s = s  # noqa

# ── 2) stroke 页签：两条独立滑轨 → 一条双点行程滑轨 + 速度滑轨 ────────────
old = '''  var VL_TABS = {
    stroke: { label: "设备行程与速度", rows: [
      ["stroke_min", "行程下限 %", 0, 90], ["stroke_max", "行程上限 %", 10, 100],
      ["speed", "速度", 10, 100]
    ]},
    idle: { label: "待机缓动", rows: [
      ["idle_speed", "缓动速度", 5, 60]
    ], toggle: "idle_enabled", toggleLabel: "待机缓动启用" },
    burst: { label: "一键爆发", rows: [
      ["burst_duration", "爆发时长 (s)", 1, 30], ["burst_speed", "爆发速度", 20, 100]
    ]}
  };
  function vlCfg() { return (S.settings && S.settings.video_link) || {}; }
  function saveVl(patch) {
    var merged = Object.assign({}, vlCfg(), patch);
    api("/api/settings", "POST", { video_link: merged }).then(function (r) {
      if (!r || r.ok === false) toast("保存失败", (r && r.error) || "", "err");
    });
  }
  function renderVlTabCard() {
    var box = $("#vlTabCard");
    if (!box) return;
    var t = VL_TABS[vlTab];
    var cfg = vlCfg();
    var html = '<div class="name" style="margin-bottom:8px">' + t.label + '</div>';
    if (t.toggle) {
      html += '<div class="row flush"><div class="grow"><div class="sub">' + t.toggleLabel + '</div></div>' +
        '<button class="btn ghost" id="vlToggle_' + t.toggle + '">' + (cfg[t.toggle] ? "开" : "关") + '</button></div>';
    }
    (t.rows || []).forEach(function (row) {
      var v = cfg[row[0]];
      if (v == null) v = Math.round((row[2] + row[3]) / 2);
      html += '<label class="field" style="margin-top:6px"><span>' + row[1] + '：<b id="vlv_' + row[0] + '">' + v + '</b></span>' +
        '<input type="range" class="vl-slider" data-k="' + row[0] + '" min="' + row[2] + '" max="' + row[3] + '" value="' + v + '"></label>';
    });
    html += '<div class="sub" style="margin-top:8px">设备通道接入后生效（M2）</div>';
    box.innerHTML = html;
    $$(".vl-slider", box).forEach(function (sl) {
      sl.addEventListener("change", function () {
        var patch = {}; patch[this.getAttribute("data-k")] = Number(this.value);
        saveVl(patch);
      });
      sl.addEventListener("input", function () {
        var el = $("#vlv_" + this.getAttribute("data-k"));
        if (el) el.textContent = this.value;
      });
    });
    if (t.toggle) {
      var tb = $("#vlToggle_" + t.toggle);
      if (tb) tb.addEventListener("click", function () {
        var patch = {}; patch[t.toggle] = !cfg[t.toggle];
        saveVl(patch);
        renderVlTabCard();
      });
    }
  }'''
new = '''  var vlCfgCache = {};
  function vlCfg() { return (S.settings && S.settings.video_link) || {}; }
  function saveVl(patch) {
    vlCfgCache = Object.assign({}, vlCfg(), patch);
    api("/api/settings", "POST", { video_link: vlCfgCache }).then(function (r) {
      if (!r || r.ok === false) toast("保存失败", (r && r.error) || "", "err");
    });
  }
  /* 双点行程滑轨（一条轨道两个把手，手机 SteelRangeSlider 同构） */
  function dualSliderHtml(id, min, max, lo, hi, label, valueText) {
    return '<div class="steel-slider-row"><div class="steel-slider-head">' +
      '<span class="steel-label">' + label + '</span>' +
      '<span class="steel-value" id="' + id + '_val">' + valueText + '</span></div>' +
      '<div class="vl-dual" id="' + id + '" data-min="' + min + '" data-max="' + max + '" data-lo="' + lo + '" data-hi="' + hi + '">' +
      '<div class="track"></div><div class="fill"></div>' +
      '<div class="thumb" data-side="lo"></div><div class="thumb" data-side="hi"></div></div></div>';
  }
  function initDual(id, onChange) {
    var el = $("#" + id);
    if (!el) return;
    var min = Number(el.getAttribute("data-min")), max = Number(el.getAttribute("data-max"));
    var lo = Number(el.getAttribute("data-lo")), hi = Number(el.getAttribute("data-hi"));
    var track = el.querySelector(".track"), fill = el.querySelector(".fill");
    var thumbs = { lo: el.querySelector('[data-side="lo"]'), hi: el.querySelector('[data-side="hi"]') };
    function pct(v) { return (v - min) / (max - min) * 100; }
    function paint() {
      fill.style.left = pct(lo) + "%";
      fill.style.width = pct(hi) - pct(lo) + "%";
      thumbs.lo.style.left = pct(lo) + "%";
      thumbs.hi.style.left = pct(hi) + "%";
    }
    function set(which, v) {
      v = Math.round(Math.min(max, Math.max(min, v)));
      if (which === "lo" && v > hi - 1) v = hi - 1;
      if (which === "hi" && v < lo + 1) v = lo + 1;
      if (which === "lo") lo = v; else hi = v;
      el.setAttribute("data-lo", lo); el.setAttribute("data-hi", hi);
      paint();
      onChange(lo, hi);
    }
    Object.keys(thumbs).forEach(function (side) {
      var th = thumbs[side];
      th.addEventListener("pointerdown", function (e) {
        e.preventDefault();
        th.setPointerCapture(e.pointerId);
        var move = function (ev) {
          var rect = track.getBoundingClientRect();
          set(side, min + (ev.clientX - rect.left) / rect.width * (max - min));
        };
        var up = function () {
          th.removeEventListener("pointermove", move);
          th.removeEventListener("pointerup", up);
          onChange(lo, hi);
        };
        th.addEventListener("pointermove", move);
        th.addEventListener("pointerup", up);
      });
    });
    paint();
  }
  function renderVlTabCard() {
    var box = $("#vlTabCard");
    if (!box) return;
    var cfg = vlCfg();
    var g = function (k, d) { return cfg[k] != null ? cfg[k] : d; };
    var html = "";
    if (vlTab === "stroke") {
      html = '<div class="steel-card-title">设备行程与速度</div>' +
        dualSliderHtml("vl_range", 0, 100, g("range_min", 0), g("range_max", 100),
                       "限制输出范围", g("range_min", 0) + "% - " + g("range_max", 100) + "%") +
        sliderRowHtml("设备速度上限", g("max_speed", 300) + " Units/s", "vl_max_speed", 0, VL_SPEED_MAX, g("max_speed", 300));
    } else if (vlTab === "idle") {
      html = '<div class="steel-card-title">待机缓动</div>' +
        dualSliderHtml("vl_idle_range", 0, 100, g("idle_min", 10), g("idle_max", 60),
                       "运动范围", g("idle_min", 10) + "% - " + g("idle_max", 60) + "%") +
        linkBoxHtml("vl_idle_link", !!cfg.idle_link) +
        sliderRowHtml("运动速度", g("idle_speed", 30) + " Units/s", "vl_idle_speed", 0, VL_SPEED_MAX, g("idle_speed", 30));
    } else {
      html = '<div class="steel-card-title">一键爆发</div>' +
        dualSliderHtml("vl_burst_range", 0, 100, g("burst_min", 30), g("burst_max", 100),
                       "运动范围", g("burst_min", 30) + "% - " + g("burst_max", 100) + "%") +
        linkBoxHtml("vl_burst_link", !!cfg.burst_link) +
        sliderRowHtml("运动速度", g("burst_speed", 100) + " Units/s", "vl_burst_speed", 0, VL_SPEED_MAX, g("burst_speed", 100)) +
        linkBoxHtml("vl_burst_speed_link", !!cfg.burst_speed_link, "关联上限");
    }
    box.innerHTML = html;
    var bind = function (id, key) {
      var el = $("#" + id);
      if (!el) return;
      el.addEventListener("change", function () {
        var patch = {}; patch[key] = Number(this.value);
        saveVl(patch);
      });
    };
    bind("vl_max_speed", "max_speed");
    bind("vl_idle_speed", "idle_speed"); bind("vl_burst_speed", "burst_speed");
    [["vl_idle_link", "idle_link"], ["vl_burst_link", "burst_link"], ["vl_burst_speed_link", "burst_speed_link"]]
      .forEach(function (pair) {
        var el = $("#" + pair[0]);
        if (el) el.addEventListener("change", function () {
          var patch = {}; patch[pair[1]] = this.checked;
          saveVl(patch);
        });
      });
    if (vlTab === "stroke") initDual("vl_range", function (lo, hi) { saveVl({ range_min: lo, range_max: hi }); });
    if (vlTab === "idle") initDual("vl_idle_range", function (lo, hi) { saveVl({ idle_min: lo, idle_max: hi }); });
    if (vlTab === "burst") initDual("vl_burst_range", function (lo, hi) { saveVl({ burst_min: lo, burst_max: hi }); });
  }'''
assert old in s, "tab card"
s = s.replace(old, new)

# ── 3) sliderRowHtml 需要提前定义（renderVlTabCard 引用）──────────────────
old = '''  var vlCfgCache = {};'''
new = '''  function sliderRowHtml(label, valueText, id, min, max, val) {
    return '<div class="steel-slider-row"><div class="steel-slider-head">' +
      '<span class="steel-label">' + label + '</span>' +
      '<span class="steel-value">' + valueText + '</span></div>' +
      '<input type="range" class="steel-range" id="' + id + '" min="' + min + '" max="' + max + '" value="' + val + '"></div>';
  }
  function linkBoxHtml(id, checked, labelText) {
    return '<label class="steel-link"><input type="checkbox" id="' + id + '"' + (checked ? " checked" : "") + '> ' + (labelText || "关联输出") + '</label>';
  }
  var vlCfgCache = {};'''
assert old in s, "sliderRow"
s = s.replace(old, new)

# ── 4) 预设网格：全量 24、可滚、一屏约 3 行 ─────────────────────────────
old = '''    // 固定 4 个、两列、不滚动（用户拍板：始终只显示 4 个预设）
    grid.innerHTML = VL_PRESETS.slice(0, 4).map(function (pr) {'''
new = '''    // 全量 24 个、两列、可滚（与手机 PresetGridCard 一致；一屏约 3 行）
    grid.innerHTML = VL_PRESETS.map(function (pr) {'''
assert old in s
s = s.replace(old, new)

# ── 5) 时间轴：时长标签不被吞（CSS 已修，JS 无需改）────────────────────
open(p, 'w', encoding='utf-8').write(s)
import ast
print("app.js fixes OK")
