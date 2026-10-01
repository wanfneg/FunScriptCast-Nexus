"""设备写入仲裁（单一写者不变量 I1，设计见 review/设备写入所有权设计.md）。

用户口径：「最终发送到设备上的动作要干净，不能是两种模式的动作同时发送给设备」。
BLE 上只有一条写帧流，任一时刻至多一个"运动会话"该处于 active；会话转移矩阵
（新会话开始时必须停掉谁）以前散落在两处 `preset.stop()` 里、其余格子全空 ——
现在全部收口到这里，宿主四个入口（/api/sync/start、/api/preset、/api/quick）只调仲裁。

急停（I6，用户裁定"四个全受"）：stop_all() 把四个会话**全部停止本身**，再由路由
把 allow_move 置 false；解除急停只复位闸门，**不重启任何会话**（有意严于手机端
的"按拍拦帧、解除即续"——急停解除后动作自动恢复属于安全隐患）。
"""
from __future__ import annotations


class DeviceArbiter:
    def __init__(self, channel, quick, preset, sync) -> None:
        self.ch = channel
        self.quick = quick
        self.preset = preset
        self.sync = sync

    # ---- 内部：停掉除 keep 外的所有会话 ----
    def _stop_all_except(self, keep: str) -> None:
        if keep != "sync":
            self.sync.stop()
        if keep != "preset":
            self.preset.stop()
        if keep != "orgasm":
            self.quick.stop_orgasm()
        if keep != "slow":
            self.quick.stop_slow()

    # ---- 会话启动（矩阵的"新会话"列）----
    def start_script(self, path: str) -> dict:
        res = self.sync.start(path)          # 先校验脚本能加载，失败就不动别人
        if res.get("ok"):
            self._stop_all_except("sync")    # 清预设 + 停快捷动作；不停视频（PC 集成页口径）
        return res

    def start_preset(self) -> dict:
        self._stop_all_except("preset")      # = 三件事里的"清脚本 + 停快捷动作"
        self.quick.discard_resume()          # 预设在播，视频再播放不许把爆发/缓动拉起来抢设备
        return self.preset.start()

    def toggle_preset(self) -> dict:
        return self.preset.stop() if self.preset.playing else self.start_preset()

    def start_orgasm(self) -> dict:
        self._stop_all_except("orgasm")
        return self.quick.start_orgasm()

    def start_slow(self) -> dict:
        self._stop_all_except("slow")
        return self.quick.start_slow()

    # ---- 急停 / 断开 / 退出共用 ----
    def stop_all(self) -> None:
        self._stop_all_except("")
