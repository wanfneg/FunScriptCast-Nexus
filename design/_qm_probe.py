import asyncio, sys, time, threading
sys.path.insert(0, r'E:\Development\FunScriptCast-Nexus')
from vendor.device.quick_moves import QuickMoves

class St:
    allow_move = True
class Ch:
    state = St()
    range_lo, range_hi, max_speed = 0.0, 100.0, 500
    def __init__(self): self.sent = []
    def set_allow_move(self, v): Ch.state.allow_move = bool(v)
    def on_move(self, cb): self._cb = cb
    async def move_to(self, pos, speed=None, force=False, raw=False):
        if not Ch.state.allow_move: return False
        self.sent.append(round(float(pos),1)); return True

loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
ch = Ch(); qm = QuickMoves(ch, loop)
qm.slow.idle_detect_seconds = 1
print('① 缓动：开启后空闲 1 秒开始')
qm.start_slow(); time.sleep(2.5); n1 = len(ch.sent)
print('   2.5 秒帧数 =', n1, '(应 > 0)')
print('② 急停：不杀循环，只是不发帧')
qm.set_stop(True); time.sleep(2.0); n2 = len(ch.sent)
print('   急停 2 秒新增 =', n2 - n1, '(应 0) ｜ is_slow 仍 =', qm.is_slow)
qm.set_stop(False); time.sleep(2.5); n3 = len(ch.sent)
print('③ 放开急停：自动继续新增 =', n3 - n2, '(应 > 0)')
print('④ 互斥：开爆发应关掉缓动')
qm.start_orgasm(); time.sleep(0.3)
print('   is_orgasm =', qm.is_orgasm, '｜ is_slow =', qm.is_slow, '(应为 True/False)')
print('⑤ 播放器暂停/恢复联动')
qm.pause_for_player(); print('   pause 后 orgasm =', qm.is_orgasm)
qm.resume_for_player(); print('   resume 后 orgasm =', qm.is_orgasm, '(应 True)')
qm.stop_orgasm(); qm.stop_slow(); time.sleep(0.4)
a = len(ch.sent); time.sleep(1.5); b = len(ch.sent)
print('⑥ 全停后新增帧 =', b - a, '(应 0)')
loop.call_soon_threadsafe(loop.stop)
