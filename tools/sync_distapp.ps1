# 仓库 vendor/subtitle → dist-app/vendor/subtitle 同步工具
#
#   powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1              # 同步 + 报告
#   powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1 -Restart     # 同步后重启字幕服务
#   powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1 -Check       # 只报告不同步
#
# 背景：Nexus 打包版（dist-app\FunScriptCast-Nexus.exe）拉起的字幕服务跑
# dist-app\vendor\subtitle 的独立快照——仓库改动不同步过去就完全无效
# （2026-09-16 一整轮修复因此"看起来无效"，见 README「⚠️ 运行形态」）。
param(
    [switch]$Check,      # 只报告差异，不复制
    [switch]$Restart,    # 同步后重启字幕服务（经宿主 API；宿主不在则用 .venv python 手动拉起）
    [switch]$ClearCache  # 同时清 dist-app\cache\subtitles\（改判据/策略后建议）
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$src = Join-Path $root 'vendor\subtitle'
$dst = Join-Path $root 'dist-app\vendor\subtitle'
if (-not (Test-Path $dst)) { throw "打包目录不存在：$dst（先运行 build\build_exe.ps1）" }

# ---- 比对（含"快照缺失文件"检测——新文件最容易被漏掉）----
$srcFiles = Get-ChildItem $src -File | Sort-Object Name
$changed = @()
$missing = @()
foreach ($f in $srcFiles) {
    $d = Join-Path $dst $f.Name
    if (-not (Test-Path $d)) { $missing += $f.Name; continue }
    if ((Get-FileHash $f.FullName -Algorithm MD5).Hash -ne (Get-FileHash $d -Algorithm MD5).Hash) {
        $changed += $f.Name
    }
}
$dstExtra = Get-ChildItem $dst -File | Where-Object { -not (Test-Path (Join-Path $src $_.Name)) } | Select-Object -ExpandProperty Name

Write-Host ("仓库文件 {0} 个：内容不同 {1}，快照缺失 {2}，快照多余 {3}" -f `
    $srcFiles.Count, $changed.Count, $missing.Count, $dstExtra.Count) -ForegroundColor Cyan
if ($changed)  { $changed  | ForEach-Object { Write-Host ("  不同: " + $_) } }
if ($missing)  { $missing  | ForEach-Object { Write-Host ("  缺失: " + $_) } }
if ($dstExtra) { $dstExtra | ForEach-Object { Write-Host ("  快照多余(仓库已删): " + $_) } }

if ($Check) { exit 0 }

# ---- 同步 ----
foreach ($f in $srcFiles) { Copy-Item $f.FullName (Join-Path $dst $f.Name) -Force }
foreach ($e in $dstExtra) { Remove-Item (Join-Path $dst $e) -Force }
Write-Host "[sync] 已同步 $($srcFiles.Count) 个文件" -ForegroundColor Green

if ($ClearCache) {
    $cc = Join-Path $root 'dist-app\cache\subtitles'
    if (Test-Path $cc) { Remove-Item (Join-Path $cc '*.json') -Force -ErrorAction SilentlyContinue }
    Write-Host "[sync] 已清 dist-app\cache\subtitles"
}

# ---- 重启字幕服务（先停旧实例；宿主 API 优先，宿主不在则借 .venv python 手动拉起）----
if ($Restart) {
    # 停旧实例：有上限轮询确认端口真的释放（不写固定 sleep 蒙）
    Get-NetTCPConnection -LocalPort 8756 -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-NetTCPConnection -LocalPort 8756 -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    if (Get-NetTCPConnection -LocalPort 8756 -State Listen -ErrorAction SilentlyContinue) {
        throw "[sync] :8756 在 30s 内没能停掉（可能被别的进程占用）"
    }

    # 优先让宿主拉起。实测宿主 API 在 127.0.0.1:8790；:8791 是 LAN 面，不带令牌会 403。
    $hostOk = $false
    try {
        Invoke-WebRequest -Uri 'http://127.0.0.1:8790/api/subtitle/start' -Method POST `
            -UseBasicParsing -TimeoutSec 10 | Out-Null
        $hostOk = $true
        Write-Host "[sync] 已请求宿主拉起字幕服务" -ForegroundColor Green
    } catch {
        Write-Host "[sync] 宿主不可达（Nexus 没开？），用 .venv python 手动拉起" -ForegroundColor Yellow
    }
    if (-not $hostOk) {
        Start-Process -FilePath (Join-Path $root '.venv\Scripts\python.exe') `
            -ArgumentList @('run_server.py', '--port', '8756') -WorkingDirectory $dst -WindowStyle Hidden
    }

    # 就绪检测：有上限轮询（不固定 sleep 硬等）
    $deadline = (Get-Date).AddSeconds(240)
    $h = $null
    while ((Get-Date) -lt $deadline) {
        try { $h = Invoke-RestMethod -Uri 'http://127.0.0.1:8756/health' -TimeoutSec 3; break }
        catch { Start-Sleep -Milliseconds 800 }
    }
    if (-not $h) { throw "[sync] 字幕服务 240s 内未就绪（用 .venv python 前台跑一次看报错）" }
    Write-Host ("[sync] 字幕服务就绪 pid={0} code_sig={1} 翻译后端={2}" -f `
        $h.pid, $h.code_sig, $h.translate_backend) -ForegroundColor Green
}
