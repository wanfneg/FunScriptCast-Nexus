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

[Files]
Source: "..\dist-app\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist-app\runtime\*"; DestDir: "{app}\runtime"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist-app\ui\*"; DestDir: "{app}\ui"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist-app\vendor\*"; DestDir: "{app}\vendor"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
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
