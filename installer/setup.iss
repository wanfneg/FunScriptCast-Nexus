; FunScriptCast-Nexus —— Windows 安装包（Inno Setup 6）
;
;   ISCC.exe /DMyAppVersion=1.0.12 installer\setup.iss
;   （版本号由 build\build_installer.ps1 从 version.json 注入）
;
; 产物：dist-installer\FunScriptCast-Nexus-Setup-<版本>.exe
;   · 向导：欢迎 → 选目录（默认 %LOCALAPPDATA%\Programs）→ 附加任务 → 安装 → 完成
;   · 附加任务：桌面快捷方式（默认不勾）、开机自启动（默认不勾）
;   · 按用户安装（不需要管理员），自带卸载器；用户数据（cache\、models 配置）保留
;
; 自包含：runtime\ 内嵌官方 embeddable Python + 字幕服务依赖（见 build\make_runtime.ps1），
; 安装后不借用任何外部 .venv。模型文件（大体积数据）不进安装包，由 config.json 指向。

#define MyAppName "FunScriptCast-Nexus"
#define MyAppPublisher "FunScriptCast"
#define MyAppExeName "FunScriptCast-Nexus.exe"

#ifndef MyAppVersion
#define MyAppVersion "0.0.0"
#endif

[Setup]
AppId={{8F3A9D52-6B7E-4C31-9A48-D2E1F0C5B7A3}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 运行中升级：宿主启动时会创建这个命名互斥量（host_server._create_app_mutex），
; 安装器检测到就提示用户先关闭程序，而不是覆写 exe 失败后留一堆裸报错。
AppMutex=FunScriptCastNexusMutex
DefaultDirName={localappdata}\Programs\{#MyAppName}
UsePreviousAppDir=yes
PrivilegesRequired=lowest
OutputDir=..\dist-installer
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
DisableProgramGroupPage=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; \
    GroupDescription: "附加任务："; Flags: unchecked
Name: "autostart"; Description: "开机自动启动 FunScriptCast-Nexus（当前用户）"; \
    GroupDescription: "附加任务："; Flags: unchecked

[InstallDelete]
; 升级安装必须清掉"上一版有、这一版不再分发"的旧文件（坑 #20 同族：Inno 只覆盖同名文件，
; 从不删多余文件，于是升级完行为还是旧的）。**只删代码**，且显式避开运行数据：
;   · {app}\vendor\subtitle 里混着用户在 UI 里攒出来的运行数据 —— config.json（含云端
;     key）、glossary_*.json（术语表）、*.json.bak*（宿主自保备份）。整目录删除等于把
;     词库和 key 一起清空（正是 P0-2 那类事故的安装包版本），所以这一层只逐项删代码文件。
;   · cache\、logs\ 是运行产物，卸载时有意保留，这里同样不动。
;   · vendor\llama 是 fetch_llama.ps1 下载的运行时二进制（约 1.1 GB，可能未随包分发或被
;     用户自行升级/替换），不在删除范围——宁可留下旧 dll，也不删用户手里的运行时。
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\ui"
Type: filesandordirs; Name: "{app}\tools"
Type: filesandordirs; Name: "{app}\vendor\dlna"
Type: filesandordirs; Name: "{app}\vendor\subtitle\docs"
Type: filesandordirs; Name: "{app}\vendor\subtitle\tools"
Type: filesandordirs; Name: "{app}\vendor\subtitle\__pycache__"
Type: files; Name: "{app}\vendor\subtitle\*.py"
Type: files; Name: "{app}\vendor\subtitle\*.pyc"
Type: files; Name: "{app}\vendor\subtitle\*.md"
Type: files; Name: "{app}\vendor\subtitle\*.txt"
Type: files; Name: "{app}\vendor\subtitle\*.bat"

[Files]
Source: "..\dist-app\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist-app\runtime\*"; DestDir: "{app}\runtime"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist-app\ui\*"; DestDir: "{app}\ui"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist-app\vendor\*"; DestDir: "{app}\vendor"; \
    Excludes: "subtitle\config.json,subtitle\config.json.bak-prompt,subtitle\glossary_ja_zh.json,subtitle\glossary_en_zh.json"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; ⚠ 上面这条**必须** Excludes 掉这几样 —— 它们是「用户数据迁移源」：
;   运行时的真身在 %APPDATA%\FunScriptCast-Nexus\（见 vendor\subtitle\user_paths.py），
;   安装目录这份只是出厂模板/种子。但**升级那一刻**，老用户的云端 key 与词表还在旧位置的
;   这份文件里，[Files] 整树覆盖会把它换成打包机副本（key 为空）⇒ 首次迁移就再也读不到
;   key。所以下面单独铺一份，且 onlyifdoesntexist：全新安装有种子、升级不覆盖。
;   （config.json.bak-prompt 是开发机的历史备份，含开发机路径，直接不发。）
Source: "..\dist-app\vendor\subtitle\config.json"; DestDir: "{app}\vendor\subtitle"; \
    Flags: onlyifdoesntexist
Source: "..\dist-app\vendor\subtitle\glossary_ja_zh.json"; DestDir: "{app}\vendor\subtitle"; \
    Flags: onlyifdoesntexist
Source: "..\dist-app\vendor\subtitle\glossary_en_zh.json"; DestDir: "{app}\vendor\subtitle"; \
    Flags: onlyifdoesntexist
Source: "..\dist-app\tools\*"; DestDir: "{app}\tools"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist-app\version.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist-app\start.bat"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist-app\README.md"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; \
    Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; \
    ValueType: string; ValueName: "{#MyAppName}"; \
    ValueData: """{app}\{#MyAppExeName}"""; Tasks: autostart; \
    Flags: uninsdeletevalue

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

; 用户数据（cache\、日志）卸载时有意保留：Inno 默认只删除它安装过的文件。
