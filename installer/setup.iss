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
; 升级安装：沿用上次选的目录（**不会**装成第二份）。同一个 AppId 让 Inno 认出已有安装，
; 附加任务（桌面图标/开机自启）的选择也一并沿用（UsePreviousTasks 默认就是 yes）。
UsePreviousAppDir=yes
PrivilegesRequired=lowest
; 让安装包 EXE 自己在文件属性里带版本号 —— 用户右键就能看出手里这个是哪一版。
; （R47 的教训：发行资产落后两个修复轮却没人发现，只能靠比 mtime。有这个就不用猜了。）
; ⚠ 值必须是纯数字 x.y.z（version.json 的 versionName 一直是这种形式）。
VersionInfoVersion={#MyAppVersion}
VersionInfoProductVersion={#MyAppVersion}
VersionInfoProductName={#MyAppName}
VersionInfoDescription={#MyAppName} 安装程序
VersionInfoCompany={#MyAppPublisher}
; 安装日志落到 %TEMP%\Setup Log*.txt：装失败时用户能把它发过来，不必靠回忆。
SetupLogging=yes
OutputDir=..\dist-installer
OutputBaseFilename={#MyAppName}-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
DisableProgramGroupPage=yes

[Languages]
Name: "chinesesimplified"; MessagesFile: "compiler:Languages\ChineseSimplified.isl"

[Messages]
; 首页就把"升级会不会动我的数据"说清楚——这是用户最担心的点，也是本安装包最该讲清的规则。
WelcomeLabel2=即将安装 [name/ver] 到你的电脑。%n%n· 升级安装：只覆盖程序文件，你的数据（data\ 目录：云端 Key、术语表、DLNA 共享目录）与已下载的模型都会保留，不会重新下载。%n· 全新安装：请选一个空间充足的目录 —— 模型可能占用 20GB 以上，建议不要装在系统盘。%n%n继续前请先关闭正在运行的程序。

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

[Code]
{ ── 升级识别 ────────────────────────────────────────────────────────────────
  Inno 靠 AppId 认出"已有安装"并沿用目录，但它**不会告诉你装的是哪一版，也不比较新旧**：
  拿一个旧安装包覆盖新装会静默降级，同版本重装也毫无提示。R47 那次"发行资产落后两个
  修复轮"正是这种沉默的土壤。所以这里读一次注册表里的已装版本，把四种情形讲明白。 }
const
  { ⚠ 必须与 [Setup] 的 AppId **逐字一致**（含花括号）：Inno 就是用它建这条卸载注册项。
    改了 AppId 却忘了改这里（或反之），升级识别会失效——表现是"检测不到已安装版本"，
    而且**不报错**。 }
  UninstallKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\{8F3A9D52-6B7E-4C31-9A48-D2E1F0C5B7A3}_is1';
  { 换行。**不要**在续行开头直接写 #13#10 —— ISPP 会把行首的 '#' 当成预处理器指令，
    报 "Unknown preprocessor directive"（已实测踩过）。 }
  NL = #13#10;

var
  PrevVersion: String;
  PrevDir: String;

{ 取版本号第 Index 段（从 1 起）。只吃数字与点；遇到 '-' 之类后缀就停。 }
function VerPart(const S: String; Index: Integer): Integer;
var
  i, part: Integer;
  cur: String;
begin
  Result := 0;
  part := 1;
  cur := '';
  for i := 1 to Length(S) do
  begin
    if S[i] = '.' then
    begin
      if part = Index then
      begin
        Result := StrToIntDef(cur, 0);
        Exit;
      end;
      Inc(part);
      cur := '';
    end
    else if (S[i] >= '0') and (S[i] <= '9') then
      cur := cur + S[i]
    else
      Break;
  end;
  if part = Index then
    Result := StrToIntDef(cur, 0);
end;

{ 逐段比较，A<B 返回 -1，A=B 返回 0，A>B 返回 1。 }
function CompareVer(const A, B: String): Integer;
var
  i, x, y: Integer;
begin
  Result := 0;
  for i := 1 to 4 do
  begin
    x := VerPart(A, i);
    y := VerPart(B, i);
    if x < y then begin Result := -1; Exit; end;
    if x > y then begin Result := 1; Exit; end;
  end;
end;

function InitializeSetup(): Boolean;
var
  msg: String;
  rc: Integer;
begin
  Result := True;
  PrevVersion := '';
  PrevDir := '';
  RegQueryStringValue(HKEY_CURRENT_USER, UninstallKey, 'DisplayVersion', PrevVersion);
  RegQueryStringValue(HKEY_CURRENT_USER, UninstallKey, 'InstallLocation', PrevDir);
  { 落进安装日志（SetupLogging=yes → %TEMP%\Setup Log*.txt）：装出问题时能看出
    它到底认没认出已装版本，而不是靠猜。 }
  Log('升级检测：已装版本=' + PrevVersion + ' / 本包版本={#MyAppVersion} / 位置=' + PrevDir);

  { 全新安装：不打扰，直接进向导选目录 }
  if PrevVersion = '' then
    Exit;

  if PrevDir <> '' then
    msg := '安装位置：' + PrevDir + NL + NL;

  rc := CompareVer(PrevVersion, '{#MyAppVersion}');
  Log('升级检测：判定=' + IntToStr(rc) + '（-1 升级 / 0 同版本 / 1 降级）');

  if rc > 0 then
  begin
    { 已装的比本安装包更新 → 默认不降级（默认按钮落在"否"） }
    if MsgBox('检测到已安装【更新】的版本：' + PrevVersion + NL +
              '本安装包是较旧的 {#MyAppVersion}。' + NL + NL +
              '继续会用旧版程序文件覆盖当前安装（你的数据与已下载的模型不受影响），' +
              '通常不是你想要的。' + NL + NL + msg + '仍要降级安装吗？',
              mbConfirmation, MB_YESNO or MB_DEFBUTTON2) <> IDYES then
      Result := False;
  end
  else if rc = 0 then
  begin
    if MsgBox('检测到已安装同版本（' + PrevVersion + '）。' + NL + NL +
              '继续将重新覆盖程序文件（可用于修复损坏的安装）；' +
              '你的数据（data\）与已下载的模型会保留。' + NL + NL + msg +
              '要重新安装吗？', mbConfirmation, MB_YESNO) <> IDYES then
      Result := False;
  end
  else
  begin
    MsgBox('检测到已安装 {#MyAppVersion} 之前的版本：' + PrevVersion + NL +
           '将升级到 {#MyAppVersion}。' + NL + NL +
           '· 只覆盖程序文件，不会重新下载模型' + NL +
           '· 你的数据保留：data\（云端 Key / 术语表 / DLNA 共享目录）' + NL +
           '· 老版本放在 vendor\subtitle\ 或 %APPDATA% 的数据，首次运行会自动迁移过来' +
           NL + NL + msg, mbInformation, MB_OK);
  end;
end;

{ 用户数据（data\、cache\、logs\、models\）卸载时有意保留：Inno 默认只删除它安装过的文件。
  ⚠ 这一行必须是 Pascal 注释（花括号），不能写 `;` —— [Code] 段之后 `;` 不再是注释，
  它会当成新例程的开头并报 "'BEGIN' expected"（已实测踩过）。 }
