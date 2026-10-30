//! 应用图标：exe/窗口/托盘/前端标题栏共用 `tools/icon.png`。

/// 返回 RGBA 像素与尺寸（编译期嵌入，不依赖安装目录文件）。
pub fn rgba() -> Option<(Vec<u8>, u32, u32)> {
    let bytes = include_bytes!("../../../../tools/icon.png");
    let img = image::load_from_memory(bytes).ok()?.into_rgba8();
    let (w, h) = img.dimensions();
    Some((img.into_raw(), w, h))
}
