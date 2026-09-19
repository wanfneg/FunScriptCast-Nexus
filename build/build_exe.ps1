# 构建 FunScriptCast-Nexus 单文件 EXE，并组装 dist-app/（EXE + ui + vendor）
#
#   powershell -ExecutionPolicy Bypass -File build\build_exe.ps1
#   powershell -ExecutionPolicy Bypass -File build\build_exe.ps1 -NoBump   # 不递增版本
#
# 产物：
#   dist-app\FunScriptCast-Nexus.exe    应用本体（含 pywebview / DLNA 模块）
#   dist-app\ui\                        前端静态资源（外置，改完即生效）
#   dist-app\vendor\                    DLNA / 字幕服务源码（字幕服务仍用 .venv 跑）
#   dist-app\start.bat                  备用启动器（源码模式）
#
# 注意：models\ 与 .venv\ 不进产物，运行时按 EXE 所在目录查找。

param([switch]$NoBump)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { throw "找不到 venv Python：$py" }

Write-Host "[1/4] PyInstaller 打包…" -ForegroundColor Cyan
& $py -m PyInstaller (Join-Path $PSScriptRoot 'nexus.spec') --noconfirm --clean --distpath (Join-Path $root 'dist') --workpath (Join-Path $root 'build\work')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（exit $LASTEXITCODE）" }

$exe = Join-Path $root 'dist\FunScriptCast-Nexus.exe'
if (-not (Test-Path $exe)) { throw "没有产出 EXE：$exe" }

# 版本号递增必须排在 EXE 产物校验**之后**：此前它在 PyInstaller 之前跑，打包失败
# 也白吃一个版本号，还让仓库 version.json 与 dist-app\version.json（安装包版本来源）
# 偏离、git 工作区平白变脏。version.json 是运行时读取的外置文件，不进 EXE 包。
if (-not $NoBump) {
    Write-Host "[1.5/4] 递增版本号（EXE 已产出才消耗版本号）…" -ForegroundColor Cyan
    & powershell -ExecutionPolicy Bypass -File (Join-Path $root 'tools\bump_version.ps1')
    if ($LASTEXITCODE -ne 0) { throw "版本号递增失败" }
}

