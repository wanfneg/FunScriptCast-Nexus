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
if (Test-Path $out) {
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

Write-Host "[3/4] 清理临时目录…" -ForegroundColor Cyan
Remove-Item (Join-Path $root 'build\work') -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $root 'dist') -Recurse -Force -ErrorAction SilentlyContinue

Write-Host "[4/4] 完成。" -ForegroundColor Green
Get-ChildItem $out | Select-Object Name, @{n='Size';e={ if($_.PSIsContainer){''}else{"{0:N1} MB" -f ($_.Length/1MB)} }} | Format-Table -AutoSize
Write-Host "把 .venv 和 models 复制到 $out 即可独立运行（字幕服务需要）。" -ForegroundColor Yellow
