import sys; sys.path.insert(0, r'E:\Development\FunScriptCast-Nexus')
from vendor.device.channel import DeviceChannel
c = DeviceChannel.__new__(DeviceChannel)
c.range_lo, c.range_hi, c.max_speed, c.reversed = 20.0, 80.0, 500, False
print('range 20-80, 输入 50% → remap', c._remap(50), '(手机: round(50*60/100)+20 = 50)')
print('  速度 200 → 缩放', c._scale_speed(200), '(手机: round(200*60/100) = 120)')
c.reversed = True
print('  反转后 remap(50) =', c._remap(50), '(手机: 100-50 = 50)')
print('  forceMoveToInverted(50) =', c._invert(50), '(手机: 100-50 = 50)')
c.reversed = False; c.range_lo, c.range_hi = 0.0, 100.0
print('range 0-100: remap(30)=', c._remap(30), ' speed 500 →', c._scale_speed(500))