Write-Host "[2/4] 组装 dist-app…" -ForegroundColor Cyan
$out = Join-Path $root 'dist-app'
# 组装前必须停掉字幕服务：它就从 dist-app\vendor\subtitle 运行，
# 进程不死会导致删除/覆盖不完整，产出残缺目录（2026-09-16 实测踩坑）
# 端口与宿主/同步工具同源：host_server.py 与 sync_distapp.ps1 都读 FS_SUBTITLE_PORT。
# 硬编码 8756 时，用户设过该变量就停不到真正的字幕服务，而它整进程持有
# vendor\subtitle\logs\run_server.log → 删不掉 dist-app → 落到下面的覆盖回退分支。
$subPort = if ($env:FS_SUBTITLE_PORT) { [int]$env:FS_SUBTITLE_PORT } else { 8756 }
Get-NetTCPConnection -LocalPort $subPort -State Listen -ErrorAction SilentlyContinue |     ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
# 应用本体也锁 exe：一并停止（构建完成后由安装/用户重新启动）
Get-Process -Name 'FunScriptCast-Nexus' -ErrorAction SilentlyContinue |     Stop-Process -Force -ErrorAction SilentlyContinue
# 有上限轮询确认真的退出（原来只 sleep 2 秒靠蒙）：进程没死透就删不掉 dist-app，
# 一旦落到覆盖分支就可能产出残缺目录，所以这里宁可失败也不带病往下走。
$deadline = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $deadline) {
    if (-not (Get-NetTCPConnection -LocalPort $subPort -State Listen -ErrorAction SilentlyContinue) -and
        -not (Get-Process -Name 'FunScriptCast-Nexus' -ErrorAction SilentlyContinue)) { break }
    Start-Sleep -Milliseconds 500
}
$stillListen = Get-NetTCPConnection -LocalPort $subPort -State Listen -ErrorAction SilentlyContinue
if ($stillListen) {
    throw ("端口 {0} 仍有进程监听（PID {1}）——字幕服务没停掉，它占着 dist-app\vendor\subtitle\logs\run_server.log，" +
           "dist-app 删不干净。先手工结束它再重编。") -f $subPort, (($stillListen.OwningProcess | Select-Object -Unique) -join ',')
}
$stillExe = Get-Process -Name 'FunScriptCast-Nexus' -ErrorAction SilentlyContinue
if ($stillExe) {
    throw ("FunScriptCast-Nexus 进程仍在（PID {0}），它锁着 dist-app 里的 EXE，先关掉程序再重编。") -f (($stillExe.Id) -join ',')
}
# 运行配置里用户在 UI 填的云端 key 存在 dist-app 的 config.json（坑 #12 的根源）。
# 覆盖 vendor 前先摘出来，组装完再回填——重编不再丢 key。
$subtitleDst = Join-Path $out 'vendor\subtitle'
$distCfgPath = Join-Path $subtitleDst 'config.json'
$preservedKey = ''
if (Test-Path $distCfgPath) {
    try { $preservedKey = [string]((Get-Content $distCfgPath -Raw -Encoding UTF8 | ConvertFrom-Json).translate.openai.api_key) } catch { }
}
$cleanRemoved = -not (Test-Path $out)   # 目录本来就不存在 = 谈不上"清理失败"
# R49 起用户数据住 `dist-app\data\`（配置 / 宿主设置 / DLNA 数据，见
# vendor\subtitle\user_paths.py）。$out 是**整目录删除**后重建的，不先摘出来就每次重编
# 清空一遍——比坑 #12 更狠：那次只丢云端 key，这次连 DLNA 共享目录一起没。
# 用"整目录搬走再搬回"而不是逐文件读字节：这里可能有宿主的 .bak 自保备份与 .tmp，
# 逐文件搬运会漏掉它们，而漏掉的正是用户的后悔药。
$dataStash = Join-Path $root 'build\_dist-app-data'
$distDataDir = Join-Path $out 'data'
# 上一次重编中途失败会留下暂存（此时 dist-app\data 不在原位）：先回填，别让数据卡在暂存里
if ((Test-Path $dataStash) -and -not (Test-Path $distDataDir)) {
    New-Item -ItemType Directory -Path $out -Force | Out-Null
    Move-Item $dataStash $distDataDir -Force
    Write-Host "  发现上次重编遗留的数据暂存，已先回填 dist-app\data" -ForegroundColor Yellow
}
$stashedData = $false
if (Test-Path $distDataDir) {
    Remove-Item $dataStash -Recurse -Force -ErrorAction SilentlyContinue
    Move-Item $distDataDir $dataStash -Force
    $stashedData = $true
    $nData = (Get-ChildItem $dataStash -Recurse -File -Force | Measure-Object).Count
    Write-Host "  已摘出 dist-app\data（运行数据 $nData 个文件），组装完回填" -ForegroundColor DarkGray
}
# dist-app\vendor\llama（llama.cpp + CUDA，约 1.1 GB）与 data 同理：它不进安装包
# （v1.0.18 起改为界面内下载），但**日常重编的 dist-app 就是用户的运行实例**——
# 不摘出来，每次重编都会吃掉它；下一次字幕服务启动时"找不到 llama-server.exe"，
# 本地翻译静默失效（预热只打一行日志，界面上毫无感知，R55 实测踩中）。
$llamaStash = Join-Path $root 'build\_dist-app-llama'
$distLlamaDir = Join-Path $out 'vendor\llama'
if ((Test-Path $llamaStash) -and -not (Test-Path $distLlamaDir)) {
    Move-Item $llamaStash $distLlamaDir -Force
    Write-Host "  发现上次重编遗留的 llama 暂存，已先回填 dist-app\vendor\llama" -ForegroundColor Yellow
}
$stashedLlama = $false
if (Test-Path $distLlamaDir) {
    Remove-Item $llamaStash -Recurse -Force -ErrorAction SilentlyContinue
    Move-Item $distLlamaDir $llamaStash -Force
    $stashedLlama = $true
    Write-Host "  已摘出 dist-app\vendor\llama（本地翻译运行时），组装完回填" -ForegroundColor DarkGray
}
if (Test-Path $out) {
    # 先摘除 junction（只删链接点本身）。PS5.1 的 Remove-Item -Recurse 会**跟随
    # junction 递归删除目标内容**（PowerShell#621）：dist-app 里的 models/.venv
    # 是指向仓库真身的链接，直接递归删 = 删掉 8GB 模型 / 整个 .venv。
    Get-ChildItem $out -Force -Directory -ErrorAction SilentlyContinue |
        Where-Object { $_.LinkType } |
        ForEach-Object {
            Write-Host "  摘除链接点 $($_.Name)（保护目标内容不被递归删除）" -ForegroundColor DarkGray
            cmd /c rmdir "$($_.FullName)" 2>$null | Out-Null
        }
    try {
        Remove-Item $out -Recurse -Force -ErrorAction Stop
        $cleanRemoved = $true
    } catch {
        # 目录可能被资源管理器/杀软/上次运行的进程占用，删不掉就原地覆盖
        Write-Host "  dist-app 无法删除（被占用），改为原地覆盖写入" -ForegroundColor Yellow
    }
}
New-Item -ItemType Directory -Path $out -Force | Out-Null
Copy-Item $exe $out -Force
foreach ($d in 'ui', 'vendor', 'tools') {
    $srcDir = Join-Path $root $d
    $dstDir = Join-Path $out $d
    if ($cleanRemoved) {
        Copy-Item $srcDir $dstDir -Recurse -Force
    } else {
        # 回退分支**只能复制内容，不能复制目录本身**：目标目录已存在时
        # `Copy-Item <源目录> <已存在目录> -Recurse` 是"放进容器"语义，会造出
        # vendor\vendor\subtitle 这种嵌套（PS5.1 已用 -WhatIf 实证），而删了一半的
        # vendor\subtitle 又缺文件——EXE 跑到旧代码或找不到自己的 vendor。
        # 用 Get-ChildItem -Force 逐项复制（通配符 '*' 漏隐藏文件）。
        if (-not (Test-Path $dstDir)) { New-Item -ItemType Directory -Path $dstDir -Force | Out-Null }
        Get-ChildItem -LiteralPath $srcDir -Force | ForEach-Object {
            Copy-Item -LiteralPath $_.FullName -Destination $dstDir -Recurse -Force
        }
    }
}
# 布局自检：一旦出现嵌套就立刻失败，不要让它走到"完成"并被打进安装包。
if (Test-Path (Join-Path $out 'vendor\vendor')) {
    throw "组装异常：$out\vendor\vendor 是嵌套目录（覆盖回退写坏），请手工删掉 dist-app 后重跑本脚本"
}
foreach ($f in 'version.json', 'start.bat', 'README.md') {
    Copy-Item (Join-Path $root $f) $out -Force
}

