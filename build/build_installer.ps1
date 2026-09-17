# 构建 Windows 安装包（Inno Setup）→ dist-installer\FunScriptCast-Nexus-Setup-<版本>.exe
#
#   powershell -ExecutionPolicy Bypass -File build\build_installer.ps1
#   powershell -ExecutionPolicy Bypass -File build\build_installer.ps1 -NoBump
#
# 流程：build_exe.ps1（PyInstaller + 组装 dist-app）
#     → make_runtime.ps1（自带 Python 运行时：embeddable + 轻量依赖）
#     → Inno Setup 编译 installer\setup.iss
#
# 前置：Inno Setup 6（ISCC.exe）。查找顺序：NEXUS_ISCC 环境变量 →
#       "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" → "C:\Program Files\Inno Setup 6\ISCC.exe"
param([switch]$NoBump)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot

# ---- 哨兵：绝不让云端 API Key 进安装包 --------------------------------------
# setup.iss 打包的是 dist-app\vendor\*，其中 config.json 可能留着你填的云端 key。
# 本脚本第 1 步会用它自己的 vendor 覆盖 dist-app（配置来自仓库），所以"会被打包的"
# 是**仓库那份** —— 只要它带 key 就直接拒绝，不给"手滑打包出去"的机会。
$repoCfg = Join-Path $root 'vendor\subtitle\config.json'
if (Test-Path $repoCfg) {
    $k = ''
    try { $k = (Get-Content $repoCfg -Raw -Encoding UTF8 | ConvertFrom-Json).translate.openai.api_key } catch { }
    if ($k) {
        throw ("拒绝打包：仓库配置里带着 API Key（{0}）。`n" +
               "  请先清空 translate.openai.api_key（界面填的 key 应只留在 dist-app 的运行配置里），再重新打包。") -f $repoCfg
    }
}
# dist-app 运行配置里的 key：build_exe 现在会原样保留（坑 #12 已关），所以
# 走「ISCC 前摘除、打包后回填」——安装包里永远没有 key，本机 dist-app 不丢。
$distOut = Join-Path $root 'dist-app'
$distCfg = Join-Path $distOut 'vendor\subtitle\config.json'

