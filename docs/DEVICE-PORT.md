# 设备层 Rust 移植（阶段 1）

> 2026-10-19 ｜ 分支 `refactor/foundation`
> 源：Python `vendor/device/`（1,317 行） → 目标：Rust `rust/crates/nexus-device/`

---

## 1. 状态

| 项 | 结果 |
|---|---|
| 语义移植 | **完成**（协议 / 通道 / 脚本同步 / 快捷动作 / 预设 / 仲裁） |
| 单元测试 | **17 个**（协议编解码、闸门、重映射、分片写、通知解析） |
| 对拍测试 | **34 个**（逐条对应 Python `tests/test_device_arbiter.py`） |
| 默认特性 | 通过（不依赖蓝牙栈） |
| `--features ble` | 通过（btleplug 0.13） |
| 真实硬件验证 | **未做**（开发机无设备）—— 见 §5 清单 |

---

## 2. 模块对照

| Python | Rust | 行数（Py → Rs） | 说明 |
|---|---|---:|---|
| `protocols.py` | `protocols.rs` | 143 → 300 | GATT 档案、命令构造、通知解析 |
| `channel.py` | `channel.rs` | 411 → 480 | 状态、闸门、重映射/缩放/反转、分片写 |
| `channel.py`（bleak 部分） | `ble.rs`（`--features ble`） | — → 270 | 扫描/连接/握手/通知泵 |
| `sync_engine.py` | `sync_engine.rs` | 199 → 330 | 段边界一帧、live 灌入、seek |
| `quick_moves.py` | `quick_moves.rs` | 255 → 390 | 爆发/缓动 + 空闲看护 |
| `preset_player.py` | `preset_player.rs` | 208 → 360 | 段播放、BOOST、随机 |
| `arbiter.py` | `arbiter.rs` | 90 → 150 | 单一写者不变量 |

**没有行为改动**：所有常量、舍入口径、帧语义、闸门位置逐条保持。

---

## 3. 关键设计差异（为什么这样移植）

### 3.1 传输抽象取代 bleak 单例循环

Python 把 bleak 放在一条专用线程的事件循环里，宿主用 `submit(coro)` 跨线程投递。
Rust 侧改成 `Transport` trait（`write` + `is_connected`）：

- 生产：`ble.rs` 的 `BleTransport`（btleplug）
- 测试：`RecordTransport`（记录帧，零硬件）

好处：闸门/重映射/分片这些**语义**可以在没有蓝牙的机器上焊死；Python 那边这些逻辑
藏在 `FakeChannel` 后面，实际测的是替身而不是真实现。

### 3.2 观测探针（`set_move_tap`）

Python 的 `FakeChannel.move_to` 直接记录 `(percent, speed)`，绕过了真实的
重映射与速度压缩。Rust 侧保留真实通道，另加一个**只读探针** `set_move_tap`：

- 每帧成功落下后回调 `(原始位置, 压缩前速度)`
- 与 Python `ch.calls` 语义逐值一致，断言可以直接照抄

### 3.3 超时即取消

Python 用 `run_coroutine_threadsafe` + `future.cancel()`。
Rust 用 `tokio::time::timeout`：超时丢弃 future 即取消，`Drop` 保证清理。
测试 `f6_timeout_cancels_pending_work` 用 `Drop` 守卫把这个保证焊死。

---

## 4. 对拍测试清单

