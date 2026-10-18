# 拉取 AI 实时识别模块的模型权重（构建机 / 首次安装用）
#
#   powershell -ExecutionPolicy Bypass -File tools\fetch_vision.ps1
#   powershell -ExecutionPolicy Bypass -File tools\fetch_vision.ps1 -Force
#
# 拉两样东西：
#   1. 官方 GoblinScript v0.6.0 checkpoint（MIT，约 17MB）→ vendor\vision\weights\checkpoints
#   2. V-JEPA 2.1 ViT-B 编码器（Meta，MIT，约 1.66GB）→ torch.hub 缓存（全机共享，不重复下载）
#
# 注意：一律用 curl.exe 下载。urllib / torch.hub 在这台机器上会**静默截断**大文件，
# 而 torch.hub 只查文件存在、不校验完整性，坏文件会被永久复用（本机踩过两次）。

param([switch]$Force)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$mod = Join-Path $root "vendor\vision"
$ckptDir = Join-Path $mod "weights\checkpoints"
$ckpt = Join-Path $ckptDir "v0.6.0.pt"
$ckptUrl = "https://github.com/ofs69/GoblinTrainingGrounds/releases/download/weights-1/v0.6.0.pt"
$ckptSha = "5e26f2012bf17691dbc0485b874e8adfccba44340b71cb5ca24549bc63d6c74b"

New-Item -ItemType Directory -Force -Path $ckptDir | Out-Null

function Get-Sha256($path) {
    if (-not (Test-Path $path)) { return "" }
    return (Get-FileHash -Algorithm SHA256 $path).Hash.ToLower()
}

# ---- 1. checkpoint ----
if ($Force -or (Get-Sha256 $ckpt) -ne $ckptSha) {
    Write-Host "[vision] 下载 checkpoint v0.6.0.pt (~17MB)..."
    & curl.exe -L --retry 3 --fail -o "$ckpt.part" $ckptUrl
    if ($LASTEXITCODE -ne 0) { throw "checkpoint 下载失败 (curl exit $LASTEXITCODE)" }
    Move-Item -Force "$ckpt.part" $ckpt
    $got = Get-Sha256 $ckpt
    if ($got -ne $ckptSha) { throw "checkpoint 校验失败: $got != $ckptSha" }
    Write-Host "[vision] checkpoint OK"
} else {
    Write-Host "[vision] checkpoint 已存在且校验通过，跳过"
}

# ---- 2. V-JEPA 编码器 ----
$hubDir = Join-Path $env:USERPROFILE ".cache\torch\hub\checkpoints"
$enc = Join-Path $hubDir "vjepa2_1_vitb_dist_vitG_384.pt"
$encUrl = "https://dl.fbaipublicfiles.com/vjepa2/vjepa2_1_vitb_dist_vitG_384.pt"
New-Item -ItemType Directory -Force -Path $hubDir | Out-Null

# torch.hub 的坏文件特征: 大小明显小于官方(1.66GB)。用大小兜底 + torch.load 探测。
$needEnc = $true
if ((Test-Path $enc) -and -not $Force) {
    $sz = (Get-Item $enc).Length
    if ($sz -gt 1600000000) {
        try {
            $py = Join-Path $root ".venv\Scripts\python.exe"
            if (Test-Path $py) {
                & $py -c "import torch,sys; torch.load(sys.argv[1],map_location='cpu',weights_only=False)" $enc *> $null
                if ($LASTEXITCODE -eq 0) { $needEnc = $false }
            } else { $needEnc = $false }
        } catch { $needEnc = $true }
    }
}

if ($needEnc) {
    Write-Host "[vision] 下载 V-JEPA 2.1 ViT-B 编码器 (~1.66GB, 全机共享缓存)..."
    if (Test-Path $enc) { Remove-Item -Force $enc }   # 坏文件必须先删
    & curl.exe -L --retry 3 --fail -o "$enc.part" $encUrl
    if ($LASTEXITCODE -ne 0) { throw "编码器下载失败 (curl exit $LASTEXITCODE)" }
    Move-Item -Force "$enc.part" $enc
    $sz = (Get-Item $enc).Length
    if ($sz -lt 1600000000) { throw "编码器大小异常: $sz bytes (下载被截断?)" }
    Write-Host "[vision] 编码器 OK ($([math]::Round($sz/1GB,2)) GB)"
} else {
    Write-Host "[vision] 编码器已存在且可用，跳过"
}

Write-Host ""
Write-Host "[vision] 完成。启动服务:"
Write-Host "  .venv\Scripts\python.exe vendor\vision\run_server.py"
