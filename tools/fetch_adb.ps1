# 下载 Android platform-tools（adb.exe）到 tools\adb\，随应用一起分发
#
#   powershell -ExecutionPolicy Bypass -File tools\fetch_adb.ps1
#
# 为什么自带：adb 不是系统自带组件。此前依赖"用户机器上恰好装过 Android SDK /
# 在 PATH 上"，没有就得让用户自己去下载——现在宿主优先探测 tools\adb\adb.exe，
# 本脚本跑一次（约 6 MB）即可让设备同步开箱即用。
param(
    [string]$Url = "https://dl.google.com/android/repository/platform-tools-latest-windows.zip",
    [switch]$Force      # 已存在也重新下载覆盖
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dst  = Join-Path $root 'tools\adb'
$exe  = Join-Path $dst 'adb.exe'

if ((Test-Path $exe) -and -not $Force) {
    Write-Host "adb 已存在：$exe（要更新请加 -Force）" -ForegroundColor Yellow
    & $exe version 2>$null | Select-Object -First 2
    exit 0
}

$tmp = Join-Path $env:TEMP ("nexus-adb-" + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Force -Path $dst, $tmp | Out-Null
Write-Host "[1/3] 下载 $Url" -ForegroundColor Cyan
$zip = Join-Path $tmp "platform-tools.zip"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$env:NO_PROXY = '*'; $env:no_proxy = '*'   # 系统代理残留会劫持下载（同 fetch_llama 的教训）
Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing

Write-Host "[2/3] 解压 adb.exe / AdbWinApi.dll / AdbWinUsbApi.dll" -ForegroundColor Cyan
Add-Type -AssemblyName System.IO.Compression.FileSystem
$z = [IO.Compression.ZipFile]::OpenRead($zip)
try {
    foreach ($name in 'adb.exe', 'AdbWinApi.dll', 'AdbWinUsbApi.dll') {
        $entry = $z.Entries | Where-Object { $_.Name -eq $name } | Select-Object -First 1
        if (-not $entry) { throw "zip 里找不到 $name" }
        $out = Join-Path $dst $name
        [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $out, $true)
    }
} finally { $z.Dispose() }

Write-Host "[3/3] 自检" -ForegroundColor Cyan
if (-not (Test-Path $exe)) { throw "解压后仍缺 adb.exe：$exe" }
& $exe version | Select-Object -First 2
Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
Write-Host "[完成] adb 已随应用分发：$dst" -ForegroundColor Green
