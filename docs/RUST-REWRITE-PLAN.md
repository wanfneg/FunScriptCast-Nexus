# 全项目 Rust 重写方案

> 2026-10-19 立项。目标：用 Rust 重写 FunScriptCast-Nexus 的**整个后端与运行时**，
> 不再依赖 Python；现有 Web UI 资产保留，继续由 Rust 宿主提供同样的 `/api/*`。

## 先定边界

- **保留**：`ui-legacy` 的 HTML/CSS/JS（前端不重写），`vendor/vision/bundles` 的 ONNX 模型，
  Inno Setup 打包链路，用户数据目录兼容（`var/data`）。
- **重写**：宿主 HTTP/静态服务、托盘/窗口、设置、设备 BLE、同步/仲裁、预设/快捷、
  DLNA/VR、字幕（音频采集 + ASR + 术语翻译）、视觉识别（ONNX + 流式样式 + 静止门）。
- **不再有**：PyInstaller、PyWebView、Python 解释器、`run_server.py`、`runtime`/`vision-runtime`。

## 技术栈

| 领域 | Rust 选型 | 风险 |
|---|---|---|
| HTTP/API | axum + tokio + serde | 低 |
| 窗口 | wry + WebView2（或 tauri） | 低 |
| 托盘/单实例 | tray-icon / interprocess | 低 |
| 设备 BLE | btleplug | 中（WinRT 行为需对拍） |
| 视觉 ONNX | `ort` crate + DirectML EP | 中（DirectML feature/版本） |
| 视频 | ffmpeg-next（libav）或先 ffmpeg.exe | 中 |
| 字幕 ASR | 需决策：CTranslate2 FFI（对齐 faster-whisper）或 whisper-rs | **高** |
| DLNA/SSDP | 自写 SSDP + hyper range | 中 |
| 打包 | Rust release + Inno Setup + WebView2 | 低 |

## 工程结构

```
rust/
  Cargo.toml                # workspace
  crates/
    nexus-core/             # 路径/配置/日志/错误
    nexus-host/             # axum + 静态 UI + 托盘/窗口
    nexus-vision/           # ort + DirectML + 编码/解码/样式/静止门
    nexus-device/           # btleplug + SyncEngine/Arbiter
    nexus-subtitle/         # WASAPI + ASR + 术语
    nexus-dlna/             # SSDP/HTTP/VR
  xtask/                    # 构建/打包辅助
```

## 迁移策略

不是“一次推倒”，而是**同端口径逐步替换**：

1. Rust 宿主先实现 `/api/state`、静态 UI、设置/日志，能单独启动；
2. 视觉 crate 用金标对拍（tracks/动作/静止门），替换识别；
3. 设备 crate 对拍 BLE 帧与仲裁行为；
4. 字幕 crate 对拍识别文本/翻译；
5. DLNA/VR；
6. 最后删 Python 目录、改安装包，用户数据原样迁移。

开发期保留 Python 版作为**参照实现**，每个 crate 有金标测试；全部通过才切默认。

## 阶段与验收

| 阶段 | 产出 | 验收 |
|---|---|---|
| 0 | Rust 工具链、workspace、金标采集脚本 | `cargo check` 通过 |
| 1 | Rust 宿主 + 静态 UI + 设置/日志 | 浏览器打开同 UI，`/api/state` 对齐 |
| 2 | 视觉：ORT DirectML 编码器 | latent corr >0.999 |
| 3 | 视觉：轨道/样式/静止门/流式 | 动作数/位置/A-B 对齐 |
| 4 | 设备 BLE + 同步/仲裁 | 真机帧序列对拍 |
| 5 | 字幕 ASR/翻译 | 文本/时间轴对齐 |
| 6 | DLNA/VR + 打包 | 安装包替换，无 Python |
| 7 | 全量回归/删旧 | 用户数据无损 |

## 现实预期

- 这是**按月计**的重写；视觉样式机与字幕后端是两大不确定项。
- 字幕是最大风险：faster-whisper 依赖 CTranslate2，Rust 没有成熟等价物；
  要么用 `cxx` 绑 CTranslate2 C++ API，要么换 whisper.cpp（质量/速度需重测）。
- 我建议顺序：宿主壳 → 视觉 → 设备 → 字幕 → DLNA；每步可运行、可回退。
