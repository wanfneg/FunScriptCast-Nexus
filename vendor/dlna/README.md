# VR-DLNA（抚物器）

面向 **DeoVR + 云盘/网盘挂载** 的轻量 DLNA 媒体服务器。

## 功能

- 通过 DLNA 在 VR/手机上播放电脑里的视频
- 支持云盘/网盘挂载目录，浏览速度快
- 支持外挂字幕（`.srt / .ass / .vtt`）
- 支持 `.strm` 链接代理
- 附带 funscript / 视频同步工具

## 使用说明

1. 从 GitHub Releases 下载最新 EXE：
   https://github.com/wanfneg/VR-DLNA/releases/latest
2. 双击运行 EXE。
3. 添加要共享的视频文件夹。
4. 启动 DLNA 服务。
5. 在 DeoVR 中自动发现并播放。

源码运行：

```powershell
python vr_dlna.py
```
