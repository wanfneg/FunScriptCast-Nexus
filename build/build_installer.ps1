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
if ($stashKey) {
    try {
        $cfg = Get-Content $distCfg -Raw -Encoding UTF8 | ConvertFrom-Json
        $cfg.translate.openai.api_key = ''
        [IO.File]::WriteAllText($distCfg, ($cfg | ConvertTo-Json -Depth 24),
            (New-Object System.Text.UTF8Encoding($false)))
        Write-Host "  已暂存 dist-app 的云端 key（打包后回填）" -ForegroundColor DarkGray
    } catch { $stashKey = '' }
}

# ---- 哨兵（第 2 道）：打包对象全树扫描 --------------------------------------
# 上面的主检查只看 vendor\subtitle\config.json 一个文件；但 setup.iss 打的是
# dist-app\vendor\* 递归——.bak 配置、用户自建的 json、tools 里误放的脚本
# 都可能带 key。扫一遍整个 dist-app（排除 runtime 的第三方库与运行日志），
# 任何非空 api_key 都拒绝出包。
Write-Host "[2/4] 哨兵扫描：dist-app 全树 API Key 检查…" -ForegroundColor Cyan
$hits = @()
$distOut = Join-Path $root 'dist-app'
Get-ChildItem $distOut -Recurse -File -ErrorAction SilentlyContinue |
    Where-Object {
        $_.FullName -notmatch '\\runtime\\' -and $_.FullName -notmatch '\\logs\\' -and
        $_.FullName -notmatch '__pycache__' -and $_.Extension -in '.json', '.bak-prompt', '.bak'
    } |
    ForEach-Object {
        $m = Select-String -LiteralPath $_.FullName -Pattern '"api[_-]?key"\s*:\s*"([^"]+)"' -AllMatches -ErrorAction SilentlyContinue
        if ($m) {
            foreach ($x in $m.Matches) {
                $v = $x.Groups[1].Value
                if ($v -and $v -notlike '*`**') {
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
$isccFailed = ($LASTEXITCODE -ne 0)

# 回填 key（无论打包成败都要做，否则本机 dist-app 丢 key）
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
if ($isccFailed) { throw "ISCC 失败（exit $LASTEXITCODE）" }

$out = Join-Path $root 'dist-installer'
Write-Host "[完成] 安装包：" -ForegroundColor Green
Get-ChildItem $out -Filter "*.exe" | Select-Object Name, @{n='Size';e={ "{0:N1} MB" -f ($_.Length/1MB) }} | Format-Table -AutoSize
