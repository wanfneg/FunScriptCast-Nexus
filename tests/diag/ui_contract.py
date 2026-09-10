import re, pathlib
js = pathlib.Path("ui/app.js").read_text(encoding="utf-8")
html = pathlib.Path("ui/index.html").read_text(encoding="utf-8")
ids_js = sorted(set(re.findall(r'\$\("#([A-Za-z0-9_-]+)"\)', js)))
ids_html = set(re.findall(r'id="([A-Za-z0-9_-]+)"', html))
print(f"app.js 引用 {len(ids_js)} 个 id")
print("  " + " ".join(ids_js))
missing = [i for i in ids_js if i not in ids_html]
print(f"\nHTML 中缺失（会导致运行时 null 崩溃）: {missing or '无'}")
unused = sorted(ids_html - set(ids_js))
print(f"\nHTML 有但 JS 未引用（{len(unused)}）: {' '.join(unused)}")
print("\n--- 页面 section ---")
for m in re.findall(r'<section class="page[^"]*" id="([^"]+)"', html):
    print("  " + m)
print("\n--- 用到的图标 ---")
print("  " + " ".join(sorted(set(re.findall(r'href="#i-([a-z0-9-]+)"', html)))))