| Rust 测试 | 对应 Python | 覆盖点 |
|---|---|---|
| `to_segments_matches_phone` | 同名 | 波形式→段式无损转换 |
| `all_24_real_presets_have_play_segments` | 同名 | 真实 24 预设、22 波形 |
| `keyframes_only_preset_yields` | 同名 | 空段会锁死循环 |
| `a1_single_writer_matrix` | 同名 | 会话转移矩阵全格 |
| `a2_estop_stops_all_and_no_auto_resume` | 同名 | 急停全停、解除不自动恢复 |
| `f5_channel_gate_and_defense` | 同名 | 闸门拒写（含 raw） |
| `a8_estop_toggle_path` | 同名 | 急停开关往返 |
| `f6_cleanup_resets_session_state` | 同名 | 断开复位 |
| `f6_timeout_cancels_pending_work` | `test_f6_submit_timeout_cancels_coroutine` | 超时取消 |
| `f16_no_frames_outside_action_range` | 同名 | 首末点外不发 |
| `slow_segment_speed_is_zero_not_max` | 同名 | 慢段 speed=0 |
| `f17_skip_idle_defaults_and_seek_semantics` | 同名 | 跳过无动作 |
| `delay_positive_lags_like_phone` | 同名 | 延迟方向 |
| `segment_boundary_frames` | 同名 | 段边界一帧 |
| `dedup_within_segment` | 同名 | 同段去重 |
| `rate_scales_speed` | 同名 | 倍速 |
| `reset_resends_current_segment` | 同名 | reset 重发 |
| `f14_slow_interval_follows_speed` | 同名 | 缓动间隔 |
| `slow_starts_only_after_full_idle_countdown` | 同名 | 空闲倒计时 |
| `external_move_restarts_slow_countdown` | 同名 | 外部动作重置 |
| `orgasm_params_live_and_links` | 同名 | 关联实时解析 |
| `orgasm_cadence_speed_consistent` | 同名 | 速度与间隔同源 |
| `slow_own_frames_do_not_reset_idle` | 同名 | 自触发免疫 |
| `script_defers_while_orgasm_runs` | 同名 | 爆发期间脚本让路 |
| `slow_survives_script_watchdog` | 同名 | 缓动不被看护顶掉 |
| `kotlin_round_boundaries_parity` | 同名 | 舍入口径 |
| `channel_remap_scale_use_kotlin_round` | 同名 | span=50 上取 |
| `raw_move_not_clamped_through_world` | 同名 | raw 不夹紧 |
| `live_feed_* / estop_blocks_live_frames` | （Python 侧 `test_vision_live_sync.py`） | AI 实时模式 |

**Python 侧未移植的两项**：`test_f24_clamp_video_link` 与
`test_a10_import_does_not_kill_following_settings` —— 二者测的是 `host_server` 的设置
应用逻辑，属于阶段 2（宿主层）范围。

---

## 5. 首次上机验证清单（**必做**）

`ble.rs` 未在真实硬件上跑过。第一次接设备时按顺序核对：

1. `cargo run --features ble --example scan`（待补）能否列出设备、`supported` 是否正确
2. 连接后 `/api/device/state` 的 `info` 是否有硬件/固件版本（= D0 握手成功）
3. `limit_min` / `limit_speed` 是否随通知更新（= 订阅链路通）
4. `moves` 是否随下发自增（= 写帧成功）
5. 断开再连：`allow_move` 必须回到 `true`（= 会话边界复位）
6. 设备重启（切换狂暴/伪装模式）后能否自动重连

> 若第 2 步失败：多半是 `subscribe` 的 RX 特征选错，检查 `TOYS` 档案与广播名是否一致。

---

## 6. 与宿主的对接（下一步）

阶段 1 的产物是一个**库**，还没有 HTTP 路由。接入方式二选一：

- **进程内**：`nexus-host` 直接 `use nexus_device::build(...)`，替换 `host_server` 的
  `/api/device/*`、`/api/sync/*`、`/api/preset`、`/api/quick` 四组路由
- **子进程**：编译成 `nexus-device.exe` 走本地 HTTP（阶段 2 宿主迁完之前更稳妥）

推荐**先走子进程**：Python 宿主继续用，Rust 设备层通过 HTTP 接手写帧，
这样"先开 AI 后连设备"这类跨层问题可以在两套宿主下并存验证。

---

## 7. 命令

```powershell
cd rust
cargo test -p nexus-device                    # 51 个测试（17 + 34），不需要蓝牙
cargo test -p nexus-device --features ble     # 同上，含 BLE 传输编译
cargo build                                   # workspace 全量
```
