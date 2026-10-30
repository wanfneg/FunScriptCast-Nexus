# 构建 Rust 版安装包 → artifacts/installer-rust/FunScriptCast-Nexus-Rust-Setup-<版本>.exe
#
#   powershell -ExecutionPolicy Bypass -File build\build_rust.ps1
#
# 与 Python 版（build_installer.ps1）的区别：**安装包里没有 Python 运行时**。
# 携带的是：Rust 宿主 exe + 前端静态资源 + 视觉 ONNX bundle + audiocpp(CPU ASR)
# + ffmpeg/ffprobe + adb（干净机器也能识别/出缩略图/同步）。
# 翻译运行时（vendor\llama，约 1.1GB）与 audiocpp GPU 版仍走界面下载。

param(
    [switch]$NoBuild,      # 跳过 cargo build（已有 release 产物时用）
    [switch]$NoCompile     # 只组装 dist，不调 ISCC
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$rust = Join-Path $root 'rust'
$dist = Join-Path $root 'artifacts\app-rust'
$installerDir = Join-Path $root 'artifacts\installer-rust'

Write-Host "[1/4] cargo build --release…" -ForegroundColor Cyan
if (-not $NoBuild) {
    Push-Location $rust
    try {
        # BLE 是可选 feature（默认关掉以免 CI 拉蓝牙栈），安装包必须带上，
        # 否则界面扫描设备会直接报"本构建未启用 BLE"。
        & cargo build -p nexus-host --release --features ble
        if ($LASTEXITCODE -ne 0) { throw "cargo build 失败（exit $LASTEXITCODE）" }
    } finally {
        Pop-Location
    }
}
$exe = Join-Path $rust 'target\release\nexus-host.exe'
if (-not (Test-Path $exe)) { throw "找不到 $exe" }

Write-Host "[2/4] 组装 $dist …" -ForegroundColor Cyan
if (Test-Path $dist) { Remove-Item $dist -Recurse -Force }
New-Item -ItemType Directory -Path $dist -Force | Out-Null

# 宿主 exe（安装后叫 FunScriptCast-Nexus.exe，与 Python 版同名，便于替换升级）
Copy-Item $exe (Join-Path $dist 'FunScriptCast-Nexus.exe') -Force
Copy-Item (Join-Path $root 'version.json') $dist -Force
if (Test-Path (Join-Path $root 'README.md')) { Copy-Item (Join-Path $root 'README.md') $dist -Force }

# 前端静态资源（宿主 fallback 到 ui-legacy）
Copy-Item (Join-Path $root 'ui-legacy') (Join-Path $dist 'ui-legacy') -Recurse -Force

# 视觉：只要 ONNX bundle 与配置；Python 实现不再随包
$visSrc = Join-Path $root 'vendor\vision'
$visDst = Join-Path $dist 'vendor\vision'
New-Item -ItemType Directory -Path $visDst -Force | Out-Null
if (Test-Path (Join-Path $visSrc 'bundles')) {
    Copy-Item (Join-Path $visSrc 'bundles') (Join-Path $visDst 'bundles') -Recurse -Force
} else {
    Write-Warning '  vendor\vision\bundles 不存在：安装包将无法做视觉识别'
}
if (Test-Path (Join-Path $visSrc 'config.json')) {
    Copy-Item (Join-Path $visSrc 'config.json') $visDst -Force
}

# 字幕：CPU + Vulkan 识别运行时自带（CUDA 改为界面可选下载，不进包）
$audioSrc = Join-Path $root 'vendor\audiocpp'
$audioDst = Join-Path $dist 'vendor\audiocpp'
New-Item -ItemType Directory -Path $audioDst -Force | Out-Null
foreach ($part in @('cpu', 'vulkan')) {
    if (Test-Path (Join-Path $audioSrc $part)) {
        Copy-Item (Join-Path $audioSrc $part) (Join-Path $audioDst $part) -Recurse -Force
    } else {
        throw "vendor\audiocpp\$part 不存在：Vulkan/CPU 识别运行时是安装包必需项"
    }
}
if (Test-Path (Join-Path $audioSrc 'LICENSE')) { Copy-Item (Join-Path $audioSrc 'LICENSE') $audioDst -Force }
# silero VAD 模型（audiocpp_cli --task vad 用）：不进包则 VAD 静默降级、
# 轻声内容救不回（2026-10-27 打包发现）
if (Test-Path (Join-Path $audioSrc 'assets')) {
    Copy-Item (Join-Path $audioSrc 'assets') (Join-Path $audioDst 'assets') -Recurse -Force
} else {
    throw 'vendorudiocppssets 不存在：silero VAD 模型是安装包必需项'
}

# 翻译运行时：随包 Vulkan（跨厂商）；CUDA 改为界面可选下载。
$llamaSrc = Join-Path $root 'vendor\llama-vulkan'
$llamaDst = Join-Path $dist 'vendor\llama-vulkan'
if (Test-Path $llamaSrc) {
    Copy-Item $llamaSrc $llamaDst -Recurse -Force
} else {
    throw 'vendor\llama-vulkan 不存在：Vulkan 翻译运行时是安装包必需项'
}

# ONNX Runtime 运行库（视觉推理必需）：从 .venv 的 onnxruntime 目录取，
# 装到 runtime\ort\，宿主启动时会把 ORT_DYLIB_PATH 指过去。
$ortSrc = Join-Path $root '.venv\Lib\site-packages\onnxruntime\capi'
$ortDst = Join-Path $dist 'runtime\ort'
if (Test-Path (Join-Path $ortSrc 'onnxruntime.dll')) {
    New-Item -ItemType Directory -Path $ortDst -Force | Out-Null
    foreach ($dll in @('onnxruntime.dll', 'DirectML.dll', 'onnxruntime_providers_shared.dll')) {
        $src = Join-Path $ortSrc $dll
        if (Test-Path $src) { Copy-Item $src $ortDst -Force }
    }
} else {
    # 与 audiocpp/llama-vulkan 同一处置：缺失即中止。静默继续会产出
    # 无视觉推理能力的缺陷安装包，问题要到用户机器上才暴露。
    throw ('找不到 onnxruntime.dll（期望位置 ' + $ortSrc + '）：视觉推理是安装包必需项')
}

# 字幕配置种子（首次安装铺到 var\data，之后不再动）
$subCfg = Join-Path $root 'vendor\subtitle\config.json'
if (Test-Path $subCfg) {
    Copy-Item $subCfg (Join-Path $dist 'subtitle_config.seed.json') -Force
}

# 外部工具：ffmpeg/ffprobe 放宿主 exe 同目录（Windows 的应用目录优先于 PATH），
# adb 放 tools\adb（extras.rs 的 adb_exe 优先查这里）。
$ffmpegSrc = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
$ffprobeSrc = (Get-Command ffprobe -ErrorAction SilentlyContinue).Source
if (-not $ffmpegSrc -or -not $ffprobeSrc) {
    throw '找不到 ffmpeg/ffprobe：AI 识别与媒体库缩略图依赖它们，请先安装后重试'
}
Copy-Item $ffmpegSrc (Join-Path $dist 'ffmpeg.exe') -Force
Copy-Item $ffprobeSrc (Join-Path $dist 'ffprobe.exe') -Force
$adbSrc = Join-Path $root 'tools\adb'
if (-not (Test-Path (Join-Path $adbSrc 'adb.exe'))) {
    throw '找不到 tools\adb\adb.exe：设备扫描/同步会不可用'
}
$adbDst = Join-Path $dist 'tools\adb'
New-Item -ItemType Directory -Path $adbDst -Force | Out-Null
Copy-Item (Join-Path $adbSrc '*') $adbDst -Recurse -Force

$size = (Get-ChildItem $dist -Recurse -File | Measure-Object -Property Length -Sum).Sum
Write-Host ("  组装完成：{0:N1} MB" -f ($size / 1MB)) -ForegroundColor DarkGray

Write-Host "[3/4] 定位 Inno Setup…" -ForegroundColor Cyan
$iscc = $env:NEXUS_ISCC
if (-not $iscc -or -not (Test-Path $iscc)) {
    $pf86 = [Environment]::GetFolderPath('ProgramFilesX86')
    $cands = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path $pf86 'Inno Setup 6\ISCC.exe'),
        (Join-Path $env:ProgramFiles 'Inno Setup 6\ISCC.exe')
    )
    foreach ($c in $cands) {
        if ($c -and (Test-Path $c)) { $iscc = $c; break }
    }
}
if (-not $iscc) { throw '找不到 ISCC.exe（装 Inno Setup 6，或设 NEXUS_ISCC）' }

$version = (Get-Content (Join-Path $root 'version.json') -Raw | ConvertFrom-Json).versionName

if ($NoCompile) {
    Write-Host "[4/4] 跳过编译（-NoCompile）" -ForegroundColor Yellow
    return
}

Write-Host "[4/4] 编译安装包…" -ForegroundColor Cyan
New-Item -ItemType Directory -Path $installerDir -Force | Out-Null
& $iscc "/DMyAppVersion=$version" "/DStageDir=$dist" (Join-Path $PSScriptRoot 'installer\nexus-rust.iss')
if ($LASTEXITCODE -ne 0) { throw "ISCC 失败（exit $LASTEXITCODE）" }

Get-ChildItem $installerDir -Filter *.exe | ForEach-Object {
    Write-Host ("[完成] " + $_.FullName + ("  ({0:N1} MB)" -f ($_.Length / 1MB))) -ForegroundColor Green
}