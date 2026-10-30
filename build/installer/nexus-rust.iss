; Rust 版安装包（无 Python 运行时）
; 由 build\build_rust.ps1 调用：ISCC /DMyAppVersion=... /DStageDir=<dist> nexus-rust.iss
;
; 与 Python 版 setup.iss 的差异：
;   · 不再安装 runtime\（Python 嵌入运行时）与 vision-runtime\
;   · 仍然带 vendor\vision\bundles（ONNX 模型，约 222MB）与 vendor\audiocpp\cpu（ASR，约 27MB）
;   · vendor\llama（翻译运行时，约 1.1GB）与 audiocpp GPU 版仍走界面下载

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef StageDir
  #define StageDir "..\..\artifacts\app-rust"
#endif

#define MyAppName "FunScriptCast-Nexus"
; 显示名带后缀：Python 版与本版同时装着时，用户要能在「应用和功能」里分清
#define MyDisplayName "FunScriptCast-Nexus"
#define MyAppExeName "FunScriptCast-Nexus.exe"
#define MyAppPublisher "FunScriptCast-Nexus"

[Setup]
AppId={{8F3C1D42-5B7A-4E19-9C2D-7A1B6E4F0C31}
AppName={#MyDisplayName}
SetupIconFile=..\..\tools\icon.ico
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 免管理员安装（对齐 Python 版 setup.iss）：装进当前用户目录，不需要 UAC 提权
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=..\..\artifacts\installer-rust
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ArchitecturesAllowed=x64compatible
; 安装/卸载前关掉正在跑的宿主，否则文件被占用
CloseApplications=yes
RestartApplications=no
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[InstallDelete]
; 从 Python 版升级过来时，清掉旧运行时（新版不需要，留着只是占地方）
Type: filesandordirs; Name: "{app}\runtime"
Type: filesandordirs; Name: "{app}\vision-runtime"
Type: filesandordirs; Name: "{app}\ui"
; CUDA 已改为可选下载：清掉旧版随包的 CUDA 运行时，避免升级后白占 3GB
Type: filesandordirs; Name: "{app}\vendor\llama"
Type: filesandordirs; Name: "{app}\vendor\audiocpp\gpu"

[Files]
Source: "{#StageDir}\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
; 外部工具：AI/媒体库与设备同步必需（放 {app} 根，Windows 优先从 exe 目录找）
Source: "{#StageDir}\ffmpeg.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageDir}\ffprobe.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageDir}\tools\adb\*"; DestDir: "{app}\tools\adb"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\version.json"; DestDir: "{app}"; Flags: ignoreversion
Source: "{#StageDir}\README.md"; DestDir: "{app}"; Flags: ignoreversion; Check: FileExistsAtPath('{#StageDir}\README.md')
Source: "{#StageDir}\ui-legacy\*"; DestDir: "{app}\ui-legacy"; Flags: ignoreversion recursesubdirs createallsubdirs
; 视觉模型：只在缺失时铺（升级不重下 222MB）
Source: "{#StageDir}\vendor\vision\bundles\*"; DestDir: "{app}\vendor\vision\bundles"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\vendor\vision\config.json"; DestDir: "{app}\vendor\vision"; Flags: ignoreversion
; ASR 运行时（CPU + Vulkan 随包；CUDA 改为界面可选下载）
Source: "{#StageDir}\vendor\audiocpp\cpu\*"; DestDir: "{app}\vendor\audiocpp\cpu"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\vendor\audiocpp\vulkan\*"; DestDir: "{app}\vendor\audiocpp\vulkan"; Flags: ignoreversion recursesubdirs createallsubdirs
; silero VAD 模型（audiocpp_cli --task vad 用；缺失则混合切句降级 RMS+硬切）
Source: "{#StageDir}\vendor\audiocpp\assets\*"; DestDir: "{app}\vendor\audiocpp\assets"; Flags: ignoreversion recursesubdirs createallsubdirs
; 翻译运行时（llama.cpp Vulkan）随包，模型仍走下载；CUDA 改为可选下载
Source: "{#StageDir}\vendor\llama-vulkan\*"; DestDir: "{app}\vendor\llama-vulkan"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageDir}\vendor\audiocpp\LICENSE"; DestDir: "{app}\vendor\audiocpp"; Flags: ignoreversion; Check: FileExistsAtPath('{#StageDir}\vendor\audiocpp\LICENSE')
; ONNX Runtime 运行库（视觉推理必需）
Source: "{#StageDir}\runtime\ort\*"; DestDir: "{app}\runtime\ort"; Flags: ignoreversion recursesubdirs createallsubdirs; Check: FileExistsAtPath('{#StageDir}\runtime\ort\onnxruntime.dll')
; 字幕配置种子：只在新装时铺
Source: "{#StageDir}\subtitle_config.seed.json"; DestDir: "{app}\var\data"; DestName: "subtitle_config.json"; Flags: onlyifdoesntexist; Check: FileExistsAtPath('{#StageDir}\subtitle_config.seed.json')

[Dirs]
Name: "{app}\var\data"
Name: "{app}\var\logs"
Name: "{app}\var\run"
Name: "{app}\models"
Name: "{app}\vendor\llama"

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
; 桌面快捷方式名加后缀：与 Python 版同名快捷方式会同位覆盖（Python 版桌面名不带后缀）
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 界面下载的更新残留（旧 Setup 接近 GB 级 + 交接脚本），卸载时一并清掉。
; models/var 是用户数据（GB 级模型、观看进度），保留不清。
Type: filesandordirs; Name: "{app}\var\data\update"

[UninstallRun]
; 开机自启动键必须删：残留会导致卸载后每次开机报"找不到程序"
Filename: "reg.exe"; Parameters: "delete ""Software\Microsoft\Windows\CurrentVersion\Run"" /v ""FunScriptCast-Nexus"" /f"; Flags: runhidden; RunOnceId: "DelAutostart"

[Code]
{ 可选文件的存在性判断：README / LICENSE / 配置种子不是每次都有，
  没有就跳过对应条目，而不是让 ISCC 报"源文件不存在"。 }
function FileExistsAtPath(Path: String): Boolean;
begin
  Result := FileExists(Path);
end;