Write-Host "[1/4] 构建应用与 dist-app…" -ForegroundColor Cyan
$exeArgs = @('-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'build_exe.ps1'))
if ($NoBump) { $exeArgs += '-NoBump' }
& powershell @exeArgs
if ($LASTEXITCODE -ne 0) { throw "build_exe 失败（exit $LASTEXITCODE）" }

$stashKey = ''
if (Test-Path $distCfg) {
    try { $stashKey = [string]((Get-Content $distCfg -Raw -Encoding UTF8 | ConvertFrom-Json).translate.openai.api_key) } catch { }
}

# ⚠️ 从"清空 dist-app 的 key"这一刻起，直到回填完成，中间**任何**抛出都会让用户
# 永久丢 key：旧代码把回填放在最后一步 ISCC 之后，而哨兵 throw、make_runtime 失败、
# 找不到 ISCC、语言包复制失败都在两者之间——进程带着内存里的 $stashKey 直接死掉，
# dist-app\vendor\subtitle\config.json 永久留空（注释写的"无论打包成败都要做"
# 只覆盖了 ISCC 失败这一种）。因此摘除之后的全部步骤包进 try，回填放 finally。
$isccFailed = $false
$isccExit = 0
try {
    if ($stashKey) {
        try {
            $cfg = Get-Content $distCfg -Raw -Encoding UTF8 | ConvertFrom-Json
            $cfg.translate.openai.api_key = ''
            [IO.File]::WriteAllText($distCfg, ($cfg | ConvertTo-Json -Depth 24),
                (New-Object System.Text.UTF8Encoding($false)))
            Write-Host "  已暂存 dist-app 的云端 key（打包后回填）" -ForegroundColor DarkGray
        } catch { $stashKey = '' }
    }

    # ---- 哨兵（第 2 道）：打包对象全树扫描 ----------------------------------
    # setup.iss 打的是 dist-app\{runtime,ui,vendor,tools}\*（**递归**），所以扫描
    # 范围也必须是全树。旧版是白名单（只扫 .json/.bak/.bak-prompt）却在注释里声称
    # "全树扫描…任何非空 api_key 都拒绝出包"：.py/.ps1/.txt/.env/.ini/*.old 一律漏扫
    # ——而"tools 里误放的脚本"正是那句注释点名的场景；`\logs\` 被整体排除，可它是
    # 会被递归打包的目录。这里改成黑名单：只跳过不可能是配置文本的二进制/媒体/字体，
    # 其余一律扫；日志只跳过 *.log 文件（不是整个 logs 目录）。
    Write-Host "[2/4] 哨兵扫描：dist-app 全树 API Key 检查…" -ForegroundColor Cyan
    $hits = @()
    # 扫描范围 = setup.iss 真正打包的内容：dist-app\{vendor,ui,tools}\* 递归 + 根目录散文件
    #（EXE / version.json / start.bat / README.md）。**有意跳过**：
    #   · runtime\ —— 第三方解释器与依赖（随包分发但不是本项目文本）
    #   · models\ 与 .venv\ —— 二者是**指向仓库真身的 junction**，既不进安装包，
    #     递归进去还要读 8 GB 模型与整仓依赖（旧版靠扩展名白名单"顺带"躲开了它们）
    #   · __pycache__ / *.log —— 编译产物与运行日志
    # 其余一律扫（黑名单只排不可能是配置文本的二进制/媒体/字体扩展名）。
    $scanFiles = @()
    foreach ($r in @((Join-Path $distOut 'vendor'), (Join-Path $distOut 'ui'), (Join-Path $distOut 'tools'))) {
        if (Test-Path $r) { $scanFiles += Get-ChildItem $r -Recurse -File -ErrorAction SilentlyContinue }
    }
    $scanFiles += @(Get-ChildItem $distOut -File -ErrorAction SilentlyContinue)
    $skipExt = @(
        '.exe', '.dll', '.pyd', '.so', '.lib', '.obj', '.pyc', '.zip', '.gz', '.7z', '.rar',
        '.png', '.jpg', '.jpeg', '.gif', '.ico', '.bmp', '.webp', '.svgz',
        '.woff', '.woff2', '.ttf', '.otf', '.eot',
        '.mp3', '.wav', '.flac', '.mp4', '.mkv', '.webm', '.avi',
        '.onnx', '.bin', '.pt', '.pth', '.gguf', '.whl', '.npy', '.npz', '.h5', '.pb', '.tflite', '.model',
        '.db', '.sqlite', '.sqlite3', '.dat', '.pdf', '.msgpack', '.cache'
    )
    # 文档示例占位符：**完全等于**下列字符串才放过（不做通配匹配）。这不是给真 key 开后门
    # ——含 '*' 的值照样计入命中（旧版 `-notlike '*`**'` 会把带 '*' 的真 key 放过去）；
    # 目的只是别让 README 里的示例写法把打包莫名拦下。新增占位符写法时显式加进这里。
    $docPlaceholders = @('sk-…', 'sk-xxx', 'sk-xxxx', 'sk-your-key', 'xxx', 'xxxx', 'your-api-key', '<API_KEY>')
    $scanFiles |
        Where-Object {
            $_.FullName -notmatch '\\runtime\\' -and
            $_.FullName -notmatch '__pycache__' -and
            $_.Name -notlike '*.log' -and
            $skipExt -notcontains $_.Extension.ToLowerInvariant()
        } |
        ForEach-Object {
            $m = Select-String -LiteralPath $_.FullName -Pattern '"api[_-]?key"\s*:\s*"([^"]+)"' -AllMatches -ErrorAction SilentlyContinue
            if ($m) {
                foreach ($x in $m.Matches) {
                    $v = $x.Groups[1].Value
                    if ($v -and ($docPlaceholders -notcontains $v)) {
                        $rel = $_.FullName.Substring($distOut.Length + 1)
                        $hits += ("{0} → {1}…" -f $rel, $v.Substring(0, [Math]::Min(6, $v.Length)))
                    }
                }
            }
        }
    if ($hits) {
        throw ("拒绝打包：以下文件里带 API Key：`n  " + ($hits -join "`n  ") +
               "`n  请清空后重打包（用户 key 只应存在于本机运行配置，绝不进安装包）。")
    }

    Write-Host "[2/4] 构建自带运行时…" -ForegroundColor Cyan
    & powershell -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'make_runtime.ps1')
    if ($LASTEXITCODE -ne 0) { throw "make_runtime 失败（exit $LASTEXITCODE）" }

    $version = (Get-Content (Join-Path $root 'dist-app\version.json') -Raw | ConvertFrom-Json).versionName
    Write-Host "版本：$version" -ForegroundColor Cyan

    Write-Host "[3/4] 定位 Inno Setup（ISCC.exe）…" -ForegroundColor Cyan
    $iscc = $env:NEXUS_ISCC
    if (-not $iscc -or -not (Test-Path $iscc)) {
        foreach ($c in @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
                         "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
                         "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe")) {
            if ($c -and (Test-Path $c)) { $iscc = $c; break }
        }
    }
    if (-not $iscc -or -not (Test-Path $iscc)) {
        throw "找不到 ISCC.exe。请安装 Inno Setup 6，或设置环境变量 NEXUS_ISCC 指向 ISCC.exe"
    }

    # 中文字幕语言包（Inno 6.3 安装器尚未收录，随仓库分发：installer\Languages\）
    $isccDir = Split-Path -Parent $iscc
    $langDst = Join-Path $isccDir 'Languages\ChineseSimplified.isl'
    if (-not (Test-Path $langDst)) {
        Copy-Item (Join-Path $root 'installer\Languages\ChineseSimplified.isl') $langDst -Force
        Write-Host "  已安装中文语言包到 $langDst" -ForegroundColor Yellow
    }

    Write-Host "[4/4] 编译安装包…" -ForegroundColor Cyan
    & $iscc "/DMyAppVersion=$version" (Join-Path $root 'installer\setup.iss')
    $isccExit = $LASTEXITCODE
    $isccFailed = ($isccExit -ne 0)
} finally {
    # 回填 key（无论打包成败、也无论中间哪一步抛出——finally 是唯一可靠的位置）
    if ($stashKey -and (Test-Path $distCfg)) {
        try {
            $cfg = Get-Content $distCfg -Raw -Encoding UTF8 | ConvertFrom-Json
            if (-not [string]$cfg.translate.openai.api_key) {
                $cfg.translate.openai.api_key = $stashKey
                [IO.File]::WriteAllText($distCfg, ($cfg | ConvertTo-Json -Depth 24),
                    (New-Object System.Text.UTF8Encoding($false)))
                Write-Host "  已把云端 key 回填到 dist-app 运行配置" -ForegroundColor DarkGray
            }
        } catch {
            Write-Host "  key 回填失败（$($_.Exception.Message)），请在界面重新填一次" -ForegroundColor Yellow
        }
    }
}
if ($isccFailed) { throw "ISCC 失败（exit $isccExit）" }

$out = Join-Path $root 'dist-installer'
Write-Host "[完成] 安装包：" -ForegroundColor Green
Get-ChildItem $out -Filter "*.exe" | Select-Object Name, @{n='Size';e={ "{0:N1} MB" -f ($_.Length/1MB) }} | Format-Table -AutoSize
