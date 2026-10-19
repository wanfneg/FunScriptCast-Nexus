
import asyncio, sys, time, threading
sys.path.insert(0, r'E:\Development\FunScriptCast-Nexus')
from pathlib import Path
from vendor.device.preset_player import PresetPlayer

class FakeCh:
    max_speed = 500
    def __init__(self): self.sent = []
    async def move_to(self, pos, speed=None, force=False, raw=False):
        self.sent.append((round(float(pos),1), speed, time.monotonic()))
        return True

loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
def run(pid, secs):
    ch = FakeCh(); pp = PresetPlayer(ch, loop, Path(r'E:\Development\FunScriptCast-Nexus/ui/presets.json'))
    pp.select(pid); pp.start(); time.sleep(secs); pp.stop(); time.sleep(0.3)
    return pp, ch
pp, ch = run('normal', 4.0)
print('【normal】4 秒下发帧数:', len(ch.sent))
if ch.sent:
    print('  间隔(秒):', [round(ch.sent[i+1][2]-ch.sent[i][2],2) for i in range(min(4, len(ch.sent)-1))])
    print('  位置:', [s[0] for s in ch.sent[:6]], ' 速度:', ch.sent[0][1])
print('  stop 后 playing =', pp.state()['playing'])
pp2, ch2 = run('zip', 4.0)
print('【zipper 类】4 秒帧数:', len(ch2.sent), ' 位置:', [s[0] for s in ch2.sent[:6]])
# RANDOM：开启立即跳转
pp3 = PresetPlayer(FakeCh(), loop, Path(r'E:\Development\FunScriptCast-Nexus/ui/presets.json'))
pp3.select('normal'); pp3.start(); time.sleep(0.5)
before = pp3.state()['selected']; pp3.toggle_random(); after = pp3.state()['selected']
print('【RANDOM】开启前:', before, '→ 开启后立即变成:', after, '｜ random =', pp3.state()['random'])
pp3.toggle_random(); print('  再点关闭 random =', pp3.state()['random'], '｜ 仍在播:', pp3.state()['playing'], '｜ 预设保持:', pp3.state()['selected'])
pp3.stop()
# 停止后不再有新帧
ch4 = FakeCh(); pp4 = PresetPlayer(ch4, loop, Path(r'E:\Development\FunScriptCast-Nexus/ui/presets.json'))
pp4.select('normal'); pp4.start(); time.sleep(1.5); pp4.stop(); time.sleep(0.2)
n1 = len(ch4.sent); time.sleep(1.5); n2 = len(ch4.sent)
print('【静止检查】停止后 1.5 秒新增帧:', n2-n1, '(应为 0)')
loop.call_soon_threadsafe(loop.stop)
