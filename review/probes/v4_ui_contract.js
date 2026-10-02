/* R04 只读探针 2：index.html id ↔ app.js 选择器契约（task-4 verifier 独立编写）。
 *
 * 用法：node review/probes/v4_ui_contract.js
 * 只读：不启动 GUI/服务器，不写文件。
 *
 * 判据：
 *   · 静态 id 引用（$("#x") / getElementById("x")）必须能在 index.html 找到；
 *   · 拼接型引用（$("#sync" + cap + "Badge")）单独统计，不参与"缺失"判定；
 *   · 运行时模板生成型 id（dualSliderHtml/sliderRowHtml/linkBoxHtml 传进去的 "vl_*"）
 *     单独统计，并展开 "_val"。
 */
const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
const html = fs.readFileSync(path.join(ROOT, "ui", "index.html"), "utf8");
const js = fs.readFileSync(path.join(ROOT, "ui", "app.js"), "utf8");

const htmlIds = new Set();
for (const m of html.matchAll(/\bid\s*=\s*"([^"]+)"/g)) htmlIds.add(m[1]);
for (const m of html.matchAll(/\bid\s*=\s*'([^']+)'/g)) htmlIds.add(m[1]);

const jsLines = js.split(/\r?\n/);
const staticRefs = [];   // {id, line, raw}
const concatRefs = [];   // {prefix, line, raw}

const staticRe = /(?:\$\$?\(\s*["']#([A-Za-z][\w-]*)["']\s*\)|getElementById\(\s*["']([A-Za-z][\w-]*)["']\s*\))/g;
const concatRe = /\$\$?\(\s*["']#([A-Za-z][\w-]*)["']\s*\+/g;
jsLines.forEach((ln, i) => {
  let m;
  staticRe.lastIndex = 0;
  while ((m = staticRe.exec(ln)) !== null) staticRefs.push({ id: m[1] || m[2], line: i + 1, raw: ln.trim() });
  concatRe.lastIndex = 0;
  while ((m = concatRe.exec(ln)) !== null) concatRefs.push({ prefix: m[1], line: i + 1, raw: ln.trim() });
});

/* 模板生成：这三个函数把 "vl_*" 当参数传进去，运行时在 innerHTML 里生成 id 与 id+"_val" */
const tplIds = new Set();
for (const m of js.matchAll(/["'](vl_[a-z_]+)["']/g)) {
  tplIds.add(m[1]);
  tplIds.add(m[1] + "_val");
}

const uniq = [...new Set(staticRefs.map(r => r.id))].sort();
const dynamicOk = [], missing = [];
for (const id of uniq) {
  if (htmlIds.has(id)) continue;
  if (tplIds.has(id)) { dynamicOk.push(id); continue; }
  missing.push({ id, sites: staticRefs.filter(r => r.id === id).map(h => `app.js:${h.line}`) });
}

/* 护栏：同一行或前一行出现 if (...) / && / return 早退 */
const GUARD = {
  motionSeg: 'app.js:208 `if ($("#motionSeg"))`；initSeg 首行 `if (!seg || !thumb) return;`',
  themeToggle2: 'app.js:810 `var _t2 = $("#themeToggle2"); if (_t2)`',
  setSlowSpeed: 'app.js:2326 `if ($("#setSlowSpeed"))`',
  setOrgasmSpeed: 'app.js:2327 `if ($("#setOrgasmSpeed"))`',
};

const out = [];
out.push(`index.html id 总数        = ${htmlIds.size}`);
out.push(`app.js 静态选择器引用     = ${staticRefs.length} 处 / ${uniq.length} 个唯一 id`);
out.push(`拼接型引用（不展开）      = ${concatRefs.length} 处 -> ${[...new Set(concatRefs.map(c => c.prefix + "*"))].join(", ")}`);
out.push(`运行时模板生成 id         = ${tplIds.size} 个 -> ${[...tplIds].sort().join(", ")}`);
out.push("");
out.push(`[静态引用但 index.html 无此 id] 共 ${missing.length} 个：`);
for (const m of missing) out.push(`  - ${m.id} @ ${[...new Set(m.sites)].join(", ")}   护栏: ${GUARD[m.id] || "**未见护栏**"}`);
out.push("");
out.push(`[模板生成，不需 HTML] = ${dynamicOk.join(", ") || "（无）"}`);
const expect = ["motionSeg", "themeToggle2", "setSlowSpeed", "setOrgasmSpeed"];
const ok = missing.length === expect.length && expect.every(x => missing.some(m => m.id === x));
out.push("");
out.push(`[断言] 缺失 id 恰好 == {motionSeg, themeToggle2, setSlowSpeed, setOrgasmSpeed}: ${ok ? "PASS" : "FAIL"}`);

console.log(out.join("\n"));
