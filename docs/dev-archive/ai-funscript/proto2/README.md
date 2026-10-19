# proto2 — 视频联动 + AI 脚本 端到端原型

在 proto1（BLE 直驱验证通过）之上加两块：

1. **真实媒体时钟**：mpv（winget `shinchiro.mpv`）JSON IPC，时钟语义 = SyncEngine
   （本地 monotonic 推进 + `time-pos` 上报锚定：偏差 >0.5s 吸附，否则 10% 渐进校正）
2. **AI 生成脚本**：GoblinScript v0.6.0 CUDA 版（V-JEPA 2.1 ViT-B，`_third_party/goblinscript/`），
   22.75 分钟 1080p50 视频 13m25s 生成（**1.7x 实时**），4361 动作点，置信度 0.727

## 运行

```bash
# 端到端（视频窗口 + AI 脚本 + VorzePiston，启动后按空格播放，x=急停 q=退出）
run_ai_vorze.bat

# 或命令行（--script 换 ai_scripts\K1drsp.funscript / E:\testvideo\K1drsp.funscript 对比 AI 与手工）
python main_video.py --video "E:\testvideo\K1drsp.mp4" --script ai_scripts\K1drsp.funscript --device vorze --paused
python main_video.py --video <视频> --device sim --start 300 --no-video --duration 12   # 无头验证

# AI 重新生成（--out 独立目录，不会覆盖手工脚本）
_third_party\goblinscript\goblinscript.exe "E:\testvideo\K1drsp.mp4" --out ai_scripts
```

## 结构

- `media_clock.py` — SyncEngine 时钟语义移植（锚定/吸附/渐进校正）
- `mpv_player.py` — mpv JSON IPC 客户端（命名管道）
- `main_video.py` — 20ms 主循环 + 事件驱动发送（复用 proto1 的 funscript/device）
- 脚本默认自动发现：视频同目录同名 `.funscript`（`--script` 可覆盖）

## 踩坑记录（重要）

1. **mpv IPC 必须单条双工连接**：命令应答只回发到发出命令的那条连接，双连接收不到应答；
2. **Windows 同步句柄同一时刻只允许一个 I/O 操作**：后台读线程阻塞在 `read()` 时，
   同句柄 `write()` 永久死锁 —— 因此用单线程同步请求-应答（poll 仅 5Hz，阻塞毫秒级）；
3. Python 3.10 `FileIO` 没有 `read1`（读线程曾因此秒崩）；
4. bat 文件必须纯 ASCII（cmd 按 GBK 解析 UTF-8 中文注释会整段错乱）；
5. `--out` 生成到独立目录，GoblinScript 默认不覆盖已有 .funscript（防误伤手工脚本）。

## 下一步（生产化）

- 位置源抽象层：`PositionSource` 接口统一三路 —— funscript 文件 / AI track 流（GoblinScript
  `--serve` 或 GTG 栈原生集成）/ Nexus SSE 远程位置流
- seek 到未生成区间的段缓存调度（见调研结论：缓存未命中 = 定向补段）
- 设备层并排 Intiface 后端（750+ 生态设备）
