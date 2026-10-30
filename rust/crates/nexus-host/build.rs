fn main() {
    #[cfg(windows)]
    {
        // 把应用图标嵌进 exe：资源管理器/任务栏/安装包快捷方式都读它。
        // 路径相对 crate 根目录（rust/crates/nexus-host）→ 仓库 tools/icon.ico。
        let mut res = winresource::WindowsResource::new();
        res.set_icon("../../../tools/icon.ico");
        if let Err(e) = res.compile() {
            println!("cargo:warning=嵌入应用图标失败：{e}");
        }
    }
}
