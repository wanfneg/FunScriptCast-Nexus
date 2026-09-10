import re, pathlib
EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U0000FE0F\U00002190-\U000021FF\U00002300-\U000023FF"
    "\U000025A0-\U000025FF\U00002000-\U0000206F]")
targets = list(pathlib.Path("ui").glob("*"))
targets += [pathlib.Path("host_server.py"), pathlib.Path("vendor/subtitle/server_app.py")]
for f in targets:
    if not f.is_file() or f.suffix not in (".js", ".html", ".css", ".py", ""):
        continue
    try:
        txt = f.read_text(encoding="utf-8")
    except Exception:
        continue
    for i, line in enumerate(txt.splitlines(), 1):
        for m in EMOJI.finditer(line):
            ch = m.group()
            print(f"{f.name}:{i}: U+{ord(ch):04X} {ch!r}  |  {line.strip()[:80]}")
