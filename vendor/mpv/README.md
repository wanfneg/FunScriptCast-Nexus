# vendor/mpv —— 桌面播放器运行时（libmpv）

本目录存放桌面播放器（媒体库内置播放）的运行时 `libmpv-2.dll`（约 121MB）。
该 DLL **不进 git**（见 .gitignore），由 `tools/fetch_mpv.ps1` 获取，构建时整体随安装包分发。

## 来源

- 构建方：[zhongfly/mpv-winbuild](https://github.com/zhongfly/mpv-winbuild) 的 `mpv-dev-x86_64-*.7z`
  （该仓库 `mpv-dev-lgpl-x86_64-*.7z` 为 LGPL 变体）。
- 上游：[mpv 项目](https://github.com/mpv-player/mpv)（`mpv-dev` 包内含 libmpv-2.dll 与开发头文件）。

## 许可证

- mpv 默认按 **GPLv2+** 分发（`-Dgpl=false` 构建才是 LGPLv2.1+，见下方 Copyright 摘录）。
  当前 fetch 脚本匹配的是**不含 lgpl 字样**的资产 → 随包的 libmpv-2.dll 为 GPL 构建。
- 本目录随附许可文本（逐字取自上游官方文件）：
  - `LICENSE.GPL-2.0.txt` —— GPLv2 全文（FSF 官方文本）；
  - `mpv-Copyright.txt` —— mpv 官方 Copyright 文件（顶层版权与各目录组件许可说明）。
- 本项目的 python-mpv 绑定（`vendor/player/mpv.py`）许可证随 libmpv（其文件头有原文）。

## 源码获取（GPL 合规）

- mpv 上游源码：https://github.com/mpv-player/mpv （随 libmpv-2.dll 的对应版本见 zhongfly 构建 tag）
- 构建脚本：https://github.com/zhongfly/mpv-winbuild
- 如需改为 LGPL 变体（对播放场景足够、许可更宽松）：把 `tools/fetch_mpv.ps1` 的资产
  匹配改为 `mpv-dev-lgpl-x86_64-*.7z` 后 `-Force` 重下（见 docs/THIRD-PARTY-NOTICES.md）。
