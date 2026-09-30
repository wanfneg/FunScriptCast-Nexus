# -*- coding: utf-8 -*-
"""R110 验收轮综合修复：主题令牌化 + 竖版文件夹卡 + 双点滑轨 + 预设网格 + 时间轴/按钮修正。"""
import ast, re

# ═══════════ 1) 替换 styles.css 的 R110 追加块（全部改用真实主题令牌）═══════════
p = 'ui/styles.css'
s = open(p, encoding='utf-8').read()
start = s.index('/* ======================== 媒体库（M1） ======================== */')
new_css = '''/* ======================== 媒体库/视频联动（M1+R110，全部走主题令牌） ======================== */
.lib-card {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  overflow: hidden;
  cursor: pointer;
  transition: border-color 0.15s, transform 0.15s;
}
.lib-card:hover { border-color: var(--accent-line); transform: translateY(-2px); }
.lib-thumb {
  position: relative;
  aspect-ratio: 16 / 10;
  background: var(--raise);
  display: flex; align-items: center; justify-content: center;
}
.lib-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.lib-nothumb { color: var(--ink-4); font-size: 40px; }
.lib-dur {
  position: absolute; right: 6px; bottom: 6px;
  background: rgba(0, 0, 0, 0.72); color: #fff;
  font-size: 11px; padding: 2px 6px; border-radius: 4px;
}
.lib-badge {
  position: absolute; top: 6px; left: 6px;
  background: rgba(0, 0, 0, 0.72); color: var(--accent);
  font-size: 11px; padding: 2px 6px; border-radius: 4px;
}
.lib-prog { position: absolute; left: 0; right: 0; bottom: 0; height: 4px; background: var(--line); }
.lib-prog i { display: block; height: 100%; background: var(--accent); }
.lib-title {
  padding: 8px 10px 2px; font-size: 13px; font-weight: 600; color: var(--ink);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.lib-sub {
  padding: 0 10px 10px; font-size: 11px; color: var(--ink-2);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* 视频联动（R110 草图布局） */
.vl-stage { margin-bottom: 14px; }
.vl-playing {
  display: flex; align-items: center; gap: 12px;
  background: var(--accent-soft); border: 1px solid var(--accent-line);
  border-radius: 8px; padding: 10px 14px; margin-bottom: 12px;
}
.vl-playing .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--ok); box-shadow: 0 0 8px var(--ok); }
.lib-grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 14px; margin-top: 10px;
}
.vl-timeline {
  display: flex; align-items: center; gap: 12px;
  padding: 10px 14px; margin-bottom: 14px;
}
.vl-time { font-family: var(--f-mono); font-size: 13px; color: var(--ink-2); min-width: 56px; flex-shrink: 0; text-align: center; }
.vl-heat { flex: 1 1 auto; min-width: 0; height: 88px; border-radius: 6px; cursor: crosshair; }
.vl-btnrow { display: flex; align-items: center; gap: 14px; margin-bottom: 14px; }
.vl-tabs { display: flex; gap: 8px; flex: 1; }
.vl-tabs button {
  background: var(--panel); color: var(--ink-2);
  border: 1px solid var(--line); border-radius: var(--r-pill);
  padding: 10px 18px; font-size: 13px; cursor: pointer; flex: 1;
}
.vl-tabs button:hover { color: var(--ink); border-color: var(--line-3); }
.vl-tabs button[aria-selected="true"] {
  color: #fff; border-color: transparent; background: var(--steel, #4A5F8A);
}
.vl-capsule {
  background: var(--raise-2); color: var(--ink);
  border: 1px solid var(--line); border-radius: var(--r-pill);
  padding: 10px 18px; font-size: 13px; font-weight: 500; cursor: pointer; white-space: nowrap;
}
.vl-capsule:hover { border-color: var(--line-3); }
.vl-capsule.filled-blue { background: var(--steel, #4A5F8A); border-color: transparent; color: #fff; }
.vl-capsule.filled-green { background: #2E7D32; border-color: transparent; color: #fff; }
.vl-capsule.filled-red { background: #C0392B; border-color: transparent; color: #fff; }
.vl-lower {
  display: grid; grid-template-columns: 1.15fr auto 1.15fr;
  gap: 14px; align-items: stretch; margin-top: 14px;
}
.vl-tabstack { min-height: 200px; }

/* 钢蓝卡片与滑轨行（手机 SteelCard/SliderRow 同构，走主题令牌） */
.steel-card {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 14px; padding: 12px 16px;
  display: flex; flex-direction: column; gap: 8px;
}
.steel-card-title { font-size: 15px; font-weight: 600; color: var(--ink); }
.vl-phead { font-size: 15px; font-weight: 600; color: var(--ink); }
.steel-slider-row { display: flex; flex-direction: column; gap: 4px; }
.steel-slider-head { display: flex; align-items: center; }
.steel-label { font-size: 12px; font-weight: 500; color: var(--ink); }
.steel-value { margin-left: auto; font-size: 20px; font-weight: 700; color: var(--ink); }
.steel-link { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-2); cursor: pointer; }

/* 双点行程滑轨（一条轨道两个把手） */
.vl-dual { position: relative; height: 26px; }
.vl-dual .track {
  position: absolute; left: 0; right: 0; top: 50%; height: 4px; transform: translateY(-50%);
  background: var(--line-2); border-radius: 2px;
}
.vl-dual .fill {
  position: absolute; top: 50%; height: 4px; transform: translateY(-50%);
  background: var(--steel, #4A5F8A); border-radius: 2px;
}
.vl-dual .thumb {
  position: absolute; top: 50%; width: 16px; height: 16px;
  transform: translate(-50%, -50%);
  background: #fff; border: 2px solid var(--steel, #4A5F8A); border-radius: 50%;
  cursor: grab; z-index: 2;
}
.vl-dual .thumb:active { cursor: grabbing; box-shadow: 0 0 0 5px var(--accent-soft); }

/* 中列快捷动作（严格手机 QuickActionsCard：一行三钮，激活填充变色+文字切换） */
.vl-quickrow { display: flex; gap: 6px; }
.vl-quickrow .steel-capsule { flex: 1; padding: 10px 8px; text-align: center; }
.vl-quickrow .steel-capsule.filled-green { background: #2E7D32; }
.vl-quickrow .steel-capsule.filled-blue { background: var(--steel, #4A5F8A); }
.vl-quickrow .steel-capsule.filled-red { background: #C0392B; }

/* 右预设网格（手机 PresetGridCard：两列、一屏 3 行 = 6 个、可滚） */
.vl-presets {
  background: var(--panel); border: 1px solid var(--line);
  border-radius: 14px; padding: 12px 16px;
  display: flex; flex-direction: column; gap: 8px;
}
.vl-phead { font-size: 15px; font-weight: 600; color: var(--ink); }
.vl-pgrid {
  display: grid; grid-template-columns: 1fr 1fr; gap: 8px;
  flex: 1; min-height: 0; overflow-y: auto;
  grid-auto-rows: 96px;
}
.vl-pcard {
  background: var(--card-embed, var(--panel-2));
  border-radius: 10px; padding: 8px 10px; cursor: pointer; overflow: hidden;
  display: flex; flex-direction: column;
}
.vl-pcard.selected { background: var(--accent-soft); }
.vl-phead-row { display: flex; align-items: center; gap: 6px; }
.vl-pname {
  font-size: 12px; font-weight: 500; color: var(--ink);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1;
}
.vl-pname.sel { color: var(--accent); font-weight: 600; }
.vl-ploop { font-size: 9px; color: var(--ink-3); white-space: nowrap; }
.vl-wave { width: 100%; height: calc(100% - 18px); display: block; }

@media (max-width: 1100px) {
  .vl-lower { grid-template-columns: 1fr; }
}
'''
s = s[:start] + new_css
open(p, 'w', encoding='utf-8').write(s)
print("styles rewritten (R110 block replaced)")

# ═══════════ 2) index.html：给需要的新元素补类 ═══════════
p = 'ui/index.html'
s = open(p, encoding='utf-8').read()
# 时间轴面板补边框（已有 panel card 类 ✓）。检查 vl 按钮行 BOOST/播放/RANDOM 的类（vl-capsule ✓）
# 快捷动作三个按钮已是 steel-capsule ✓。页签已是 vl-tabs ✓。
print("index ok")
