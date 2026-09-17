# 仓库 → dist-app 同步工具（ui + vendor，递归比对）
#
#   powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1              # 同步 + 报告
#   powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1 -Restart     # 同步后重启字幕服务
#   powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1 -Check       # 只报告差异（有差异 exit 1，可做门禁）
#   powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1 -SyncConfig  # 连 config.json 一起覆盖（默认排除！）
#   powershell -ExecutionPolicy Bypass -File tools\sync_distapp.ps1 -SyncGlossary # 连术语表一起覆盖（默认排除！）
#
# 背景：Nexus 打包版（dist-app\FunScriptCast-Nexus.exe）拉起的服务跑 dist-app 里的
# 独立快照（ui\ 与 vendor\）——仓库改动不同步过去就完全无效
# （2026-09-16 一整轮修复因此"看起来无效"，见 README「⚠️ 运行形态」）。
#
# ⚠️ 运行数据默认**排除**：dist-app 侧不是"仓库的副本"，而是用户正在用的实例——
#    · config.json          UI 里填的云端 API Key / 本地调过的参数（仓库那份只是模板）
#    · glossary_*.json      用户在 UI 或 CSV 导入攒出来的术语表（几个月的成果）
#    · *.json.bak / .tmp    宿主的自保备份、写入途中的临时文件
#    用仓库模板覆盖 = 抹掉 key 和词库（与 build_exe 的坑 #12 同族）。
#    确要覆盖时显式给 -SyncConfig / -SyncGlossary。
param(
    [switch]$Check,
    [switch]$Restart,
    [switch]$ClearCache,
    [switch]$SyncConfig,
    [switch]$SyncGlossary
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$out = Join-Path $root 'dist-app'
if (-not (Test-Path $out)) { throw "打包目录不存在：$out（先运行 build\build_exe.ps1）" }

# 端口与宿主保持同源（host_server.py 读同名环境变量）
$subPort  = if ($env:FS_SUBTITLE_PORT) { [int]$env:FS_SUBTITLE_PORT } else { 8756 }
$hostPort = if ($env:FS_HOST_PORT)     { [int]$env:FS_HOST_PORT }     else { 8790 }

# 同步对（递归）。运行产物（logs / __pycache__ / *.log）双向忽略。
$pairs = @(
    @{ src = 'ui';              dst = 'ui' },
    @{ src = 'vendor\subtitle'; dst = 'vendor\subtitle' },
    @{ src = 'vendor\dlna';     dst = 'vendor\dlna' }
)
# 运行数据名单（见文件头 ⚠️ 说明）：术语表和 config.json 同等对待，
# 它们和 .py 混在同一个目录里，只保护 config.json 是 R41 的遗留漏洞。
$skipNames = @('config.json', 'glossary_ja_zh.json', 'glossary_en_zh.json')
if ($SyncConfig)   { $skipNames = @($skipNames | Where-Object { $_ -ne 'config.json' }) }
if ($SyncGlossary) { $skipNames = @($skipNames | Where-Object { $_ -notlike 'glossary_*' }) }
Write-Host ("[sync] 运行数据不参与比对/覆盖（要覆盖请显式给开关）：{0}" -f ($skipNames -join '、')) -ForegroundColor DarkGray

# 排除规则只此一份，src 与 dst **共用**：此前两侧各写一遍，dst 侧一旦漏了
# skipNames 就会把 dist-app 的运行配置当"仓库已删的多余文件"删掉（R41 差点出的事）。
# *.log 也在此统一（build_exe 清产物时删所有 *.log）：只排 logs 目录段的话，
# 仓库里被 .gitignore 忽略的 vendor\dlna\vr_dlna_access.log 会被当源码推回快照。
function Test-Excluded([string]$name, [string[]]$seg) {
    if ($seg -contains '__pycache__') { return $true }
    if ($seg -contains 'logs') { return $true }
    if ($name -like '*.log') { return $true }
    if ($skipNames -contains $name) { return $true }
    # 派生文件：宿主的 .bak 自保备份、save_glossary 写入途中的 .json.tmp
    #（同步删掉 .tmp 会撞上正在进行的保存，删掉 .bak 就是删用户的后悔药）
    if ($name -like '*.json.bak*' -or $name -like '*.json.tmp') { return $true }
    return $false
}

function Enum-Files($dir) {
    $base = (Get-Item (Join-Path $root $dir)).FullName
    Get-ChildItem $base -Recurse -File | Where-Object {
        $rel = $_.FullName.Substring($base.Length + 1)
        -not (Test-Excluded $_.Name ($rel -split '\\'))
    }
}

$totalChanged = 0
$missingDirs = @()
foreach ($p in $pairs) {
    $srcDir = Join-Path $root $p.src
    $dstDir = Join-Path $out $p.dst
    if (-not (Test-Path $dstDir)) {
        # 整目录缺失此前是"打红字 + continue"：$totalChanged 的累加在下面，计数保持 0，
        # 于是刚报完"缺失"就打印"快照与仓库一致"并 exit 0 —— 门禁假通过。
        Write-Host ("[{0}] 打包侧缺失：{1}（先跑 build\build_exe.ps1）" -f $p.src, $dstDir) -ForegroundColor Red
        $missingDirs += $p.src
        # 非 -Check 模式没什么可"报告差异"的：直接失败，别静默跳过这个目录对。
        if (-not $Check) { throw "[sync] 打包侧目录缺失：$dstDir（先跑 build\build_exe.ps1）" }
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
        # dst 侧套用**同一个** Test-Excluded（见它的注释）：漏掉运行数据会删用户的
        # config.json / 术语表，漏掉 *.log 会把日志当"快照多余"来回删。
        if (-not (Test-Excluded $_.Name ($rel -split '\\'))) { $dstMap[$rel] = $_ }
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
    # 整目录缺失也算失败：缺失目录没参与下面的计数，只看 $totalChanged 会假通过。
    if ($totalChanged -gt 0 -or $missingDirs.Count -gt 0) {
        if ($missingDirs.Count) {
            Write-Host ("[check] 打包侧整目录缺失：" + ($missingDirs -join '、')) -ForegroundColor Red
        }
        exit 1   # 有差异 → 非零退出，可做 CI/发布门禁
    }
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
