; FunScriptCast-Nexus —— Windows 安装包（Inno Setup 6）
;
;   ISCC.exe /DMyAppVersion=1.0.12 installer\setup.iss
;   （版本号由 build\build_installer.ps1 从 version.json 注入）
;
; 产物：dist-installer\FunScriptCast-Nexus-Setup-<版本>.exe
;   · 向导：欢迎 → 选目录（默认 %LOCALAPPDATA%\Programs）→ 附加任务 → 安装 → 完成
;   · 附加任务：桌面快捷方式（默认不勾）、开机自启动（默认不勾）
;   · 按用户安装（不需要管理员），自带卸载器
;
; ⚠️ **数据都在安装目录里**（用户自己选的那个文件夹）：`data\`（配置/术语表/设置/DLNA）、
;    `models\`（模型 + HF 缓存）、`logs\`。装到 D 盘就全在 D 盘，C 盘一个字节不落。
;    规则只写一份：`vendor\subtitle\user_paths.py`。因此：
;      · `data\` 是运行期产物 —— 本脚本**不安装它、也不删它**（只在新装时铺一份出厂种子），
;        升级安装自然保留用户的 key / 术语表 / 共享目录设置；
;      · 卸载时它同样保留（Inno 只删自己装过的东西），用户想彻底清就删整个安装目录。
;    ← 因为模型可能有 20GB+，**安装目录建议选在空间充足的盘上**（别用系统盘）。
;
; 自包含：runtime\ 内嵌官方 embeddable Python + 字幕服务依赖（见 build\make_runtime.ps1），
; 安装后不借用任何外部 .venv。模型文件（大体积数据）不进安装包，由界面一键下载。

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
; 从不删多余文件，于是升级完行为还是旧的）。**只删代码**：
;   · `{app}\data`、`{app}\logs`、`{app}\models`、`{app}\cache` **一个字都不碰** ——
;     那是运行数据与模型，删了就是把用户的 key / 术语表 / 共享目录 / 下好的模型清空。
;     （所以这一节里永远不出现 data\、logs\、models\、cache\。）
;   · `vendor\subtitle` 里只逐项删**代码**（*.py/*.pyc/*.md/*.txt/*.bat 与 docs/tools/
;     __pycache__ 目录）：历史上配置与词表就跟 .py 混在这个目录里，整目录删除曾等于把
;     词库和 key 一起清空（P0-2 事故的安装包版本）。R49 起数据已搬进 `data\`，这里更安全。
;   · `vendor\llama` 是 fetch_llama 下载的运行时二进制（约 1.1 GB），不在删除范围——
;     宁可留下旧 dll，也不删用户手里的运行时。
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
; 开发机留下的历史备份（含开发机路径），早期版本曾随包发出，升级时顺手清掉
Type: files; Name: "{app}\vendor\subtitle\config.json.bak-prompt"

[Files]
Source: "..\dist-app\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist-app\runtime\*"; DestDir: "{app}\runtime"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
Source: "..\dist-app\ui\*"; DestDir: "{app}\ui"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; ⚠ Excludes 里的 4 个数据文件是**老用户的迁移源**（R49 之前它们就住在这个目录里）：
;   升级那一刻，用户的云端 key 与术语表还在这份文件里，[Files] 整树覆盖会把它换成打包机
;   副本（key 为空）⇒ 首次迁移读到的就是空 key。所以这里排除掉，改由 data\ 那份种子负责。
;   开发者垃圾（__pycache__ / *.pyc / *.log / *.json.bak* / *.json.tmp）也一并排除：
;   "用户装到的是干净的"——包里不该有打包机的缓存、日志和历史备份。
Source: "..\dist-app\vendor\*"; DestDir: "{app}\vendor"; \
    Excludes: "subtitle\config.json,subtitle\config.json.bak-prompt,subtitle\glossary_ja_zh.json,subtitle\glossary_en_zh.json,subtitle\__pycache__\*,subtitle\logs\*,*.pyc,*.log,*.json.bak*,*.json.tmp"; \
    Flags: ignoreversion recursesubdirs createallsubdirs
; 出厂种子：**只在新装时铺**（onlyifdoesntexist）。装了就不再动它——用户的 key、术语表、
; 以及界面上调过的参数都在这三份文件里。配置在 data\ 里叫 subtitle_config.json。
Source: "..\dist-app\vendor\subtitle\config.json"; DestDir: "{app}\data"; \
    DestName: "subtitle_config.json"; Flags: onlyifdoesntexist
Source: "..\dist-app\vendor\subtitle\glossary_ja_zh.json"; DestDir: "{app}\data"; \
    Flags: onlyifdoesntexist
Source: "..\dist-app\vendor\subtitle\glossary_en_zh.json"; DestDir: "{app}\data"; \
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