# 本地翻译运行时（llama.cpp + CUDA 库，约 1.1GB）不进产物：改为「识别与翻译」卡
# 的一键下载项（host_server MODELS_CATALOG → llama-runtime）。设备同步脚本
# toolsetch_llama.ps1 仍是构建机侧的安装方式。
$distLlama = Join-Path $out 'vendor\llama'
if (Test-Path $distLlama) {
    Remove-Item $distLlama -Recurse -Force -ErrorAction SilentlyContinue
    Write-Host "  已剔除 vendor\llama（改为界面内下载，安装包瘦身约 300MB）" -ForegroundColor DarkGray
}

# 清理不该进产物/安装包的东西：运行日志、__pycache__（仓库里可能残存，Copy 不看 .gitignore）
Get-ChildItem (Join-Path $out 'vendor'), (Join-Path $out 'ui'), (Join-Path $out 'tools') -Recurse -Force -ErrorAction SilentlyContinue |
    Where-Object { -not $_.PSIsContainer -and $_.Name -like '*.log' } |
    Remove-Item -Force -ErrorAction SilentlyContinue
Get-ChildItem $out -Recurse -Force -Directory -Filter '__pycache__' -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

# 重建 models/.venv junction（坑 #6 的脚本级修复）：此前重编会删掉链接、
# 收尾只打印一句"记得复制"，忘了就是"重编后 ASR 找不到模型"。
foreach ($name in 'models', '.venv') {
    $srcP = Join-Path $root $name
    $dstP = Join-Path $out $name
    if ((Test-Path $srcP) -and -not (Test-Path $dstP)) {
        cmd /c mklink "/J" "$dstP" "$srcP" | Out-Null
        if (Test-Path $dstP) { Write-Host "  已重建 junction $name → $srcP" -ForegroundColor DarkGray }
    }
}

