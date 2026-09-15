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

Write-Host "[1/4] 构建应用与 dist-app…" -ForegroundColor Cyan
$exeArgs = @('-ExecutionPolicy', 'Bypass', '-File', (Join-Path $PSScriptRoot 'build_exe.ps1'))
if ($NoBump) { $exeArgs += '-NoBump' }
& powershell @exeArgs
if ($LASTEXITCODE -ne 0) { throw "build_exe 失败（exit $LASTEXITCODE）" }

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
if ($LASTEXITCODE -ne 0) { throw "ISCC 失败（exit $LASTEXITCODE）" }

$out = Join-Path $root 'dist-installer'
Write-Host "[完成] 安装包：" -ForegroundColor Green
Get-ChildItem $out -Filter "*.exe" | Select-Object Name, @{n='Size';e={ "{0:N1} MB" -f ($_.Length/1MB) }} | Format-Table -AutoSize
