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

if (-not $NoBump) {
    Write-Host "[0/4] 递增版本号…" -ForegroundColor Cyan
    & powershell -ExecutionPolicy Bypass -File (Join-Path $root 'tools\bump_version.ps1')
    if ($LASTEXITCODE -ne 0) { throw "版本号递增失败" }
}

Write-Host "[1/4] PyInstaller 打包…" -ForegroundColor Cyan
& $py -m PyInstaller (Join-Path $PSScriptRoot 'nexus.spec') --noconfirm --clean --distpath (Join-Path $root 'dist') --workpath (Join-Path $root 'build\work')
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（exit $LASTEXITCODE）" }

$exe = Join-Path $root 'dist\FunScriptCast-Nexus.exe'
if (-not (Test-Path $exe)) { throw "没有产出 EXE：$exe" }

Write-Host "[2/4] 组装 dist-app…" -ForegroundColor Cyan
$out = Join-Path $root 'dist-app'
# 组装前必须停掉字幕服务：它就从 dist-app\vendor\subtitle 运行，
# 进程不死会导致删除/覆盖不完整，产出残缺目录（2026-09-16 实测踩坑）
Get-NetTCPConnection -LocalPort 8756 -State Listen -ErrorAction SilentlyContinue |     ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
# 应用本体也锁 exe：一并停止（构建完成后由安装/用户重新启动）
Get-Process -Name 'FunScriptCast-Nexus' -ErrorAction SilentlyContinue |     Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
# 运行配置里用户在 UI 填的云端 key 存在 dist-app 的 config.json（坑 #12 的根源）。
# 覆盖 vendor 前先摘出来，组装完再回填——重编不再丢 key。
$distCfgPath = Join-Path $out 'vendor\subtitle\config.json'
$preservedKey = ''
if (Test-Path $distCfgPath) {
    try { $preservedKey = [string]((Get-Content $distCfgPath -Raw -Encoding UTF8 | ConvertFrom-Json).translate.openai.api_key) } catch { }
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
    } catch {
        # 目录可能被资源管理器/杀软/上次运行的进程占用，删不掉就原地覆盖
        Write-Host "  dist-app 无法删除（被占用），改为覆盖写入" -ForegroundColor Yellow
    }
}
New-Item -ItemType Directory -Path $out -Force | Out-Null
Copy-Item $exe $out -Force
foreach ($d in 'ui', 'vendor', 'tools') {
    Copy-Item (Join-Path $root $d) (Join-Path $out $d) -Recurse -Force
}
foreach ($f in 'version.json', 'start.bat', 'README.md') {
    Copy-Item (Join-Path $root $f) $out -Force
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
