import asyncio, sys
sys.path.insert(0, r'E:\Development\FunScriptCast-Nexus')
from vendor.device import channel as CH
from vendor.device.protocols import DeviceInfo
c = CH.DeviceChannel.__new__(CH.DeviceChannel)
c.state = CH.ChannelState(); c.state.connected = True; c._ready = True
calls = []
async def fake_write(payload): calls.append(bytes(payload)); return True
c._write = fake_write
async def main():
    async def feed():
        await asyncio.sleep(1.3)
        c.state.info = DeviceInfo(hardware_version=172, software_version=945, max_speed=500, motor_power=100).as_dict()
    asyncio.ensure_future(feed())
    await c._read_info_with_retry()
    print('D0 发送次数 =', sum(1 for x in calls if x == b'D0'), '（设备慢 → 应 >1）')
    print('读到:', c.state.info.get('hardware'), '/', c.state.info.get('software'), '/', c.state.info.get('motor_power'))
asyncio.run(main())