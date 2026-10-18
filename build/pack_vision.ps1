# 打包 AI 实时识别模块为可独立分发的 zip（GitHub Release 资产）
#
#   powershell -ExecutionPolicy Bypass -File build\pack_vision.ps1
#   powershell -ExecutionPolicy Bypass -File build\pack_vision.ps1 -NoWeights
#
# 产物：dist-modules\vision-<版本>.zip
#   vendor/vision/{run_server.py,service.py,stream_engine.py,goblintrain/,
#                  weights/checkpoints/v0.6.0.pt,config.json,requirements.txt,README.md}
#
# 用途：安装版用户（%LOCALAPPDATA% 安装目录）若想单独更新/补装识别模块，
# 下载这个 zip 解压到 <安装目录>\vendor\vision 即可，不必重装整个程序。
# 宿主「识别与翻译」卡也用它作为在线更新源。
#
# ⚠ 不含：cache\（转码与 latent 缓存，运行时重建）、__pycache__、*.log。
# ⚠ 不含 V-JEPA 编码器（1.66GB，走 tools\fetch_vision.ps1 或首次运行时自动下载）。
param([switch]$NoWeights)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root 'vendor\vision'
if (-not (Test-Path $src)) { throw "识别模块不存在：$src" }

$ver = '0.0.0'
$vj = Join-Path $root 'version.json'
if (Test-Path $vj) {
    try { $ver = [string]((Get-Content $vj -Raw -Encoding UTF8 | ConvertFrom-Json).versionName) } catch { }
}
if (-not $ver) { $ver = '0.0.0' }

$outDir = Join-Path $root 'dist-modules'
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$zip = Join-Path $outDir ("vision-$ver.zip")
$stage = Join-Path $env:TEMP ("vision-pack-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
$stageMod = Join-Path $stage 'vision'

try {
    New-Item -ItemType Directory -Force -Path $stageMod | Out-Null
    # 只挑要发的文件：显式白名单比"全拷再删"可靠（不会漏删 cache）
    foreach ($f in 'run_server.py', 'service.py', 'stream_engine.py',
                   'config.json', 'requirements.txt', 'README.md',
                   'LICENSE.goblintrain.txt') {
        $p = Join-Path $src $f
        if (Test-Path $p) { Copy-Item $p $stageMod -Force }
    }
    $gt = Join-Path $src 'goblintrain'
    if (Test-Path $gt) {
        Copy-Item $gt (Join-Path $stageMod 'goblintrain') -Recurse -Force
        Get-ChildItem (Join-Path $stageMod 'goblintrain') -Recurse -Force -Directory -Filter '__pycache__' |
            Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (-not $NoWeights) {
        $w = Join-Path $src 'weights'
        if (-not (Test-Path (Join-Path $w 'checkpoints\v0.6.0.pt'))) {
            throw "缺少权重 weights\checkpoints\v0.6.0.pt，先跑 tools\fetch_vision.ps1（或加 -NoWeights）"
        }
        Copy-Item $w (Join-Path $stageMod 'weights') -Recurse -Force
    }

    if (Test-Path $zip) { Remove-Item $zip -Force }
    Compress-Archive -Path $stageMod -DestinationPath $zip -CompressionLevel Optimal
    $mb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
    $sha = (Get-FileHash -Algorithm SHA256 $zip).Hash.ToLower()
    Write-Host "[vision-pack] $zip  ($mb MB)" -ForegroundColor Green
    Write-Host "[vision-pack] SHA256 $sha"
    Write-Host "[vision-pack] 上传到 GitHub Release 的 runtime-assets 页："
    Write-Host "  gh release upload runtime-assets `"$zip`" --clobber"
} finally {
    Remove-Item $stage -Recurse -Force -ErrorAction SilentlyContinue
}
