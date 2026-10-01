
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync("E:\\Development\\FunScriptCast-Nexus\\ui\\app.js", 'utf8');
try { new vm.Script(src, {filename:'app.js'}); console.log('vm 解析通过'); }
catch (e) { console.log('vm 报错:', e.message); const m = /app\.js:(\d+)/.exec(e.stack||''); if (m) { const n=+m[1]; const L=src.split(/\r?\n/); for (let i=Math.max(0,n-4); i<Math.min(L.length,n+3); i++) console.log((i+1)+': '+L[i]); } }
