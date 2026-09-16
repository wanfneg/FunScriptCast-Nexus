# 仓库 → dist-app 同步工具（ui + vendor，递归比对）
#
#   powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1              # 同步 + 报告
#   powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1 -Restart     # 同步后重启字幕服务
#   powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1 -Check       # 只报告差异（有差异 exit 1，可做门禁）
#   powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1 -SyncConfig  # 连 config.json 一起覆盖（默认排除！）
#
# 背景：Nexus 打包版（dist-app\FunScriptCast-Nexus.exe）拉起的服务跑 dist-app 里的
# 独立快照（ui\ 与 vendor\）——仓库改动不同步过去就完全无效
# （2026-09-16 一整轮修复因此"看起来无效"，见 README「⚠️ 运行形态」）。
#
# ⚠️ config.json 默认**排除**：dist-app 运行配置里存着 UI 填的云端 API Key 和
#    本地调过的参数，仓库那份只是模板——用模板覆盖运行配置会把 key 抹掉
#    （与 build_exe 的坑 #12 同族）。确要覆盖时显式给 -SyncConfig。
param(
    [switch]$Check,
    [switch]$Restart,
    [switch]$ClearCache,
    [switch]$SyncConfig
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root 'dist-app'
if (-not (Test-Path $out)) { throw "打包目录不存在：$out（先运行 build\build_exe.ps1）" }

# 端口与宿主保持同源（host_server.py 读同名环境变量）
$subPort  = if ($env:FS_SUBTITLE_PORT) { [int]$env:FS_SUBTITLE_PORT } else { 8756 }
$hostPort = if ($env:FS_HOST_PORT)     { [int]$env:FS_HOST_PORT }     else { 8790 }

# 同步对（递归）。运行产物（logs / __pycache__）双向忽略。
$pairs = @(
    @{ src = 'ui';              dst = 'ui' },
    @{ src = 'vendor\subtitle'; dst = 'vendor\subtitle' },
    @{ src = 'vendor\dlna';     dst = 'vendor\dlna' }
)
$skipNames = @('config.json')
if ($SyncConfig) { $skipNames = @() }

function Enum-Files($dir) {
    $base = (Get-Item (Join-Path $root $dir)).FullName
    Get-ChildItem $base -Recurse -File | Where-Object {
        $rel = $_.FullName.Substring($base.Length + 1)
        $seg = $rel -split '\\'
        -not ($seg -contains '__pycache__') -and
        -not ($seg -contains 'logs') -and
        -not ($skipNames -contains $_.Name)
    }
}

$totalChanged = 0
foreach ($p in $pairs) {
    $srcDir = Join-Path $root $p.src
    $dstDir = Join-Path $out $p.dst
    if (-not (Test-Path $dstDir)) {
        Write-Host ("[{0}] 打包侧缺失：{1}（先跑 build\build_exe.ps1）" -f $p.src, $dstDir) -ForegroundColor Red
        continue
    }
    $srcMap = @{}
    Enum-Files $p.src | ForEach-Object {
        $rel = $_.FullName.Substring((Get-Item $srcDir).FullName.Length + 1)
        $srcMap[$rel] = $_
    }
    $dstMap = @{}
    Get-ChildItem $dstDir -Recurse -File | ForEach-Object {
        $rel = $_.FullName.Substring((Get-Item $dstDir).FullName.Length + 1)
        $seg = $rel -split '\\'
        # dst 侧必须套用同一套排除规则：漏了 skipNames 会把 dist-app 的
        # config.json 当成"仓库已删的多余文件"删掉——那是要出事的。
        if (-not ($seg -contains '__pycache__') -and -not ($seg -contains 'logs') -and
                -not ($skipNames -contains $_.Name)) { $dstMap[$rel] = $_ }
    }

    $changed = @(); $missing = @()
    foreach ($rel in $srcMap.Keys) {
        if (-not $dstMap.ContainsKey($rel)) { $missing += $rel; continue }
        if ((Get-FileHash $srcMap[$rel].FullName -Algorithm MD5).Hash -ne (Get-FileHash $dstMap[$rel].FullName -Algorithm MD5).Hash) {
            $changed += $rel
        }
    }
    $extra = @($dstMap.Keys | Where-Object { -not $srcMap.ContainsKey($_) })

    Write-Host ("[{0}] 仓库 {1} 个文件：内容不同 {2}，快照缺失 {3}，快照多余 {4}" -f `
        $p.src, $srcMap.Count, $changed.Count, $missing.Count, $extra.Count) -ForegroundColor Cyan
    foreach ($c in $changed) { Write-Host ("  不同: " + $c) }
    foreach ($m in $missing) { Write-Host ("  缺失: " + $m) }
    foreach ($e in $extra)   { Write-Host ("  快照多余(仓库已删): " + $e) -ForegroundColor Yellow }

    if ($Check) { $totalChanged += $changed.Count + $missing.Count + $extra.Count; continue }

    foreach ($rel in $changed + $missing) {
        $target = Join-Path $dstDir $rel
        $targetDir = Split-Path -Parent $target
        if (-not (Test-Path $targetDir)) { New-Item -ItemType Directory -Path $targetDir -Force | Out-Null }
        Copy-Item $srcMap[$rel].FullName $target -Force
    }
    foreach ($rel in $extra) { Remove-Item (Join-Path $dstDir $rel) -Force }
    if ($changed.Count + $missing.Count + $extra.Count -gt 0) {
        Write-Host ("[sync] {0}：已同步 {1} 处" -f $p.src, ($changed.Count + $missing.Count + $extra.Count)) -ForegroundColor Green
    }
}

if ($Check) {
    if ($totalChanged -gt 0) { exit 1 }   # 有差异 → 非零退出，可做 CI/发布门禁
    Write-Host "[check] 快照与仓库一致" -ForegroundColor Green
    exit 0
}

if ($ClearCache) {
    $cc = Join-Path $out 'cache\subtitles'
    if (Test-Path $cc) { Remove-Item (Join-Path $cc '*.json') -Force -ErrorAction SilentlyContinue }
    Write-Host "[sync] 已清 dist-app\cache\subtitles"
}

# ---- 重启字幕服务（先停旧实例；宿主 API 优先，宿主不在则借 .venv python 手动拉起）----
if ($Restart) {
    # 停旧实例：有上限轮询确认端口真的释放（不写固定 sleep 蒙）
    Get-NetTCPConnection -LocalPort $subPort -State Listen -ErrorAction SilentlyContinue |
        ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
    $deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $deadline) {
        if (-not (Get-NetTCPConnection -LocalPort $subPort -State Listen -ErrorAction SilentlyContinue)) { break }
        Start-Sleep -Milliseconds 500
    }
    if (Get-NetTCPConnection -LocalPort $subPort -State Listen -ErrorAction SilentlyContinue) {
        throw "[sync] :$subPort 在 30s 内没能停掉（可能被别的进程占用）"
    }

    # 优先让宿主拉起。实测宿主 API 在 127.0.0.1:8790；:8791 是 LAN 面，不带令牌会 403。
    $hostOk = $false
    try {
        Invoke-WebRequest -Uri "http://127.0.0.1:$hostPort/api/subtitle/start" -Method POST `
            -UseBasicParsing -TimeoutSec 10 | Out-Null
        $hostOk = $true
        Write-Host "[sync] 已请求宿主拉起字幕服务" -ForegroundColor Green
    } catch {
        Write-Host "[sync] 宿主不可达（Nexus 没开？），用 .venv python 手动拉起" -ForegroundColor Yellow
    }
    if (-not $hostOk) {
        Start-Process -FilePath (Join-Path $root '.venv\Scripts\python.exe') `
            -ArgumentList @('run_server.py', '--port', "$subPort") -WorkingDirectory (Join-Path $out 'vendor\subtitle') -WindowStyle Hidden
    }

    # 就绪检测：有上限轮询（不固定 sleep 硬等）
    $deadline = (Get-Date).AddSeconds(240)
    $h = $null
    while ((Get-Date) -lt $deadline) {
        try { $h = Invoke-RestMethod -Uri "http://127.0.0.1:$subPort/health" -TimeoutSec 3; break }
        catch { Start-Sleep -Milliseconds 800 }
    }
    if (-not $h) { throw "[sync] 字幕服务 240s 内未就绪（用 .venv python 前台跑一次看报错）" }
    Write-Host ("[sync] 字幕服务就绪 pid={0} code_sig={1} 翻译后端={2}" -f `
        $h.pid, $h.code_sig, $h.translate_backend) -ForegroundColor Green
}
