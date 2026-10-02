# vendor/mpv —— 桌面播放器运行时（libmpv）

本目录存放桌面播放器（媒体库内置播放 / 缩略图抽帧）的运行时 `libmpv-2.dll`
（约 96MB）。该 DLL **不进 git**（见 .gitignore），由 `tools/fetch_mpv.ps1` 获取，
构建时整体随安装包分发。

## 来源

- 构建方：[zhongfly/mpv-winbuild](https://github.com/zhongfly/mpv-winbuild) 的
  **`mpv-dev-lgpl-x86_64-*.7z`（LGPL 变体）**——fetch 脚本显式匹配 lgpl 资产并排除 v3。
- 上游：[mpv 项目](https://github.com/mpv-player/mpv)（`mpv-dev` 包内含 libmpv-2.dll）。
- 当前版本：`v0.41.0-1092-g3186d369f`（与 GPL 变体同日同提交，仅构建配置不同）。

## 许可证

- **LGPL 构建整体按 LGPLv2.1+ 分发**（mpv `-Dgpl=false` 构建：不含任何 GPL-only
  组件；被禁用的主要是编码器与少数滤镜，本程序只用解码/播放/抽帧，功能无影响）。
- 本目录随附许可文本（逐字取自上游官方文件）：
  - `LICENSE.LGPL-2.1.txt` —— LGPLv2.1 全文（**当前适用**，FSF 官方文本）；
  - `LICENSE.GPL-2.0.txt` —— GPLv2 全文（保留：`mpv-Copyright.txt` 引用它，
    且若未来误用 GPL 变体时可对照）；
  - `mpv-Copyright.txt` —— mpv 官方 Copyright 文件（顶层版权与各目录组件许可说明）。
- 本项目的 python-mpv 绑定（`vendor/player/mpv.py`）许可证随 libmpv（其文件头有原文）。

## 历史与决策（R118，2026-10-02）

初版 fetch 脚本取的是 `mpv-dev-x86_64-*.7z`（默认 **GPL** 构建）——与 MIT 主程序的
对外声明混搭；经用户拍板当日改为 **lgpl 变体**（脚本一行改动 + 重下，抽帧实测通过，
D 盘/dist-app 已同步）。对播放场景两者无差别：LGPL 版少的只是编码器等 GPL-only 组件。

## 源码获取（LGPL 合规）

- mpv 上游源码：https://github.com/mpv-player/mpv （对应版本见 zhongfly 构建 tag）
- 构建脚本：https://github.com/zhongfly/mpv-winbuild