# 回填 dist-app\data（见上面"摘出"处的说明：用户的配置 / 设置 / DLNA 数据）
if ($stashedData) {
    $distDataDir = Join-Path $out 'data'
    if (Test-Path $distDataDir) { Remove-Item $distDataDir -Recurse -Force -ErrorAction SilentlyContinue }
    Move-Item $dataStash $distDataDir -Force
    Write-Host "  已把 dist-app\data 回填（用户的配置 / 设置 / DLNA 数据）" -ForegroundColor DarkGray
}
# 回填 dist-app\vendor\llama（本地翻译运行时，见"摘出"处的说明）
if ($stashedLlama) {
    $distLlamaDir = Join-Path $out 'vendor\llama'
    if (Test-Path $distLlamaDir) { Remove-Item $distLlamaDir -Recurse -Force -ErrorAction SilentlyContinue }
    Move-Item $llamaStash $distLlamaDir -Force
    Write-Host "  已把 dist-app\vendor\llama 回填（本地翻译运行时）" -ForegroundColor DarkGray
}

# 回填用户 key（坑 #12 就此关闭）。注意 PS5.1 的 UTF8 必须无 BOM——
# Python 端 read_text(encoding='utf-8') 遇到 BOM 会直接 JSON 解析失败。
if ($preservedKey -and (Test-Path $distCfgPath)) {
    try {
        $cfg = Get-Content $distCfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
        if (-not [string]$cfg.translate.openai.api_key) {
            $cfg.translate.openai.api_key = $preservedKey
            [IO.File]::WriteAllText($distCfgPath, ($cfg | ConvertTo-Json -Depth 24),
                (New-Object System.Text.UTF8Encoding($false)))
            Write-Host "  已把 dist-app 原有的云端 key 回填到新配置" -ForegroundColor DarkGray
        }
    } catch {
        Write-Host "  key 回填失败（$($_.Exception.Message)），请到界面重新填一次" -ForegroundColor Yellow
    }
}

Write-Host "[3/4] 清理临时目录…" -ForegroundColor Cyan
Remove-Item (Join-Path $root 'build\work') -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $root 'dist') -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "[4/4] 完成。" -ForegroundColor Green
Get-ChildItem $out | Select-Object Name, @{n='Size';e={ if($_.PSIsContainer){''}else{"{0:N1} MB" -f ($_.Length/1MB)} }} | Format-Table -AutoSize
Write-Host "把 .venv 和 models 复制到 $out 即可独立运行（字幕服务需要）。" -ForegroundColor Yellow

# 回退分支（原地覆盖）**不能宣称成功**：旧代码只打一行黄字然后照旧打印"[4/4] 完成"
# 并 exit 0，dist-app 里的旧文件残留/半删状态会被 build_installer 当正常产物打包。
# build_installer 只看退出码，所以这里必须以非零码收尾把它拦下。
if (-not $cleanRemoved) {
    Write-Host "[!] 未能清理 dist-app：本次是原地覆盖写入，仓库已删除的旧文件仍残留在产物里，产物可能不完整。" -ForegroundColor Red
    Write-Host "    已以非零退出码结束（安装包构建会被拦下）。请关掉占用 dist-app 的进程后重跑：" -ForegroundColor Red
    Write-Host "    llama-server / 资源管理器预览 / 杀软扫描 / 终端的当前目录停在 dist-app 内。" -ForegroundColor Red
    exit 3
}
