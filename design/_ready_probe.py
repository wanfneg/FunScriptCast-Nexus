import asyncio, sys, threading, time
sys.path.insert(0, r'E:\Development\FunScriptCast-Nexus')
from vendor.device import channel as CH
from vendor.device.protocols import cmd_limit
c = CH.DeviceChannel.__new__(CH.DeviceChannel)
c.state = CH.ChannelState(); c.range_lo, c.range_hi, c.max_speed = 20.0, 80.0, 500; c.reversed = False
c._ready = False; c._toy = None; c._client = None
print('ready=False 时 _write 应 False →', c.state.connected)
print('施加的临时限位字节 (min=20,max=80,speed=500):', list(cmd_limit(20, 80, 500)), '= [0x42,20,80,1,244] 手机端格式')
print('remap 用 min/max（不是 0/100）: remap(50)=', c._remap(50), ' 速度缩放 500→', c._scale_speed(500))