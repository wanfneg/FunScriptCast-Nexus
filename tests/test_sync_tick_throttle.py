# -*- coding: utf-8 -*-
"""前端 syncTick 节流的回归测试（ui-legacy/app.js）。

原缺陷：节拍在**发出**时推进 SYNC.last，宿主往返一旦超过 20ms，定时器后续每一拍
都被 `now - SYNC.last < 20` 拦下 ⇒ 实际变成"串行 + 一拍一次"，预热期宿主慢时
掉到 ~1.5s 一拍。修好后节拍从**回包之后**重新计时，且用 in-flight 闸门防并发。

这里用 Node 直接驱动 app.js 里的真实实现（抽取函数源码执行），不依赖浏览器。

运行：python -m unittest tests.test_sync_tick_throttle -v
"""
from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
APP_JS = APP_DIR / "ui-legacy" / "app.js"


def _node() -> str | None:
    return shutil.which("node") or shutil.which("node.exe")


def _extract_sync_tick(src: str) -> str:
    start = src.index("  function syncTick(t, rate) {")
    # 花括号配平到函数结尾
    i = src.index("{", start)
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError("syncTick 花括号未配平")


@unittest.skipIf(_node() is None, "未安装 node，跳过前端节流测试")
class TestSyncTickThrottle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = APP_JS.read_text(encoding="utf-8")
        cls.fn = _extract_sync_tick(src)
        # 前置断言：修复必须体现为"从回包之后计时"
        assert "SYNC.inflight" in cls.fn, "syncTick 缺少 in-flight 闸门"
        assert "}).then(function () {\n      SYNC.inflight = false;" in cls.fn, \
            "syncTick 必须在回包之后才推进节拍"

    def _run(self, js_body: str) -> dict:
        harness = f"""
const SYNC = {{ on: false, last: 0, inflight: false }};
const vlVid = () => ({{ paused: false, seeking: false, currentTime: 0 }});
const vlSeekTo = () => {{}};
let calls = 0;
let now = 0;
Date.now = () => now;
let pending = [];
const api = (url) => {{
  calls++;
  return new Promise((res) => pending.push(res));
}};
{self.fn}
const flush = async () => {{ const p = pending; pending = []; p.forEach(f => f({{ ok: true }})); await Promise.resolve(); await Promise.resolve(); }};
(async () => {{
SYNC.on = true;
{js_body}
}})();
"""
        p = subprocess.run([_node(), "-e", harness], capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise AssertionError(f"node 执行失败：{p.stderr}")
        return json.loads(p.stdout.strip().splitlines()[-1])

    def test_no_concurrent_requests(self):
        """未回包前不允许再发；回包后节拍重新开始。"""
        out = self._run("""
for (let i = 0; i < 5; i++) { now += 20; syncTick(1.0, 1); }
await flush();
const beforeLate = calls;
for (let i = 0; i < 5; i++) { now += 20; syncTick(1.0, 1); }
console.log(JSON.stringify({ calls: calls, beforeLate }));
""")
        self.assertEqual(out["beforeLate"], 1, "同一时刻只允许一拍在途")
        self.assertEqual(out["calls"], 2, "回包后应恢复发拍")

    def test_slow_roundtrip_does_not_throttle_forever(self):
        """宿主缓慢（1.5s 往返）：修好后每拍都能跟上，而不是被永久拦下。"""
        out = self._run("""
let sent = 0;
for (let i = 0; i < 100; i++) {
  now += 20; syncTick(1.0, 1);
  if (pending.length) { sent++; now += 1500; await flush(); }
}
console.log(JSON.stringify({ calls: calls, sent }));
""")
        self.assertGreater(out["calls"], 1, "慢往返下也必须继续发拍")

    def test_failed_request_releases_gate(self):
        """回包失败不能把闸门锁死（否则一次抖动让 tick 永久停摆）。"""
        out = self._run("""
now += 20; syncTick(1.0, 1);
const midFlight = SYNC.inflight ? 'locked' : 'open';
pending[0]({ ok: false });
pending = [];
for (let i = 0; i < 8; i++) await Promise.resolve();
const afterFail = SYNC.inflight ? 'locked' : 'open';
now += 1000; syncTick(1.0, 1);
console.log(JSON.stringify({ calls: calls, midFlight, afterFail }));
""")
        self.assertEqual(out["midFlight"], "locked", "在途期间闸门必须是关的")
        self.assertEqual(out["afterFail"], "open", "回包失败后闸门必须放开")
        self.assertEqual(out["calls"], 2, "放开后应能继续发下一拍")


if __name__ == "__main__":
    unittest.main(verbosity=2)
