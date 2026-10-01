import sys, traceback
sys.path.insert(0, sys.argv[1])
from vendor.device.sync_engine import SyncEngine
class FakeState:
    connected = True
    allow_move = True
class FakeCh:
    state = FakeState()
    max_speed = 500
    def move_to(self, *a, **k): return True
try:
    se = SyncEngine(FakeCh(), None)
    print('resolve_script:', se.resolve_script(sys.argv[2]))
    print('start:', se.start(sys.argv[2]))
except Exception:
    traceback.print_exc()